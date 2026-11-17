"""Check a deployment's ConfigMap and Secret against the configuration this build asks for.

The gap this closes is not "the app is broken". It is "the app was fine and then somebody renamed a variable, or
added a typo, or upgraded mcbot to a build that wants a variable this deployment never supplied" - and each of
those produces a *running* deployment with a setting that silently took a default. Half of them default to
something safe, which is the point: a deployment that quietly stops being able to do the thing you deployed it
for looks identical to a deployment that is working.

Reads plain Kubernetes manifests (no cluster access, no YAML library) and .env files, and reports four things:

- a required variable that is absent;
- a variable the deployment asked for by switching something on, and did not supply;
- a name the deployment sets that no version of this code reads - a typo, a leftover, or a rename that went one
  way only;
- what the deployment actually arms: the capability switches and the plugin allowlist, as resolved values, so a
  reviewer can see "in-game actions off, no plugin allowed" in the output instead of trusting the file.

    python3 hack/check-mcbot-config.py deploy/example/configmap.yaml deploy/example/secret.example.yaml
    python3 hack/check-mcbot-config.py deploy/example/configmap.yaml deploy/example/secret.example.yaml

Exit 0 when the deployment is complete, 1 when it is not.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config_table as cfg  # noqa: E402

KEY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$")


# A name the pod receives from `valueFrom:` rather than from a value in a file. The checker cannot see it and
# is not supposed to: a Secret applied out of band is the point. Marking it as injected is what keeps the
# checker demanding a value it has no business asking for, while still reporting that the name is supplied.
INJECTED = "<valueFrom>"


def read_manifest(path: str) -> dict[str, str]:
    """Keys from `data:` / `stringData:` of a ConfigMap or Secret, from a Deployment's `env:`, or from .env.

    Line-based on purpose. This runs in CI, in a pod, and on a laptop; none of those have PyYAML in common, and
    the only thing needed here is which names carry which values. Multi-line block scalars are not parsed - a
    ConfigMap value for mcbot is a single line in every case this checker knows about.

    A Deployment is worth reading because that is where a secret gets its *name in the container*: `RCON_PASSWORD`
    in a Secret and `MCBOT_RCON_PASSWORD` in the pod are the same value, and a checker that only reads the
    ConfigMap reports the password missing on a deployment that has it.
    """
    values: dict[str, str] = {}
    inside = False
    indent = 0
    mode = ""
    entry = ""          # the name of the `env:` list item currently being read
    entry_indent = 0    # the indent of that item's `- name:` line, so nested `name:` keys stay out
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"^(\s*)(data|stringData|env):", line)
            if m:
                inside = True
                mode = m.group(2)
                indent = len(m.group(1))
                entry = ""
                continue
            if not inside:
                # A flat KEY=value file.
                m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", stripped)
                if m:
                    values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= indent and stripped and not stripped.startswith("-"):
                inside = False
                continue
            if mode == "env":
                m = re.match(r"^- name: ([A-Za-z0-9_.-]+)$", stripped)
                if m:
                    entry, entry_indent = m.group(1), current_indent
                    values.setdefault(entry, INJECTED)
                    continue
                m = re.match(r"^name: ([A-Za-z0-9_.-]+)$", stripped)
                if m and current_indent <= entry_indent + 2:
                    # The entry's own `name:` key, which only appears when a list item is written on separate
                    # lines. Anything deeper is a `secretKeyRef.name`, which names an object and not a variable.
                    entry = m.group(1)
                    values.setdefault(entry, INJECTED)
                    continue
                m = re.match(r"^value: (.*)$", stripped)
                if m and entry:
                    values[entry] = m.group(1).strip().strip('"').strip("'")
                    continue
                continue
            if stripped.startswith("name:"):
                m = KEY_RE.match(stripped)
                if m:
                    values.setdefault(m.group(1), "")
                continue
            m = KEY_RE.match(stripped)
            if m:
                values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return values


def check(values: dict[str, str], origin: str, complete: dict[str, str]) -> tuple[list[str], list[str]]:
    """`values` is one file; `complete` is every file in the deployment merged.

    Required-ness is judged against the merged view because a Secret is allowed to be where the password lives.
    An unknown name is judged per file, because "which file has the typo" is the useful question.
    """
    problems: list[str] = []
    notes: list[str] = []

    # The api key is the one variable whose name is itself configured, so the name in the manifest is whatever
    # `MCBOT_PI_API_KEY_ENV` says. Flagging it as unread would be the checker contradicting its own design.
    key_env = str(cfg.value_of("MCBOT_PI_API_KEY_ENV", complete)).strip()
    unknown = sorted(name for name in values
                     if name not in cfg.names()
                     and name != key_env
                     and re.match(r"^(MCBOT_|DISCORD_|PI_)", name))

    if unknown:
        problems.append(f"{origin}: set but not read by this build (a typo, a leftover, or a rename that went "
                        f"one way only): {', '.join(unknown)}")

    for name in sorted(values):
        if name not in cfg.names():
            continue
        if values[name] == INJECTED:
            notes.append(f"{origin}: {name} is supplied by valueFrom, so this checker cannot see its value")
            continue
        if not str(values[name]).strip():
            e = cfg.entry(name)
            if e.required or e.when:
                problems.append(f"{origin}: {name} is present but empty, and it has no default to fall back to")
            elif e.default:
                notes.append(f"{origin}: {name} is empty, so this build uses its default {e.default!r}")

    return problems, notes


def visible(values: dict[str, str]) -> dict[str, str]:
    """The merged view with the unread values removed.

    Conditions and capability switches have to be evaluated against values the checker can actually read;
    treating a `valueFrom` placeholder as a string would arm a switch on the strength of its name.
    """
    return {k: v for k, v in values.items() if v != INJECTED}


def armed(values: dict[str, str]) -> list[str]:
    """What the deployment arms, as resolved values rather than as strings in a file.

    Printed whether or not anything is wrong, because the interesting question about a bot that can change a
    server is not whether it starts. These four lines are also the fastest way to see that an upgrade changed a
    default underneath you.
    """
    out = []
    for name, what in (
        ("MCBOT_SERVER_ACTIONS", "out-of-game changes (manifests, whitelist, restarts)"),
        ("MCBOT_INGAME_ACTIONS", "in-game changes (give, teleport, gamemode, kick)"),
    ):
        if name in values and name not in visible(values):
            out.append(f"{name} -> supplied by valueFrom; this checker cannot see whether it is on")
            continue
        on = cfg.value_of(name, values)
        out.append(f"{name} -> {'ON' if on else 'off'} ({what})")
    allow = cfg.value_of("MCBOT_PLUGIN_ALLOWLIST", values)
    out.append("MCBOT_PLUGIN_ALLOWLIST -> "
               + (", ".join(allow) if allow else "empty (no plugin jar may be committed)"))
    out.append(f"MCBOT_APPROVER_MODE -> {cfg.value_of('MCBOT_APPROVER_MODE', values)}")
    return out


def deployment_problems(complete: dict[str, str]) -> list[str]:
    """What the deployment as a whole fails to supply.

    Judged once, against every file together: mcbot's password lives in a Secret, its switches live in a
    ConfigMap, and a name injected by `valueFrom` has a value this checker is not allowed to see. A per-file
    version of this check reports the same missing variable once per file, which teaches people to skim past it.
    """
    problems: list[str] = []
    real = visible(complete)
    missing = [n for n in cfg.missing_required(complete) if complete.get(n) != INJECTED]
    conditional = [pair for pair in cfg.missing_conditions(real) if complete.get(pair[0]) != INJECTED]
    if missing:
        problems.append("required and absent: " + ", ".join(missing))
    if conditional:
        problems.append("required by the settings in force: "
                        + "; ".join(f"{name} ({why})" for name, why in conditional))
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    all_problems: list[str] = []
    all_notes: list[str] = []
    merged: dict[str, str] = {}
    for path in argv:
        if not os.path.exists(path):
            print(f"FAIL: no such file: {path}")
            return 1
        values = read_manifest(path)
        if not values:
            print(f"FAIL: {path}: no configuration keys found")
            return 1
        for name, value in values.items():
            if name in merged and merged[name] == INJECTED and value != INJECTED:
                merged[name] = value
            else:
                merged.setdefault(name, value)

    # Two passes: each file on its own for names nothing reads, the merged deployment for what is required.
    for path in argv:
        values = read_manifest(path)
        problems, notes = check(values, os.path.basename(path), merged)
        if not values:
            continue
        all_problems += problems
        all_notes += notes

    all_problems += deployment_problems(merged)

    for line in all_notes:
        print("  " + line)
    for line in armed(merged):
        print("  armed: " + line)
    for line in all_problems:
        print("PROBLEM " + line)
    if all_problems:
        print(f"\nFAIL: {len(all_problems)} configuration problem(s); see docs/configuration.md")
        return 1
    print(f"\nOK: {len(merged)} variable(s) supplied; every variable this build requires is present")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
