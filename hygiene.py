#!/usr/bin/env python3
"""Private-artefact hygiene for this repository, runnable with no dependencies at all.

Split out of test-config.py so it can also run in the dependency-free CI gate step, which works on the clone rather
than on the built image. The two runs answer different questions and neither is redundant: the gate step sees every
document in the repository, including the ones nobody installs, and the build-time run sees what the image actually
ships - the stricter question about what a stranger receives, and completely blind to a prose document that was
never copied in. A maintenance note that names a hostname is published just as readily as code that names one, and
the reason it survives is that nobody reads comments.

**The list of names this project keeps private is not in this file.** It cannot be. A blocklist that spells out one
person's repository names, CI hostnames, tailnet range and pod names is itself the leak it exists to prevent, and
it is standing in a public repository for anybody to read. So this file holds the *shapes* that are private
wherever they appear, and the names go in `hygiene.private.txt` beside it - gitignored, owner-only, read if it is
there and silently unused if it is not:

    # one literal per line; blank lines and # comments ignored
    the-name-of-my-manifests-repository
    the-name-of-my-ci-node

Which is an honest division rather than a complete one. On the owner's machine the check knows the names. In CI,
where the file does not exist, it enforces the shapes and nothing more - so a fork gets a working check with no
information about anybody's infrastructure, and the owner gets the stronger version. Closing that gap in CI means
feeding the file to the gate step as a secret, which is a decision about the CI deployment rather than about this
code, and is written down in `AGENTS.md` as an open item instead of being quietly half-done here.

What the shapes cover:

- **A private address.** RFC1918 and link-local ranges. In a public repository this is the same leak as a hostname,
  with extra charm: it looks like a placeholder until somebody's router is on it. Fixtures here use documentation
  ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) and `.test` / `.example` / `.invalid` hostnames,
  which are reserved for exactly this and are not leaks.
- **A hostname that only resolves inside a private network**: a single-label host, a `.internal` / `.local` /
  `.home` / `.lan` suffix, or a Kubernetes service FQDN built on a *specific* cluster domain (the generic
  `svc.cluster.local` shape is fine to document; a real cluster's own suffix is not).
- **A real Discord id.** An id is a snowflake with a timestamp inside it, so it is data about an account. Fixture
  ids here use repeated digits on purpose, and `real_snowflake` keeps the distinction: a suite may hold
  `111111111111111111` to prove a policy, and must not hold somebody's actual server, bot or user id.
- **A person's name.** `PERSON_KEY_RE` looks for a name-shaped value - two capitalised words - assigned to a
  person-shaped key, which is where a name gets written into a fixture. A single common first name has no shape
  that separates it from a placeholder, and that is stated rather than papered over: it is what the owner's
  private list is for.

Run it directly:

    python3 hygiene.py            # proves itself, then names any file that leaks, or says the tree is clean
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRIVATE_NAMES_FILE = HERE / "hygiene.private.txt"

# Hostnames reserved by RFC for documentation and testing. A fixture is allowed to use these and a check that
# flagged them would train people to switch the check off.
RESERVED_HOSTS = ("example.com", "example.org", "example.net", "example.test", "example.invalid",
                  "example.local", "localhost", "localhost.localdomain")
RESERVED_TLDS = (".test", ".example", ".invalid", ".local.example")

# Suffixes that mean "inside somebody's network". `.local` alone is mDNS, which is a private resolution domain.
PRIVATE_SUFFIXES = (".internal", ".lan", ".home", ".home.arpa", ".corp", ".private", ".local")
# ...except the two shapes that are not somebody's network but the shape every cluster has. Kubernetes' default
# cluster domain is how any fork addresses a service across namespaces, and a check that flags it teaches people
# to switch the check off - which is why it is written down as an exception rather than left to luck.
GENERIC_CLUSTER_SHAPES = (".svc.cluster.local", "cluster.local")

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SNOWFLAKE_RE = re.compile(r"\b\d{15,20}\b")
# A cluster's own service suffix: `svc.` followed by something that is not the generic `cluster.local`, e.g. a
# cluster named after the person who runs it. The generic shape is documentation and is allowed.
CLUSTER_SUFFIX_RE = re.compile(r"\bsvc\.(?!cluster\.local\b)[a-z0-9][a-z0-9.-]*\b", re.I)
# Name-shaped values assigned to a person-shaped key. Two capitalised words is the shape a real name has; a
# single common first name is not distinguishable from a placeholder by shape at all, which is exactly what
# `hygiene.private.txt` is for and why this pattern is narrow rather than ambitious. A pattern that flags every
# proper noun is a check people switch off.
PERSON_KEY_RE = re.compile(
    r"""(?:"name"|'name'|\bname|\buser|\bplayer|\bwho|\bauthor|\bowner)\s*[:=]\s*"([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})"""
)
PERSON_ALLOW = {"Some Player", "Unknown Player", "Alice Example", "Bob Example", "Someone Someone",
                "Player One", "Test User", "Example Player", "Not Secure"}


def private_names() -> tuple[str, ...]:
    """The owner's own names, read from a file that is not in the repository.

    Absent in a fork and absent in CI, and that is the design: a public repository must not carry the list that
    tells a reader what to look for. When the file is missing the shapes still run.
    """
    if not PRIVATE_NAMES_FILE.exists():
        return ()
    lines = PRIVATE_NAMES_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(l.strip() for l in lines if l.strip() and not l.strip().startswith("#"))


def reserved_address(text: str) -> bool:
    """Documentation and test ranges, which fixtures are allowed to use."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True
    return (ip in ipaddress.ip_network("192.0.2.0/24")
            or ip in ipaddress.ip_network("198.51.100.0/24")
            or ip in ipaddress.ip_network("203.0.113.0/24")
            or ip in ipaddress.ip_network("127.0.0.0/8"))


def private_addresses(text: str) -> list[str]:
    hits = []
    for m in IPV4_RE.finditer(text):
        if not reserved_address(m.group(0)) and re.match(r"^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.|169\.254\.)",
                                                         m.group(0)):
            hits.append(m.group(0))
    return hits


def private_hosts(text: str) -> list[str]:
    """Hostnames that resolve only somewhere private, or that name one cluster rather than a shape."""
    hits: list[str] = []
    for m in re.finditer(r"\b[a-z0-9][a-z0-9.-]*\.[a-z][a-z0-9.-]*\b", text):
        host = m.group(0)
        if host in RESERVED_HOSTS or host.endswith(RESERVED_TLDS):
            continue
        if host.endswith(GENERIC_CLUSTER_SHAPES):
            continue
        if any(host.endswith(s) for s in PRIVATE_SUFFIXES):
            hits.append(host)
    for m in CLUSTER_SUFFIX_RE.finditer(text):
        hits.append(m.group(0))
    return sorted(set(hits))


def real_snowflake(text: str) -> list[str]:
    """Discord ids that look like ids rather than like fixtures.

    A fixture id is a repeated digit; a real snowflake is a timestamp in disguise and has almost no repeated
    digits. What must not be published is an id that identifies somebody's actual account, server or bot.
    """
    return [m.group(0) for m in SNOWFLAKE_RE.finditer(text) if len(set(m.group(0))) >= 6]


def people(text: str) -> list[str]:
    """Real-looking names in the places a real name gets written into a fixture."""
    found = []
    for m in PERSON_KEY_RE.finditer(text):
        value = m.group(1)
        if value in PERSON_ALLOW:
            continue
        found.append(value)
    return found


# Files that have to name what they forbid in order to forbid it. The list is checked against the files it excuses
# (see selftest), because an exemption that has outlived its reason is worse than none: it hides the next leak.
EXEMPT = {
    "hygiene.py": "this file defines the patterns and plants the fixtures that prove them, so it has to write them "
                  "down",
}
# The pipeline directory is exempt for a different reason, stated rather than assumed: a workflow has to name the
# daemon it builds against, the image it publishes and the manifests repository it deploys into. It is the owner's
# file and nothing in the shipped program reads it - a fork rewrites it. The selftest keeps the exemption honest by
# requiring the file to still name infrastructure.
EXEMPT_PREFIXES = (".woodpecker/",)
INFRA_RE = re.compile(r"tcp://[a-z0-9.-]+:[0-9]{2,5}|\b[a-z0-9.-]+\.[a-z]{2,}/[a-z0-9._/-]+")


def tracked_files(root: Path = HERE) -> list[Path]:
    """Everything in the repository that a stranger would receive.

    `git ls-files` is the honest answer and the only one that can see a document nobody installs. Without a git
    binary - which is the case in the CI gate step's image, while its working directory is a full clone, and also
    the case inside the built image - this walks the tree instead, which is why the fallback exists rather than
    being a convenience. The walk skips VCS internals, a virtualenv and compiled bytecode: none of that is
    published, and a `.pyc` is a stale copy of constants its source file has already been scanned for.
    """
    try:
        r = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        r = None
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return [root / p for p in r.stdout.splitlines() if (root / p).exists()]
    return [p for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts and ".venv" not in p.parts
            and "__pycache__" not in p.parts and p.suffix != ".pyc"]


def findings(text: str) -> list[str]:
    """Everything private about one file's contents, as a list of what was found."""
    hits = private_addresses(text) + private_hosts(text) + real_snowflake(text) + people(text)
    hits += [n for n in private_names() if n in text]
    return hits


def _scan(root: Path, exempt: set[str], exempt_prefixes: tuple[str, ...]) -> list[str]:
    offenders: list[str] = []
    for f in tracked_files(root):
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        rel = f.relative_to(root).as_posix()
        if rel in exempt or rel.startswith(exempt_prefixes):
            continue
        hits = findings(text)
        if hits:
            offenders.append(f"{rel}: " + ", ".join(sorted(set(hits))[:8]))
    return offenders


def scan(root: Path = HERE) -> list[str]:
    """Every file in the tree that names a private address, a private hostname, a person or a real id."""
    return _scan(root, EXEMPT, EXEMPT_PREFIXES)


def selftest() -> list[str]:
    """Proof that the scanner refuses what it claims to refuse, and that each exemption is still earned.

    A rule nobody has watched fire is a rumour, and this one has already been the cause of a failed pipeline, so
    every branch gets a planted case in a scratch tree: a private address, a reserved address that must *not* be
    flagged, a private suffix, a cluster suffix that is a real one, a fixture id, a timestamp-shaped id, and a
    name-shaped placeholder. Then each exemption is checked against the file it excuses - an exemption that has
    outlived its reason is worse than none, because it hides the next leak.
    """
    import tempfile

    fails: list[str] = []
    cases = {
        "an RFC1918 address is reported": ("a.md", "reach it on 10.101.4.5\n", 1),
        "the other RFC1918 ranges are reported": ("b.md", "172.20.0.1 and 192.168.1.7\n", 2),
        "a documentation address is not reported": ("c.md", "fixture 203.0.113.9\n", 0),
        "a reserved hostname is not reported": ("d.md", "https://example.test/x\n", 0),
        "a private suffix is reported": ("e.md", "http://build-node.internal:8000\n", 1),
        "a real cluster suffix is reported": ("f.md", "svc.myownclustername.local\n", 1),
        "the generic cluster shape is not reported": ("g.md", "svc.cluster.local\n", 0),
        "a repeated-digit fixture id is not reported": ("h.py", "GUILD = \"111111111111111111\"\n", 0),
        "a timestamp-shaped id is reported": ("i.py", "USER = \"1143090350882299957\"\n", 1),
        "a name-shaped placeholder is not reported": ("j.json", "{\"who\": \"Some Player\"}\n", 0),
    }
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name, (rel, body, want_hits) in cases.items():
            got = len(findings(body))
            if want_hits == 0:
                # "Must not be reported" has to actually assert that. A case written only as `got < wanted` lets a
                # check that flags everything pass its own negative cases, which is how this scanner nearly shipped
                # with the generic Kubernetes cluster domain treated as somebody's private network.
                if got:
                    fails.append(f"{name}: reported {got} hit(s) that should have been clean: {findings(body)[:4]}")
            elif got < want_hits:
                fails.append(f"{name}: found {got}, wanted at least {want_hits}")
        # The tree walk has to reach a document, not just code, or a maintenance note is invisible to the check.
        (root / "docs" / "NOTE.md").parent.mkdir(parents=True, exist_ok=True)
        (root / "docs" / "NOTE.md").write_text("the build node is build-node.internal\n")
        (root / "sub" / "pipeline.yaml").parent.mkdir(parents=True, exist_ok=True)
        (root / "sub" / "pipeline.yaml").write_text("daemon: some-host.internal\n")
        walk = _scan(root, set(), ("sub/",))
        if not any("docs/NOTE.md" in o for o in walk):
            fails.append("a private name in a document was not found by the tree walk")
        if any("sub/pipeline.yaml" in o for o in walk):
            fails.append("an exempt prefix was scanned anyway")

    for rel, why in EXEMPT.items():
        p = HERE / rel
        if not p.exists():
            fails.append(f"exemption for {rel} has no file behind it ({why})")
            continue
        if not findings(p.read_text(encoding="utf-8", errors="replace")):
            fails.append(f"exemption for {rel} is no longer earned: it names nothing private ({why})")
    for prefix in EXEMPT_PREFIXES:
        d = HERE / prefix
        if not d.exists():
            fails.append(f"exempt prefix {prefix} covers no directory")
            continue
        files = [f for f in d.rglob("*") if f.is_file()]
        if not files:
            fails.append(f"exempt prefix {prefix} covers no files")
        elif not any(INFRA_RE.search(f.read_text(encoding="utf-8", errors="replace")) for f in files):
            fails.append(f"exempt prefix {prefix} excuses nothing that names infrastructure")
    return fails


def main() -> int:
    problems = selftest()
    if problems:
        print("the scanner itself is broken:")
        for p in problems:
            print(f"  - {p}")
        return 1
    names = private_names()
    offenders = scan()
    if offenders:
        print("private artefacts in tracked files:")
        for o in offenders:
            print(f"  - {o}")
        return 1
    where = f"including {len(names)} owner names" if names else "shapes only: no hygiene.private.txt here"
    print(f"clean: {len(tracked_files())} files name no private address, private hostname, person or real id "
          f"({where}, and the scanner proved it catches a planted one)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
