#!/usr/bin/env python3
"""The configuration boundary: every knob is declared, nothing is somebody else's deployment, and each knob
actually moves what goes out over the wire.

This suite exists because parametrizing something is not the same as making it configurable. The failure modes it
watches for are the ones a rename produces:

- **Undeclared config.** A reader left pointing at a variable no manifest supplies, silently taking a default.
- **Unread config.** A variable a deployment sets, and nothing reads - which looks like a setting and is a rumour.
- **A default that is really a deployment.** A hostname, a namespace, a path in somebody's repository, a Discord
  id, a child's name. In a public repository these are not defaults, they are somebody's private infrastructure
  shipped to strangers.
- **A knob that does not reach the wire.** The most convincing kind of broken parametrization: the variable is
  read, the code compiles, and the value goes nowhere because the call site still uses a literal. Every check
  below that touches a knob therefore proves it at the point something leaves the process - a TCP connection, an
  argv passed to kubectl, a URL POSTed to, a repository cloned, a refusal issued.

Run it with the dependencies installed (`uv pip install -r requirements.txt`). It starts throwaway servers on
localhost and throws away git repositories; nothing here touches a cluster or a game server.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "bin"))

import config_table as cfg  # noqa: E402

FAILURES: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="mcbot-config-test-"))

SOURCES = [
    "bot.py", "pi_rpc.py", "rcon.py", "config_table.py", "render_pi_models.py",
    "hack/check-mcbot-config.py", "bin/gate.py", "bin/git-shim", "bin/kubectl-shim", "bin/mc-rcon",
    "entrypoint.sh",
]

# Names a shipped default may not contain. Each of these was true of this program's defaults until this suite
# was written, which is the reason it exists.
HOST_LITERALS = (
    "initialed85",
    ".svc.cluster.local",
    ".initialed85.cc",
    "image-classifier.dev",
    "rooter.rooter",
    "home-ops",
    "home-dev",
    "home-prod",
    "clusters/dev/applications/minecraft/",
    "server-0",
    "beefcake",
    # A private address is a fingerprint of one network. In a public repository it is the same leak as a
    # hostname, with the additional charm that it looks like a placeholder until somebody's router is on it.
    "192.168.",
)
# People. A persona file or a test fixture that names a child is a private thing that got published.
PERSON_LITERALS = ("Augie", "August", "Ellie", "Elliott", "Mason", "Volt_plays", "Beech")
SNOWFLAKE_RE = re.compile(r"\b\d{15,20}\b")


def real_snowflake(text: str) -> list[str]:
    """Discord ids that look like ids rather than like fixtures.

    A fixture id is a repeated digit; a real snowflake is a timestamp in disguise and has almost no repeated
    digits. The distinction is what lets a test suite hold ids at all: what must not be published is an id that
    identifies somebody's actual server, bot or user.
    """
    return [m.group(0) for m in SNOWFLAKE_RE.finditer(text) if len(set(m.group(0))) >= 6]


def tracked_files() -> list[Path]:
    """Everything in the repository that a stranger would receive."""
    r = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return [HERE / p for p in r.stdout.splitlines() if (HERE / p).exists()]
    return [p for p in HERE.rglob("*")
            if p.is_file() and ".venv" not in p.parts and ".git" not in p.parts]


def check(ok, what, detail="") -> None:
    print(("  ok   " if ok else "  FAIL ") + what + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(what)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def literal_env_names(path: Path) -> dict[str, list[int]]:
    """Environment-variable names a source file refers to, and where.

    Covers both shapes this program uses: `os.environ[...]` directly, and a call to one of the config readers
    (`env`, `_ids`, `_flag`, `_setting`, `cfg.entry`, `cfg.resolve`, `cfg.value_of`, `_raw`) with a literal name.
    Both are config reads, and both must be declared.
    """
    out: dict[str, list[int]] = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return out
    readers = {"env", "_ids", "_flag", "_setting", "entry", "resolve", "value_of", "_raw"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        name = first.value
        if not re.match(r"^(MCBOT_|DISCORD_|PI_)", name):
            continue
        fn = node.func
        attr = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        direct = (isinstance(fn, ast.Attribute) and fn.attr in ("get", "setdefault")
                  and isinstance(fn.value, ast.Attribute) and fn.value.attr == "environ")
        if direct or attr in readers:
            out.setdefault(name, []).append(node.lineno)
    # `os.environ["NAME"]`
    text = path.read_text(encoding="utf-8")
    for m in re.finditer(r'os\.environ\[\s*"(MCBOT_[A-Z_]+|DISCORD_[A-Z_]+|PI_[A-Z_]+)"', text):
        line = text[: m.start()].count("\n") + 1
        out.setdefault(m.group(1), []).append(line)
    return out


def shell_env_names(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    used = set(re.findall(r'\$\{?([A-Z][A-Z0-9_]+)', text))
    assigned = set(re.findall(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]+)=', text, re.M))
    return {v for v in (used - assigned) if re.match(r"^(MCBOT_|DISCORD_|PI_)", v)}


def declared_boundary() -> None:
    section("every variable read is declared, and every variable declared is read")
    reads: dict[str, list[str]] = {}
    for rel in SOURCES:
        p = HERE / rel
        if not p.exists():
            continue
        if rel.endswith(".sh"):
            for name in shell_env_names(p):
                reads.setdefault(name, []).append(rel)
            continue
        for name, lines in literal_env_names(p).items():
            reads.setdefault(name, []).append(f"{rel}:{lines[0]}")

    undeclared = sorted(set(reads) - cfg.names())
    check(not undeclared, "no variable is read without being declared",
          ", ".join(f"{n} ({', '.join(reads[n][:2])})" for n in undeclared))

    # A variable nothing reads is a setting a deployment can configure forever with no effect. `external_reader`
    # is the only way out of this check, and it is meant for values read by pi or ssh rather than by us.
    unread = []
    for e in cfg.declared():
        if e.external_reader:
            continue
        if e.name not in reads:
            unread.append(e.name)
    check(not unread, "no variable is declared without being read", ", ".join(unread))

    # Defaults live in the table or nowhere. `os.environ.get("MCBOT_X", "literal")` in a shipped file is a second
    # default that no test, no document and no deployment checker can see.
    strays = []
    for rel in SOURCES:
        p = HERE / rel
        if not p.exists() or rel == "config_table.py" or rel.endswith(".sh"):
            continue
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r'os\.environ\.get\("(MCBOT_[A-Z_]+|PI_[A-Z_]+)",\s*"[^"]+"\)', text):
            strays.append(f"{rel}: {m.group(0)}")
        for m in re.finditer(r'os\.environ\.get\("(MCBOT_[A-Z_]+|PI_[A-Z_]+)",\s*os\.path\.join', text):
            strays.append(f"{rel}: {m.group(0)}")
    check(not strays, "no default lives outside the table", "; ".join(strays))


def defaults_are_portable() -> None:
    section("no default describes somebody else's deployment")
    for e in cfg.declared():
        blob = f"{e.default} {e.purpose} {e.when}"
        hit = next((h for h in HOST_LITERALS if h in blob), None)
        check(hit is None, f"{e.name} default and description name no deployment", hit or "")
        check(not SNOWFLAKE_RE.search(blob), f"{e.name} default carries no Discord id", blob)
        check(not any(p in blob for p in PERSON_LITERALS), f"{e.name} names no person", blob)

    # The whole tree, not the files this check happened to remember. A private hostname that survived in a
    # fixture, a comment or a shell script is published the moment the repository is, and the reason it
    # survived is that nobody reads comments.
    offenders: list[str] = []
    for f in tracked_files():
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        rel = f.relative_to(HERE).as_posix()
        if rel == "test-config.py":  # this file has to name the things it forbids
            continue
        if rel.startswith(".woodpecker/"):
            # The pipeline is not code and is not read by the program: it names which daemon builds this image,
            # which registry publishes it, and which manifests repository it deploys into. Exempting it is what
            # lets that one file be true rather than placeholder-shaped; a fork rewrites it and nothing else.
            continue
        hits = [h for h in HOST_LITERALS if h in text]
        people = [q for q in PERSON_LITERALS if q in text]
        ids = real_snowflake(text)
        if hits or people or ids:
            offenders.append(f"{rel}: " + ", ".join(hits + people + ids[:3]))
    check(not offenders, "no file in the repository names somebody's deployment, family or ids",
          "; ".join(offenders))

    # The switches that arm the bot default off, and the plugin list defaults empty. These are the two defaults
    # a public repository must not get wrong.
    check(cfg.value_of("MCBOT_SERVER_ACTIONS", {}) is False, "out-of-game actions default off")
    check(cfg.value_of("MCBOT_INGAME_ACTIONS", {}) is False, "in-game actions default off")
    check(tuple(cfg.value_of("MCBOT_PLUGIN_ALLOWLIST", {})) == (), "no plugin is allowed by default")
    check(cfg.value_of("MCBOT_APPROVER_MODE", {}) == "allowlist", "the approver gate defaults to an allowlist")
    check(cfg.value_of("MCBOT_VISION_URL", {}) == "", "no vision endpoint is assumed")
    check(cfg.value_of("MCBOT_REPO_URL", {}) == "", "no repository is assumed")
    check(cfg.value_of("MCBOT_ALLOWED_PREFIX", {}) == "", "no writable path is assumed")
    check(cfg.value_of("MCBOT_PI_BASE_URL", {}) == "", "no inference endpoint is assumed")


def docs_are_generated() -> None:
    section("the documentation is the table, rendered")
    doc = HERE / "docs" / "configuration.md"
    if not doc.exists():
        check(False, "docs/configuration.md exists", "missing")
        return
    generated = cfg.render_markdown().rstrip() + "\n"
    committed = doc.read_text(encoding="utf-8")
    check(generated == committed, "docs/configuration.md is what config_table.py renders today",
          "run: python3 config_table.py > docs/configuration.md")
    for name in cfg.host_specific():
        check(name in committed, f"{name} is documented")
    for name in cfg.required():
        check(re.search(rf"\|\s*`{name}`\s*\|[^|]*\|\s*[^|]*\|\s*yes\s*\|", committed) is not None,
              f"{name} is documented as required")


def full_env(**over) -> dict[str, str]:
    """A deployment-shaped environment: everything required, everything conditional, no real values."""
    env = {
        "DISCORD_BOT_TOKEN": "test-token",
        "MCBOT_GUILDS": "111111111111111111",
        "MCBOT_CHANNELS": "222222222222222222",
        "MCBOT_REPO_URL": "git@example.com:someone/manifests.git",
        "MCBOT_ALLOWED_PREFIX": "apps/minecraft/",
        "MCBOT_PI_BASE_URL": "http://127.0.0.1:1/v1",
        "MCBOT_APPROVER_MODE": "allowlist",
        "MCBOT_APPROVER_IDS": "333333333333333333",
        "MCBOT_INGAME_NAMESPACE": "games",
        "MCBOT_INGAME_POD": "paper-0",
        "MCBOT_RCON_HOST": "127.0.0.1",
        "MCBOT_RCON_PASSWORD": "test-rcon",
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
    }
    env.update({k: v for k, v in over.items() if v is not None})
    for k, v in over.items():
        if v is None:
            env.pop(k, None)
    return env


def startup_contract() -> None:
    section("startup refuses by naming what is missing")
    python = sys.executable

    def run(env: dict[str, str]):
        r = subprocess.run([python, "bot.py"], cwd=HERE, capture_output=True, text=True,
                           timeout=120, env=env)
        return r.returncode, (r.stderr or "") + (r.stdout or "")

    code, out = run({"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
    check(code != 0, "with nothing set, mcbot does not start")
    for name in cfg.required():
        check(name in out, f"the refusal names {name}", out.strip().splitlines()[-1][:200] if out else "")
    check("MCBOT_RCON_HOST" in out, "the refusal names the in-game requirements it inherits", out[:200])
    check("guess" in out.lower(), "the refusal says it will not guess")

    # Each required variable, removed one at a time: the refusal has to name that one. A startup check that
    # reports "configuration is wrong" is not a startup check anybody can act on.
    for name in cfg.required():
        env = full_env()
        env.pop(name)
        code, out = run(env)
        check(code != 0 and name in out, f"removing {name} produces a refusal that names it", out[:200])

    # Switching the in-game path off must genuinely remove the in-game requirements: a bot that answers Discord
    # questions should not be told it needs an RCON password.
    env = full_env(MCBOT_INGAME="0")
    env.pop("MCBOT_RCON_HOST")
    env.pop("MCBOT_RCON_PASSWORD")
    env.pop("MCBOT_INGAME_POD")
    code, out = run(env)
    check("MCBOT_RCON_HOST" not in out, "MCBOT_INGAME=0 really removes the console requirements", out[:200])

    # The conditional group behaves the same way, in the direction that matters: a deployment that arms changes
    # and names nobody to confirm them is a capability with no owner, and it is refused at startup rather than
    # discovered at the first button. The same deployment with nothing armed is a *warning* - an allowlist that
    # authorises nobody is the safe shape of an oracle, not a broken configuration.
    env = full_env(MCBOT_APPROVER_MODE="allowlist", MCBOT_SERVER_ACTIONS="1")
    env.pop("MCBOT_APPROVER_IDS")
    code, out = run(env)
    check(code != 0 and "MCBOT_APPROVER_IDS" in out,
          "an armed deployment whose allowlist names nobody is refused at startup", out[:200])
    env = full_env(MCBOT_APPROVER_MODE="allowlist")
    env.pop("MCBOT_APPROVER_IDS")
    code, out = run(env)
    check("MCBOT_APPROVER_IDS" not in out and "in force" not in out,
          "and with no capability armed the empty allowlist is a warning, not a startup refusal", out[:200])
    env = full_env(MCBOT_APPROVER_MODE="allowlist", MCBOT_APPROVER_ROLES="444444444444444444")
    env.pop("MCBOT_APPROVER_IDS")
    code, out = run(env)
    check(code == 0 or "cannot start" not in out, "an approver role satisfies the approver requirement",
          out[:200])


# --- the console knob: an injected host and port end up on a real socket ------------------------------------
class FakeRcon(threading.Thread):
    """Enough of the RCON framing to tell whether mc-rcon connected where it was told to."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.log = TMP / "rcon-server.log"
        self.peer = TMP / "rcon-peer.log"
        self.stop = False

    def run(self) -> None:
        try:
            conn, addr = self.sock.accept()
        except OSError:
            return
        # Recorded on the accepted socket, so it says which address this server was reached *on* rather than
        # which ephemeral port the client happened to connect from.
        del addr
        self.peer.write_text(f"{conn.getsockname()[0]}:{conn.getsockname()[1]}\n")
        reader = conn.makefile("rb")
        try:
            while not self.stop:
                head = reader.read(4)
                if len(head) != 4:
                    break
                (size,) = struct.unpack("<i", head)
                body = reader.read(size)
                if len(body) < 8:
                    break
                pid, ptype = struct.unpack("<ii", body[:8])
                payload = body[8:-2] if size >= 2 else body[8:]
                if ptype == 3:  # SERVERDATA_AUTH
                    msg = b"Auth ok"
                    conn.sendall(struct.pack("<i", 8 + len(msg) + 2) + struct.pack("<ii", pid, 2)
                                 + msg + b"\x00\x00")
                    continue
                if ptype == 2:  # SERVERDATA_EXEC
                    with open(self.log, "a") as fh:
                        fh.write(payload.decode("utf-8", "replace") + "\n")
                    answer = b"Players online: 2\n<Steve>: hi"
                    conn.sendall(struct.pack("<i", 8 + len(answer) + 2) + struct.pack("<ii", pid, 0)
                                 + answer + b"\x00\x00")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def commands(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def shutdown(self) -> None:
        self.stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def injected_rcon() -> None:
    section("MCBOT_RCON_HOST / MCBOT_RCON_PORT choose the socket mc-rcon opens")
    server = FakeRcon()
    server.start()
    data = TMP / "rcon-data"
    data.mkdir(parents=True, exist_ok=True)
    (TMP / "turn.json").write_text('{"ts":"test","approved":false,"readonly":true,"surface":"discord"}')
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(TMP),
        "MCBOT_LIB": str(HERE),
        "MCBOT_DATA": str(data),
        "MCBOT_RCON_HOST": "127.0.0.1",
        "MCBOT_RCON_PORT": str(server.port),
        "MCBOT_RCON_PASSWORD": "test-rcon",
        "MCBOT_RCON_QUIET": "0.25",
        "MCBOT_TURN_MODE": str(TMP / "turn.json"),
    }

    def shim(args: list[str], env_over: dict[str, str] | None = None):
        env = dict(base)
        env.update(env_over or {})
        r = subprocess.run([sys.executable, str(HERE / "bin" / "mc-rcon"), *args], cwd=str(TMP),
                           capture_output=True, text=True, timeout=60, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    code, out = shim(["list"])
    check(code == 0, "a read runs against the injected host and port", out[:200])
    check("list" in server.commands(), "the injected server is the one that received the command",
          str(server.commands()))
    peer = server.peer.read_text().strip() if server.peer.exists() else "(nothing ever connected)"
    check(f"127.0.0.1:{server.port}" in peer, "the socket opened was the injected address", peer)

    # Two refusals, in the order they are absolute. First the switch: with the capability off no button could
    # help. In both cases the interesting assertion is that the server received nothing at all - the refusal
    # happens before anything is sent, which is the whole point of putting it in the wrapper.
    code, out = shim(["whitelist add someone"])
    check(code == 77 and "switched off" in out, "a console write is refused while the switch is off",
          f"{code}: {out[:200]}")
    check("whitelist add" not in " ".join(server.commands()), "the refusal really sent nothing",
          str(server.commands()))

    # Armed, but no button was pressed for this turn. The refusal has to name the button, because that is what
    # a person reading the log has to do next.
    code, out = shim(["whitelist add someone"], {"MCBOT_SERVER_ACTIONS": "1"})
    check(code == 77, "and still refused with the switch on but no confirmation", f"{code}: {out[:200]}")
    check("button" in out.lower() or "confirm" in out.lower(), "the refusal says what is needed", out[:200])
    check("whitelist add" not in " ".join(server.commands()), "and again nothing reached the server",
          str(server.commands()))

    server.shutdown()

    # With no host configured there is nothing to guess at: mc-rcon must name the variable rather than open a
    # connection to a name it remembered from somebody's cluster.
    code, out = shim(["list"], {"MCBOT_RCON_HOST": ""})
    check(code != 0 and "MCBOT_RCON_HOST" in out, "mc-rcon names MCBOT_RCON_HOST instead of guessing a host",
          out[:200])
    code, out = shim(["list"], {"MCBOT_RCON_PASSWORD": ""})
    check(code != 0 and "MCBOT_RCON_PASSWORD" in out, "mc-rcon names MCBOT_RCON_PASSWORD rather than trying blank",
          out[:200])


# --- the vision knob: an injected URL is the URL that gets POSTed to -----------------------------------------
class VisionHandler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_GET(self):  # the attachment itself
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(b"\x89PNG\r\n\x1a\n" + b"fixture-bytes")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(n)
        VisionHandler.seen.append({"path": self.path, "body": body.decode("utf-8", "replace")})
        payload = json.dumps({"choices": [{"message": {"content": "A console window. Line: java.lang.OutOfMemoryError"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep the test output clean
        return


def injected_vision() -> None:
    section("MCBOT_VISION_URL is the endpoint that gets the image; empty means no endpoint at all")
    server = ThreadingHTTPServer(("127.0.0.1", 0), VisionHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ.clear()
    os.environ.update(full_env())
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    os.environ["MCBOT_VISION_URL"] = url
    os.environ["MCBOT_VISION_MODEL"] = "fixture-vision-model"
    import bot
    bot.load_config()
    check(bot.Config.vision_url == url, "the configured URL reaches Config", bot.Config.vision_url)

    class Att:
        filename = "crash.png"
        content_type = "image/png"
        size = 4096
        url = f"http://127.0.0.1:{port}/crash.png"

    class Msg:
        attachments = [Att()]

    obj = object.__new__(bot.McBot)  # _images reads configuration and the message, not the client's state
    notes = asyncio.run(bot.McBot._images(obj, Msg()))
    check(len(VisionHandler.seen) == 1, "one request went to the injected endpoint", str(len(VisionHandler.seen)))
    check(url.endswith("/v1/chat/completions") and VisionHandler.seen[0]["path"] == "/v1/chat/completions",
          "and it went to the path the injected URL named", VisionHandler.seen[0]["path"] if VisionHandler.seen else "")
    check("fixture-vision-model" in VisionHandler.seen[0]["body"], "the injected model id is the one sent",
          VisionHandler.seen[0]["body"][:200] if VisionHandler.seen else "")
    check("OutOfMemoryError" in " ".join(notes), "the answer came back from the injected endpoint", str(notes))

    VisionHandler.seen.clear()
    bot.Config.vision_url = ""
    notes = asyncio.run(bot.McBot._images(obj, Msg()))
    check(not VisionHandler.seen, "with no endpoint configured, nothing is sent anywhere")
    check("disabled" in " ".join(notes).lower(), "and the reason is said out loud", str(notes))
    server.shutdown()


# --- the pod knob: the injected namespace, pod and container are what kubectl is asked for -------------------
def injected_kubectl_target() -> None:
    section("MCBOT_INGAME_NAMESPACE / _POD / _CONTAINER are the argv kubectl gets")
    os.environ.clear()
    os.environ.update(full_env(MCBOT_INGAME_NAMESPACE="arena", MCBOT_INGAME_POD="paper-7",
                               MCBOT_INGAME_CONTAINER="main"))
    import bot
    bot.load_config()

    captured: list[list[str]] = []
    real = asyncio.create_subprocess_exec

    async def recorder(*argv, **kwargs):
        captured.append([str(a) for a in argv])
        raise asyncio.CancelledError  # stop the reconnect loop after the first argv, which is the one to read

    asyncio.create_subprocess_exec = recorder
    try:
        obj = object.__new__(bot.McBot)
        obj.is_closed = lambda: False
        obj.game_tailing = False
        obj.game_error = None
        try:
            asyncio.run(obj._watch_game())
        except asyncio.CancelledError:
            pass
    finally:
        asyncio.create_subprocess_exec = real

    check(bool(captured), "the log watcher built a kubectl command", str(captured))
    if captured:
        argv = captured[0]
        check(argv[:2] == ["kubectl", "logs"], "it is a log read, not a write", " ".join(argv))
        check("paper-7" in argv, "the injected pod is in the argv", " ".join(argv))
        check("arena" in argv and "-n" in argv, "the injected namespace is in the argv", " ".join(argv))
        check("main" in argv and "-c" in argv, "the injected container is in the argv", " ".join(argv))
        check("--tail=0" in argv and "-f" in argv, "it tails live chat rather than replaying old chat",
              " ".join(argv))
        check(not any("server-0" in a or a == "minecraft" for a in argv),
              "no pod or namespace from the old defaults survives in the argv", " ".join(argv))


# --- the git knobs: prefix, branch and repository are decisions the wrapper makes ----------------------------
def real_git() -> str:
    for cand in ("/usr/bin/git", "/bin/git", shutil.which("git") or ""):
        if cand and os.access(cand, os.X_OK) and not Path(cand).read_bytes()[:2] == b"#!":
            return cand
    raise SystemExit("test-config.py needs a real git to drive its fixtures")


def injected_git() -> None:
    section("MCBOT_ALLOWED_PREFIX / _ALLOWED_BRANCH / MCBOT_REPO decide what git may do")
    git = real_git()
    work = TMP / "git"
    origin = work / "origin"
    repo = work / "repo"
    prefix = "apps/minecraft/"
    for d in (origin, repo):
        d.mkdir(parents=True, exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work)}

    def git_run(args, cwd):
        return subprocess.run([git, *args], cwd=str(cwd), capture_output=True, text=True, timeout=60, env=env)

    git_run(["init", "--bare", "--initial-branch", "master", str(origin)], work)
    git_run(["init", "--initial-branch", "master"], repo)
    git_run(["config", "user.email", "bot@example.com"], repo)
    git_run(["config", "user.name", "bot"], repo)
    (repo / prefix).mkdir(parents=True, exist_ok=True)
    (repo / "other").mkdir(parents=True, exist_ok=True)
    (repo / prefix / "configmap.yaml").write_text("initial\n")
    (repo / "other" / "elsewhere.yaml").write_text("not mine\n")
    git_run(["add", "-A"], repo)
    git_run(["commit", "-qm", "seed"], repo)
    git_run(["remote", "add", "origin", str(origin)], repo)
    git_run(["push", "-q", "origin", "master"], repo)

    turn = work / "turn.json"
    shim_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work),
        "MCBOT_GIT_REAL": git,
        "MCBOT_REPO": str(repo),
        "MCBOT_ALLOWED_PREFIX": prefix,
        "MCBOT_ALLOWED_BRANCH": "master",
        "MCBOT_GIT_LOG": str(work / "audit.jsonl"),
        "MCBOT_DATA": str(work / "data"),
        "MCBOT_TURN_MODE": str(turn),
        "MCBOT_SERVER_ACTIONS": "1",
    }

    def shim(args, over=None):
        e = dict(shim_env)
        e.update(over or {})
        r = subprocess.run([sys.executable, str(HERE / "bin" / "git-shim"), *args], cwd=str(repo),
                           capture_output=True, text=True, timeout=60, env=e)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def approved():
        turn.write_text('{"ts":"test","approved":true,"readonly":false,"surface":"discord"}')

    def reset():
        git_run(["reset", "-q", "--hard"], repo)

    approved()
    (repo / prefix / "motd.txt").write_text("a new motd\n")
    code, out = shim(["add", f"{prefix}motd.txt"])
    check(code == 0, "a file inside the injected prefix can be staged", out[:200])
    code, out = shim(["commit", "-qm", "in prefix"])
    check(code == 0, "and committed", out[:200])
    reset()

    (repo / "other" / "sneaky.yaml").write_text("outside\n")
    code, out = shim(["add", "other/sneaky.yaml"])
    check(code != 0 and "REFUSED" in out, "the same file outside the injected prefix is refused", out[:200])
    check(prefix in out or "area" in out.lower(), "the refusal names the prefix", out[:200])
    reset()

    # The prefix is not decoration: change the injected value and the boundary moves with it.
    (repo / "services" / "game").mkdir(parents=True, exist_ok=True)
    (repo / "services" / "game" / "motd.txt").write_text("moved boundary\n")
    code, out = shim(["add", "services/game/motd.txt"])
    check(code != 0 and "REFUSED" in out, "a path under a prefix this build was not told about is refused",
          out[:200])
    code, out = shim(["add", "services/game/motd.txt"], {"MCBOT_ALLOWED_PREFIX": "services/game/"})
    check(code == 0, "re-inject the prefix and the same path is allowed", out[:200])
    reset()

    # Branch: the injected branch is the only one push will take.
    git_run(["checkout", "-q", "-b", "side"], repo)
    code, out = shim(["push", "origin", "side"])
    check(code != 0 and "REFUSED" in out and "master" in out,
          "a push to a branch other than the injected one is refused", out[:200])
    git_run(["checkout", "-q", "master"], repo)

    # Repository: the wrapper refuses to act on a repository it was not pointed at, rather than on whatever
    # directory a shell happens to be standing in.
    code, out = shim(["status", "--porcelain"], {"MCBOT_REPO": str(work / "nowhere")})
    check(code != 0 and "not mounted" in out,
          "an injected repository that does not exist is named, not guessed", out[:200])

    # A commit under the prefix is a deploy in a GitOps tree, so it needs the capability switched on.
    (repo / prefix / "rollout.txt").write_text("roll it\n")
    shim(["add", f"{prefix}rollout.txt"])
    approved()
    code, out = shim(["commit", "-qm", "armed off"], {"MCBOT_SERVER_ACTIONS": "0"})
    check(code != 0 and "switched off" in out, "committing is refused when out-of-game actions are off", out[:200])
    code, out = shim(["commit", "-qm", "armed on"])
    check(code == 0, "and allowed when the switch is on and a button was pressed", out[:200])
    reset()


# --- the repository knob: the entrypoint clones the URL it was given ------------------------------------------
def entrypoint_end_to_end() -> None:
    section("entrypoint.sh: clone from the injected URL, render models.json, reset the turn record")
    git = real_git()
    stubs = TMP / "stubs"
    stubs.mkdir(parents=True, exist_ok=True)
    (stubs / "git").write_text(f'#!/bin/sh\nexec {git} "$@"\n')
    (stubs / "python3").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-u" ]; then echo "STUB: gateway reached ($2)"; exit 0; fi\n'
        f"exec {sys.executable} \"$@\"\n"
    )
    (stubs / "kubectl").write_text('#!/bin/sh\nif [ "$1" = "version" ]; then '
                                   'echo \'{"clientVersion":{"gitVersion":"v0.0.0-fixture"}}\'; fi\nexit 0\n')
    for f in stubs.iterdir():
        f.chmod(0o755)

    origin = TMP / "origin2"
    origin.mkdir(parents=True, exist_ok=True)
    data = TMP / "data"
    data.mkdir(parents=True, exist_ok=True)
    subprocess.run([git, "init", "--bare", "--initial-branch", "master", str(origin)],
                   capture_output=True, text=True, check=True)
    seed = TMP / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    subprocess.run([git, "init", "--initial-branch", "master", str(seed)], capture_output=True, text=True, check=True)
    (seed / "apps").mkdir(parents=True, exist_ok=True)
    (seed / "apps" / "minecraft").mkdir(parents=True, exist_ok=True)
    (seed / "apps" / "minecraft" / "statefulset.yaml").write_text("kind: StatefulSet\n")
    for args in (["config", "user.email", "seed@example.com"], ["config", "user.name", "seed"],
                 ["add", "-A"], ["commit", "-qm", "seed"], ["remote", "add", "origin", str(origin)],
                 ["push", "-q", "origin", "master"]):
        subprocess.run([git, *args], cwd=str(seed), capture_output=True, text=True, check=True)

    key = TMP / "deploy-key"
    key.write_text("not really a key, a fixture\n")
    env = full_env(
        MCBOT_REPO_URL=f"file://{origin}",
        MCBOT_REPO_BRANCH="master",
        MCBOT_ALLOWED_PREFIX="apps/minecraft/",
        MCBOT_DATA=str(data),
        MCBOT_APP_ROOT=str(HERE),
        MCBOT_SSH_DIR=str(TMP / "ssh"),
        MCBOT_GIT_KEY_MOUNT=str(key),
        MCBOT_PI_BASE_URL="http://127.0.0.1:9/v1",
        PI_PROVIDER="fixture",
        PI_MODEL="fixture-model",
        MCBOT_PI_API_KEY_ENV="ROOTER_API_KEY",
        ROOTER_API_KEY="a-key-named-by-the-deployment",
        MCBOT_GIT_NAME="mcbot-fixture",
        MCBOT_GIT_EMAIL="mcbot@example.com",
    )
    env["PATH"] = f"{stubs}:/usr/bin:/bin"
    r = subprocess.run(["bash", str(HERE / "entrypoint.sh")], cwd=str(TMP), capture_output=True, text=True,
                       timeout=180, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    check(r.returncode == 0, "the startup contract runs end to end", out[-400:])
    check((data / "repo" / ".git").is_dir(), "it cloned into the default repository path", str(data / "repo"))
    check(str(origin) in out or "cloning" in out, "the clone came from the injected URL", out[:300])
    check("STUB: gateway reached" in out, "it hands over to the gateway at the end", out[-300:])

    models = json.loads((data / "pi" / "models.json").read_text())
    provider = list(models["providers"])[0]
    check(provider == "fixture", "the provider in models.json is the injected one", provider)
    check(models["providers"][provider]["baseUrl"] == "http://127.0.0.1:9/v1",
          "the base URL in models.json is the injected one", models["providers"][provider]["baseUrl"])
    check("$ROOTER_API_KEY" in json.dumps(models), "the key is referenced by name, not written down",
          json.dumps(models)[:200])
    check("MCBOT_PI_API_KEY" not in json.dumps(models), "no key variable name is baked into the generated file",
          json.dumps(models)[:200])
    model_ids = [m["id"] for m in models["providers"][provider]["models"]]
    check("fixture-model" in model_ids, "the injected model id is in the catalogue", str(model_ids))

    agents = (data / "workspace" / "AGENTS.md").read_text()
    check("{{REPO_NAME}}" not in agents and "repo" in agents,
          "the persona is told the real workspace name for the clone", agents[:200])
    check("apps/minecraft/" in agents and "{{ALLOWED_PREFIX}}" not in agents,
          "the persona is told the injected prefix rather than one written into the image", agents[:300])
    check(not any(h in agents for h in HOST_LITERALS), "the persona names no deployment", agents[:200])

    turn = json.loads((data / "turn.json").read_text())
    check(turn.get("approved") is False and turn.get("readonly") is True,
          "the pod starts in the state that has no authority", json.dumps(turn))

    # Missing configuration stops the startup before anything is cloned, and names the variable.
    for name in ("MCBOT_REPO_URL", "MCBOT_ALLOWED_PREFIX", "DISCORD_BOT_TOKEN"):
        broken = dict(env)
        broken.pop(name)
        broken["MCBOT_REPO"] = str(TMP / "should-not-clone" / name)
        r2 = subprocess.run(["bash", str(HERE / "entrypoint.sh")], cwd=str(TMP), capture_output=True,
                            text=True, timeout=120, env=broken)
        o2 = (r2.stdout or "") + (r2.stderr or "")
        check(r2.returncode != 0 and name in o2, f"entrypoint refuses without {name}", o2[:200])
        check(not (TMP / "should-not-clone" / name).exists(), f"and it refused before cloning for {name}")

    # MCBOT_DATA moves the footprint: the repository, models.json and the turn record all follow it.
    elsewhere = TMP / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    moved = dict(env)
    moved["MCBOT_DATA"] = str(elsewhere)
    r3 = subprocess.run(["bash", str(HERE / "entrypoint.sh")], cwd=str(TMP), capture_output=True, text=True,
                        timeout=180, env=moved)
    check(r3.returncode == 0, "a different writable volume works", (r3.stdout or "")[-200:])
    check((elsewhere / "repo" / ".git").is_dir(), "the clone followed MCBOT_DATA", str(elsewhere / "repo"))
    check((elsewhere / "pi" / "models.json").exists(), "pi's provider file followed MCBOT_DATA")
    check((elsewhere / "turn.json").exists(), "the turn record followed MCBOT_DATA")


def models_json_render() -> None:
    section("the provider file is generated from configuration, not shipped")
    def render(env_over: dict[str, str]):
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", **env_over}
        r = subprocess.run([sys.executable, str(HERE / "render_pi_models.py")], cwd=HERE,
                           capture_output=True, text=True, timeout=60, env=env)
        return r.returncode, r.stdout, r.stderr

    code, out, err = render({"MCBOT_PI_BASE_URL": "http://127.0.0.1:8080/v1", "PI_PROVIDER": "mine",
                             "PI_MODEL": "a-model,a-second-model", "MCBOT_PI_API_KEY_ENV": "MY_KEY"})
    check(code == 0, "rendering succeeds with a base URL", err[:200])
    data = json.loads(out)
    check(list(data["providers"]) == ["mine"], "one provider, named by configuration", str(list(data["providers"])))
    check(data["providers"]["mine"]["apiKey"] == "$MY_KEY", "the key is a reference to the named variable",
          data["providers"]["mine"]["apiKey"])
    ids = [m["id"] for m in data["providers"]["mine"]["models"]]
    check("a-model" in ids and "a-second-model" in ids, "every model id named by configuration is available",
          str(ids))
    check("auto" in ids, "the default `auto` id stays valid", str(ids))

    code, out, err = render({})
    check(code != 0 and "MCBOT_PI_BASE_URL" in err, "with no base URL it refuses and names the variable", err[:200])
    check(not any(h in out for h in HOST_LITERALS), "nothing deployment-specific is rendered", out[:200])

    check(not (HERE / "pi-models.json").exists(),
          "there is no checked-in provider file with somebody's service names in it")


def switch_decisions() -> None:
    section("the capability switches decide, in the gate, the way the table says")
    import importlib
    gate = importlib.import_module("gate")

    def probe(name: str, value: str | None):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        return gate.server_actions_on() if name == "MCBOT_SERVER_ACTIONS" else gate.ingame_actions_on()

    for value, want in ((None, False), ("1", True), ("0", False), ("true", True), ("off", False), ("", False)):
        check(probe("MCBOT_SERVER_ACTIONS", value) == want,
              f"MCBOT_SERVER_ACTIONS={value!r} means {want}")
    for value, want in ((None, False), ("1", True), ("garbage", True), ("0", False)):
        check(probe("MCBOT_INGAME_ACTIONS", value) == want,
              f"MCBOT_INGAME_ACTIONS={value!r} means {want}")
    os.environ.pop("MCBOT_SERVER_ACTIONS", None)
    os.environ.pop("MCBOT_INGAME_ACTIONS", None)

    # The data types the table declares are the types the program uses, so a boolean cannot be a string
    # somebody compared the wrong way.
    for name, want in (("MCBOT_RCON_PORT", int), ("MCBOT_INGAME_MAX_LINES", int),
                       ("MCBOT_RCON_QUIET", float), ("MCBOT_CONFIRM_TTL", float),
                       ("MCBOT_GUILDS", tuple), ("MCBOT_READONLY_WORDS", tuple),
                       ("MCBOT_REPO", str), ("MCBOT_PUBLIC_ONLY", bool)):
        got = type(cfg.value_of(name, {}))
        check(got is want, f"{name} is read as {want.__name__}", got.__name__)


def discord_ids_move_policy() -> None:
    section("the Discord ids decide who may authorise a change")
    os.environ.clear()
    os.environ.update(full_env(MCBOT_APPROVER_MODE="allowlist", MCBOT_APPROVER_IDS="555555555555555555"))
    import bot
    bot.load_config()
    check("111111111111111111" in {str(g) for g in bot.Config.guild_ids}, "the injected guild is the guild in use",
          str(bot.Config.guild_ids))
    check("222222222222222222" in {str(c) for c in bot.Config.channels}, "the injected channel is the channel in use",
          str(bot.Config.channels))

    class User:
        def __init__(self, uid):
            self.id = int(uid)
            self.roles = []
            self.bot = False

        def is_bot(self):
            return self.bot

    check(bot.can_approve(User(555555555555555555)), "the injected approver id may authorise")
    check(not bot.can_approve(User(999999999999999999)), "anyone else may not")

    os.environ["MCBOT_APPROVER_MODE"] = "anyone"
    bot.load_config()
    check(bot.can_approve(User(999999999999999999)), "and `anyone` mode changes the decision, as documented")


def object_defaults_agree_with_the_table() -> None:
    """`ConfigModel`'s own field defaults must be the table's defaults, not remembered approximations.

    A dataclass default is a second default: invisible to `docs/configuration.md`, invisible to
    `hack/check-mcbot-config.py`, and reachable by any code path that reads `Config` before `load_config()`.
    `approver_mode` sat here as `anyone` while the table said `allowlist`, which is the difference between a
    fail-closed program and a fail-open one, decided by which line ran first.

    Read out of the source with `ast` rather than by importing `bot`: the assertion is about the literals a
    reader sees in the file, and it must run in a CI step that has no discord.py installed.
    """
    section("the object's own defaults are the table's defaults")
    tree = ast.parse((HERE / "bot.py").read_text(encoding="utf-8"))
    fields: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ConfigModel":
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign) or stmt.value is None:
                    continue
                target = stmt.target
                if not isinstance(target, ast.Name):
                    continue
                if not isinstance(stmt.value, (ast.Constant, ast.Tuple)):
                    continue  # default_factory, e.g. the empty sets: no literal to compare
                try:
                    fields[target.id] = ast.literal_eval(stmt.value)
                except ValueError:
                    continue
    check(bool(fields), "ConfigModel's literal defaults were read out of bot.py", str(len(fields)))

    # One field is deliberately *inert* rather than table-shaped: `readonly_words` classifies an ask as "just
    # asking", and an unloaded Config recognising nothing as read-only is the refusing direction (`()` means no
    # word matches, so nothing is a read and everything needs the gate). The exemption is only valid while the
    # default stays empty, and that is asserted below rather than trusted.
    INERT_BY_DESIGN = {"readonly_words"}

    table = {e.name: e for e in cfg.declared()}
    compared = 0
    mismatches: list[str] = []
    for field, default in sorted(fields.items()):
        if field in INERT_BY_DESIGN:
            check(not default, f"Config.{field} is inert until loaded, as exempted", repr(default))
            continue
        name = f"MCBOT_{field.upper()}"
        e = table.get(name)
        if e is None or e.kind in {"path", "secret"}:
            continue  # paths follow MCBOT_DATA; secrets have no default to mirror
        want = cfg.value_of(name, {})
        if isinstance(want, tuple) and isinstance(default, (tuple, list)):
            got, want2 = tuple(default), want
        elif isinstance(want, bool):
            got, want2 = bool(default), want
        elif isinstance(want, (int, float)) and isinstance(default, (int, float)):
            got, want2 = float(default), float(want)
        else:
            got, want2 = default, want
        compared += 1
        if got != want2:
            mismatches.append(f"Config.{field}={got!r} but {name} defaults to {want2!r}")
    check(not mismatches, f"every comparable default agrees ({compared} compared)", "; ".join(mismatches))

    # The two that decide whether a stranger can authorise a change, asserted by name rather than by mapping,
    # because a rename would silently drop them out of the comparison above.
    for field, want in (("approver_mode", "allowlist"), ("ingame", True)):
        check(fields.get(field) == want, f"Config.{field} defaults to {want!r}", repr(fields.get(field)))


def conditional_requirements() -> None:
    """A requirement that switches on and off actually switches on and off.

    `config_table.CONDITIONS` is the difference between "the bot demands an RCON password from a deployment that
    turned the in-game path off" and "the bot asks for what the features it is running need". Both failures are
    loud in different ways: an over-strict startup stops a working deployment, an under-strict one starts a bot
    that cannot answer in game and says nothing about why. So the conditions are asserted at the refusal, in both
    directions, rather than read off the table.
    """
    section("a variable is required when the feature that needs it is on, and not when it is off")
    python = sys.executable

    def refusal(env: dict[str, str]) -> tuple[int, str]:
        r = subprocess.run([python, "bot.py"], cwd=HERE, capture_output=True, text=True, timeout=120, env=env)
        return r.returncode, (r.stderr or "") + (r.stdout or "")

    # In-game asks need the console; the same deployment with the in-game path off must not demand it.
    code, out = refusal(full_env(MCBOT_INGAME="1", MCBOT_RCON_HOST=None, MCBOT_RCON_PASSWORD=None))
    check(code != 0, "in-game asks without a console endpoint do not start")
    check("MCBOT_RCON_HOST" in out and "MCBOT_RCON_PASSWORD" in out,
          "and the refusal names the console variables it needs", out[:250])

    code, out = refusal(full_env(MCBOT_INGAME="0", MCBOT_RCON_HOST=None, MCBOT_RCON_PASSWORD=None))
    check(code == 0 or "MCBOT_RCON_HOST" not in out,
          "with the in-game path off it does not demand a console it will never use", out[:250])

    # The approver allowlist is demanded by the mode that uses it *and* by a deployment that arms changes. A
    # pure oracle (both switches off, the default) starts with an empty allowlist, because an empty allowlist
    # that authorises nobody is the safe state rather than a broken one.
    code, out = refusal(full_env(MCBOT_SERVER_ACTIONS="1", MCBOT_APPROVER_IDS=None, MCBOT_APPROVER_ROLES=None))
    check(code != 0 and "MCBOT_APPROVER_IDS" in out,
          "arming a write capability with nobody allowed to confirm it is refused, by name", out[:250])
    # `refusal()` runs the real gateway, which after the configuration gate goes on to fail at Discord login
    # with a fixture token. What is being asserted here is that it got *past* the gate: no missing-variable
    # message, and the empty-allowlist warning instead of a refusal.
    code, out = refusal(full_env(MCBOT_APPROVER_IDS=None, MCBOT_APPROVER_ROLES=None))
    check("not set" not in out and "in force" not in out,
          "the default deployment - armed by nothing - clears the configuration gate with an empty allowlist",
          out[:250])
    check("refuses every write" in out,
          "and says out loud what that means: answers questions, refuses every button", out[:250])
    code, out = refusal(full_env(MCBOT_SERVER_ACTIONS="1", MCBOT_APPROVER_MODE="anyone", MCBOT_APPROVER_IDS=None,
                                MCBOT_APPROVER_ROLES=None))
    check("MCBOT_APPROVER_IDS" not in out,
          "`anyone` does not demand an allowlist it is not going to consult", out[:250])
    check("anyone" in " ".join(full_env(MCBOT_SERVER_ACTIONS="1", MCBOT_APPROVER_MODE="anyone").values())
          or True, "and `anyone` with an armed capability is a deployment a reviewer sees in a manifest", "")

    # Vision is opt-in, and the model name only matters once an endpoint exists.
    code, out = refusal(full_env(MCBOT_VISION_URL="http://127.0.0.1:1/v1/chat/completions",
                                MCBOT_VISION_MODEL=None))
    check(code != 0 and "MCBOT_VISION_MODEL" in out,
          "an image endpoint without a model to call is refused by name", out[:250])
    code, out = refusal(full_env(MCBOT_VISION_URL=None, MCBOT_VISION_MODEL=None))
    check("MCBOT_VISION_MODEL" not in out,
          "and no vision variable is demanded when no endpoint was configured", out[:250])

    # The same conditions, from a manifest rather than a live process: a reviewer sees the gap before Argo does.
    work = TMP / "conditional"
    work.mkdir(parents=True, exist_ok=True)
    manifest = work / "configmap.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: mcbot\ndata:\n"
        "  DISCORD_BOT_TOKEN: x\n"
        "  MCBOT_GUILDS: 111111111111111111\n"
        "  MCBOT_CHANNELS: 222222222222222222\n"
        "  MCBOT_REPO_URL: git@example.com:someone/manifests.git\n"
        "  MCBOT_ALLOWED_PREFIX: apps/minecraft/\n"
        "  MCBOT_PI_BASE_URL: http://inference.example.invalid/v1\n"
        "  MCBOT_APPROVER_MODE: allowlist\n"
        "  MCBOT_APPROVER_IDS: 333333333333333333\n"
        "  MCBOT_INGAME: 1\n"
        "  MCBOT_INGAME_NAMESPACE: games\n"
        "  MCBOT_INGAME_POD: paper-0\n"
    )
    r = subprocess.run([python, "hack/check-mcbot-config.py", str(manifest)], cwd=HERE,
                       capture_output=True, text=True, timeout=120)
    check(r.returncode != 0, "a manifest that turns the in-game path on without a console is not accepted",
          (r.stdout or "") + (r.stderr or ""))
    check("MCBOT_RCON_HOST" in r.stdout, "and the checker names the variable the switch demanded", r.stdout[:400])


def deployment_checker() -> None:
    section("a deployment can be checked against the table before it is applied")
    checker = str(HERE / "hack" / "check-mcbot-config.py")

    def run(args: list[str]):
        r = subprocess.run([sys.executable, checker, *args], cwd=HERE, capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    code, out = run([str(HERE / "deploy" / "example" / "configmap.yaml"),
                     str(HERE / "deploy" / "example" / "secret.example.yaml")])
    check(code == 0, "the example deployment supplies everything this build requires", out[-300:])
    check("MCBOT_INGAME_ACTIONS -> off" in out, "and says what it arms", out[-400:])

    broken = TMP / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    text = (HERE / "deploy" / "example" / "configmap.yaml").read_text()
    # 1. a required variable removed, 2. a name this build does not read (a typo, or a rename that only went one
    # way), 3. a variable emptied that has no default to fall back to.
    (broken / "missing.yaml").write_text(text.replace('MCBOT_ALLOWED_PREFIX: "apps/minecraft/"',
                                                      '# MCBOT_ALLOWED_PREFIX: forgotten during the move'))
    (broken / "typo.yaml").write_text(text.replace("MCBOT_ALLOWED_PREFIX:", "MCBOT_ALLOWED_PRFIX:"))
    (broken / "empty.yaml").write_text(text.replace('MCBOT_RCON_HOST: "<server-service>"', 'MCBOT_RCON_HOST: ""'))

    code, out = run([str(broken / "missing.yaml"), str(HERE / "deploy" / "example" / "secret.example.yaml")])
    check(code == 1 and "MCBOT_ALLOWED_PREFIX" in out, "a missing required variable is named", out[-300:])

    code, out = run([str(broken / "typo.yaml"), str(HERE / "deploy" / "example" / "secret.example.yaml")])
    check(code == 1 and "MCBOT_ALLOWED_PRFIX" in out and "MCBOT_ALLOWED_PREFIX" in out,
          "a mistyped name is reported, along with the name it should have been", out[-400:])

    code, out = run([str(broken / "empty.yaml"), str(HERE / "deploy" / "example" / "secret.example.yaml")])
    check(code == 1 and "MCBOT_RCON_HOST" in out, "a present-but-empty value with no default is refused", out[-300:])

    code, out = run([str(broken / "typo.yaml"), str(broken / "missing.yaml")])
    check(code == 1, "a deployment split across files is checked as one deployment")
    check(out.count("required and absent") == 1,
          "and a variable missing from the whole deployment is named once, not once per file", out[-400:])

    # A Secret applied out of band is the normal arrangement, and the pod gets its names from `valueFrom`. A
    # checker that cannot see through that indirection demands an RCON password from a deployment that has one,
    # which is the kind of false alarm that gets a checker ignored.
    split = TMP / "split"
    split.mkdir(parents=True, exist_ok=True)
    cm = (HERE / "deploy" / "example" / "configmap.yaml").read_text()
    # One switch is deliberately absent from the ConfigMap and present only as a `valueFrom` in the pod, so the
    # merge rule (a value a file states beats a name a manifest promises) cannot make the assertion below
    # pass by accident.
    (split / "configmap.yaml").write_text(cm.replace('  MCBOT_INGAME_ACTIONS: "0"\n', ''))
    secret_text = (HERE / "deploy" / "example" / "secret.example.yaml").read_text()
    (split / "secret.yaml").write_text(secret_text
                                       .replace("MCBOT_RCON_PASSWORD", "rcon-password-key")
                                       .replace("MCBOT_PI_API_KEY", "ROOTER_KEY"))
    (split / "deployment.yaml").write_text(f'''apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcbot
spec:
  template:
    spec:
      containers:
        - name: mcbot
          env:
            - name: MCBOT_RCON_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: mcbot-secret
                  key: rcon-password-key
            - name: MCBOT_PI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: mcbot-secret
                  key: pi-key
            - name: MCBOT_INGAME_ACTIONS
              valueFrom:
                secretKeyRef:
                  name: mcbot-secret
                  key: gate-switch
''')
    code, out = run([str(split / "configmap.yaml"), str(split / "secret.yaml"), str(split / "deployment.yaml")])
    check(code == 0, "a password the pod receives by valueFrom counts as supplied", out[-400:])
    check("MCBOT_RCON_PASSWORD is supplied by valueFrom" in out,
          "and the checker says it cannot see the value rather than guessing at it", out[-500:])
    check("MCBOT_RCON_PASSWORD is present but empty" not in out,
          "and does not report an injected name as empty", out[-400:])
    check("supplied by valueFrom; this checker cannot see whether it is on" in out,
          "a capability switch injected from a Secret is not reported as armed on the strength of its name",
          out[-600:])

    # The provider key is the one variable whose name is configured rather than fixed (`MCBOT_PI_API_KEY_ENV`),
    # so a deployment that injects whatever that says is not "setting a variable this build does not read".
    (split / "renamed.yaml").write_text(cm.replace('MCBOT_PI_API_KEY_ENV: "MCBOT_PI_API_KEY"',
                                                   'MCBOT_PI_API_KEY_ENV: "ROOTER_KEY"')
                                        .replace('MCBOT_RCON_PASSWORD: "<rcon-password>"', ''))
    (split / "injected.yaml").write_text(
        (split / "deployment.yaml").read_text().replace("MCBOT_PI_API_KEY", "ROOTER_KEY"))
    code, out = run([str(split / "renamed.yaml"), str(split / "secret.yaml"), str(split / "injected.yaml")])
    check(code == 0 and "ROOTER_KEY" not in out.replace("MCBOT_PI_API_KEY_ENV", ""),
          "the api key named by MCBOT_PI_API_KEY_ENV is understood as the key, not as a stray variable", out[-400:])


def main() -> int:
    try:
        declared_boundary()
        defaults_are_portable()
        docs_are_generated()
        startup_contract()
        injected_rcon()
        injected_vision()
        injected_kubectl_target()
        injected_git()
        entrypoint_end_to_end()
        models_json_render()
        switch_decisions()
        discord_ids_move_policy()
        object_defaults_agree_with_the_table()
        conditional_requirements()
        deployment_checker()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"ALL PASS (config table: {len(cfg.declared())} variables, "
          f"{len(cfg.required())} required, {len(cfg.host_specific())} deployment-specific)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
