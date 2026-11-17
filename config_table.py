"""Every environment variable mcbot reads, declared in one place.

Why this file exists: mcbot is a program that only works in someone else's cluster. Almost everything that made
it work in *our* cluster used to be a literal in `bot.py` - the RCON host, the namespace and pod to tail, the
repository to clone, the path it may commit to, the vision endpoint, the inference base URL. Those are not
defaults, they are one deployment wearing the costume of defaults, and a public repository cannot ship them:
someone who sets the Discord token up gets a bot pointed at somebody else's infrastructure, or at nothing, with
no message saying which.

So each variable is declared here with its kind, its default (empty string means "no default"), whether it is
required, and which part of the program it belongs to. Three things are then derived from that rather than
remembered:

- `bot.py`'s startup check uses `required()`, so "required" is one list instead of one the code half-remembers.
- `docs/configuration.md` is rendered from the table, so the documentation cannot drift from the code.
  `test-config.py` fails if the checked-in file is not what `render_markdown()` produces today.
- `test-config.py` walks the shipped sources for `os.environ[...]` reads and fails on any variable that is not
  in this table. Undeclared config is the quiet failure mode of a parametrization pass: a rename leaves one
  reader falling back to a default nobody noticed, and a typo in a ConfigMap looks like a working setting.

Required means: without it the program cannot know what it is pointed at, and guessing is worse than stopping.
Conditions matter - the RCON host is required when in-game asks are on, and the approver allowlist is required
when the approver mode is an allowlist. `hack/check-mcbot-config.py` compares a live
ConfigMap against this table, which is the difference between "the deployment worked yesterday" and "the
deployment still supplies everything this build asks for".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# `host_specific` marks a value that describes one particular deployment. Those must have no default here:
# test-config.py asserts none of them smuggle a hostname, a path, a Discord snowflake or a person's name into
# a published repository.
GROUPS = (
    "discord",
    "repository",
    "capabilities",
    "in-game",
    "inference",
    "answers",
    "runtime",
)


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # str | int | float | bool | list | path | secret
    group: str
    purpose: str
    default: str = ""  # empty string means there is no default
    required: bool = False
    # A requirement that only applies when another setting has a particular value. Rendered as a sentence, and
    # enforced in bot.py rather than pretended away with a default.
    when: str = ""
    host_specific: bool = False
    secret: bool = False
    # Read by something outside this repository - pi expands `$MCBOT_PI_API_KEY` in the generated models.json,
    # and the entrypoint only validates the variable named by MCBOT_PI_API_KEY_ENV. Declared so the deployment
    # checker knows about it; exempt from "every declared variable is read by our code".
    external_reader: bool = False


TABLE: list[Entry] = [
    # --- discord: who mcbot can see, and where it is allowed to speak -------------------------------
    Entry("DISCORD_BOT_TOKEN", "secret", "discord", "the bot token; nothing works without it",
          required=True, secret=True),
    Entry("MCBOT_GUILDS", "list", "discord", "guild ids mcbot may act in, comma separated",
          required=True, host_specific=True),
    Entry("MCBOT_CHANNELS", "list", "discord", "channel ids mcbot may answer in, comma separated",
          required=True, host_specific=True),
    # The singular form predates the list and still satisfies the guild requirement, so an installation written
    # before this file exists is not broken by it. Declared rather than invisible to the undeclared-variable scan.
    Entry("MCBOT_GUILD_ID", "str", "discord", "legacy single guild id; MCBOT_GUILDS is the list", default=""),
    Entry("MCBOT_APPROVER_MODE", "str", "discord", "`allowlist` or `anyone`; who may press a confirmation button",
          default="allowlist"),
    Entry("MCBOT_APPROVER_IDS", "list", "discord", "user ids allowed to confirm, when the mode is `allowlist`",
          when="MCBOT_APPROVER_MODE=allowlist and a capability switch is on", host_specific=True),
    Entry("MCBOT_APPROVER_ROLES", "list", "discord", "role ids allowed to confirm, when the mode is `allowlist`",
          when="MCBOT_APPROVER_MODE=allowlist and a capability switch is on"),

    # --- repository: what it clones, where it may write, and what a commit means ---------------------
    Entry("MCBOT_REPO_URL", "str", "repository",
          "the repository it clones for its git work. A commit inside MCBOT_ALLOWED_PREFIX is a deploy in a GitOps tree",
          required=True, host_specific=True),
    Entry("MCBOT_REPO_BRANCH", "str", "repository", "branch it checks out and may push", default="master"),
    Entry("MCBOT_REPO", "path", "repository", "where that clone lives on disk", default="/data/repo"),
    Entry("MCBOT_GIT_NAME", "str", "repository", "commit author name, so a commit is attributable to the bot",
          default="mcbot"),
    Entry("MCBOT_GIT_EMAIL", "str", "repository", "commit author email", default="mcbot@localhost"),
    Entry("MCBOT_ALLOWED_PREFIX", "str", "repository",
          "the only path prefix it may commit or push; outside it, git-shim refuses",
          required=True, host_specific=True),
    Entry("MCBOT_ALLOWED_BRANCH", "str", "repository", "the only branch it may push", default="master"),
    Entry("MCBOT_GIT_KEY_MOUNT", "path", "repository",
          "the private key file the entrypoint copies into MCBOT_SSH_DIR before ssh will accept it",
          default="/mnt/git-key/id_ed25519"),
    Entry("MCBOT_GIT_REAL", "path", "repository", "the real git binary the wrapper execs", default="/usr/bin/git"),
    Entry("MCBOT_GIT_LOG", "path", "repository", "append-only log of git decisions the wrapper made",
          default="/data/audit/git.jsonl"),
    Entry("MCBOT_PLUGIN_ALLOWLIST", "list", "repository",
          "plugin jar slugs mcbot may add. Empty, which is the default, means none: an unreadable binary with op permissions",
          default=""),

    # --- capabilities: what it is capable of at all, separate from who may ask -----------------------
    Entry("MCBOT_SERVER_ACTIONS", "bool", "capabilities",
          "out-of-game changes (config, manifests, rollouts). Fail-closed: off unless the deployment says on",
          default="0"),
    Entry("MCBOT_INGAME_ACTIONS", "bool", "capabilities",
          "changes to the world and the players in it. Off by default and not something a button can turn on",
          default="0"),
    Entry("MCBOT_APPROVER_MODE_NOTE", "str", "capabilities", "", default="", when="see discord group"),
    Entry("MCBOT_TURN_MODE", "path", "capabilities",
          "the turn record the wrappers read before execing anything; reset to read-only at startup",
          default="/data/turn.json"),
    Entry("MCBOT_AUDIT", "path", "capabilities", "append-only record of every gate decision",
          default="/data/audit/gate.jsonl"),
    Entry("MCBOT_KUBECTL_REAL", "path", "capabilities", "the real kubectl the wrapper execs",
          default="/usr/bin/kubectl"),

    # --- in-game: the chat watcher and the RCON client it answers with -------------------------------
    Entry("MCBOT_INGAME", "bool", "in-game", "turn the in-game ask path on at all", default="1"),
    Entry("MCBOT_INGAME_TRIGGERS", "list", "in-game", "prefixes that make a chat line an ask",
          default="!mcbot,@mcbot"),
    Entry("MCBOT_INGAME_NAMESPACE", "str", "in-game", "namespace the server pod lives in",
          when="MCBOT_INGAME is on, which it is by default", host_specific=True),
    Entry("MCBOT_INGAME_POD", "str", "in-game", "pod whose log is read as the chat source",
          when="MCBOT_INGAME is on, which it is by default", host_specific=True),
    Entry("MCBOT_INGAME_CONTAINER", "str", "in-game", "container in that pod, named so a sidecar cannot interleave logs",
          default="server"),
    Entry("MCBOT_RCON_HOST", "str", "in-game", "host of the server console",
          when="MCBOT_INGAME is on, which it is by default", host_specific=True),
    Entry("MCBOT_RCON_PORT", "int", "in-game", "RCON port", default="25575"),
    Entry("MCBOT_RCON_PASSWORD", "secret", "in-game", "RCON password",
          when="MCBOT_INGAME is on, which it is by default", secret=True),
    Entry("MCBOT_RCON_QUIET", "float", "in-game",
          "seconds of silence that ends a response. Paper 1.21.11 closes the connection on an empty flush packet",
          default="0.75"),
    Entry("MCBOT_INGAME_COOLDOWN", "float", "in-game", "seconds between answers to the same player", default="10"),
    Entry("MCBOT_INGAME_MAX_LEN", "int", "in-game", "characters an in-game answer may run to", default="400"),
    Entry("MCBOT_INGAME_LINE_LEN", "int", "in-game", "characters per broadcast line, because Minecraft does not wrap",
          default="110"),
    Entry("MCBOT_INGAME_MAX_LINES", "int", "in-game", "lines per in-game answer", default="6"),
    Entry("MCBOT_LIB", "path", "in-game", "where rcon.py lives, so mc-rcon can import it", default="/opt/mcbot"),

    # --- inference: the model it thinks with and the vision model it reads images with ---------------
    Entry("PI_PROVIDER", "str", "inference",
          "provider name passed to pi, and the key written into the generated models.json", default="local"),
    Entry("PI_MODEL", "list", "inference", "model id(s) passed to pi; the first is the default",
          default="auto"),
    Entry("MCBOT_PI_BASE_URL", "str", "inference",
          "OpenAI-compatible base URL written into the generated models.json. No default: this is always somebody's endpoint",
          required=True, host_specific=True),
    Entry("MCBOT_PI_API_KEY_ENV", "str", "inference",
          "the name of the environment variable holding that provider's key", default="MCBOT_PI_API_KEY"),
    Entry("MCBOT_PI_API_KEY", "secret", "inference", "the provider key, unless another name is injected",
          secret=True, external_reader=True),
    Entry("MCBOT_PI_FRESH_SESSION", "bool", "inference", "start a fresh pi session for every ask", default="1"),
    # Properties of the model rather than of the deployment, so these have defaults: a context window and a
    # completion cap that a llama.cpp server of nearly any shape will accept, and no more.
    Entry("MCBOT_PI_CONTEXT_WINDOW", "int", "inference", "tokens the model will hold at once", default="131072"),
    Entry("MCBOT_PI_MAX_TOKENS", "int", "inference", "tokens one completion may produce", default="16384"),
    Entry("MCBOT_PI_REASONING", "bool", "inference", "offer thinking levels (off/low/medium/high) to the model",
          default="1"),
    Entry("MCBOT_VISION_URL", "str", "inference",
          "chat-completions endpoint for image questions. Empty means images are refused out loud rather than sent somewhere arbitrary",
          default="", host_specific=True),
    Entry("MCBOT_VISION_MODEL", "str", "inference", "model id used for image questions",
          when="MCBOT_VISION_URL is set", host_specific=True),
    Entry("MCBOT_VISION_TIMEOUT", "float", "inference", "seconds before an image question gives up", default="60"),
    Entry("MCBOT_VISION_MAX_BYTES", "int", "inference", "largest image it will send", default="4000000"),
    Entry("MCBOT_VISION_MAX_IMAGES", "int", "inference", "images per question", default="3"),
    Entry("MCBOT_VISION_MAX_TOKENS", "int", "inference",
          "tokens the vision model may spend on a transcript. A screenshot answer is two sentences plus text",
          default="400"),

    # --- answers: how it talks, how much it remembers, how long it waits -----------------------------
    Entry("MCBOT_PUBLIC_ONLY", "bool", "answers",
          "answers are broadcast. In game that means `say` not `msg`; on Discord it means a public refusal and no DM answers",
          default="1"),
    Entry("MCBOT_DM_COOLDOWN", "float", "answers", "seconds between DM refusals aimed at one person", default="3600"),
    # These are phrases rather than words on purpose: the first live run refused "is the server up?" because
    # the list held `is it up` and bare `up` was absent, and adding bare `up` would weaken the fail-closed rule
    # (it appears inside change phrasing). Widening happens in multi-word entries only.
    Entry("MCBOT_READONLY_WORDS", "list", "answers",
          "phrases that mark an ask as a question rather than a change request",
          default="status,logs,who,uptime,version,ping,running,online,how many,is it up,is the server up,"
                 "server up,server running,how is the server,server status,tps,cpu,memory,disk,working,"
                 "currently working,is the server working,server working,is everything ok,everything ok,ip,"
                 "server ip,ip address,what is the ip"),
    Entry("MCBOT_HISTORY", "int", "answers", "past messages carried into an ask", default="12"),
    Entry("MCBOT_CONTEXT_MESSAGES", "int", "answers", "recent channel messages used as context", default="20"),
    Entry("MCBOT_CONTEXT_SELF_MAX", "int", "answers", "how many of its own past messages count as context",
          default="3"),
    Entry("MCBOT_CONTEXT_SCRATCH", "list", "answers", "phrases that mark a child trying to talk itself into a change",
          default="grown,ask a parent,find an adult,make me change the server,only the adults"),
    Entry("MCBOT_CONFIRM_TTL", "float", "answers", "seconds a confirmation button stays pressable", default="300"),
    Entry("MCBOT_TIMEOUT", "float", "answers", "overall budget for one ask", default="300"),
    Entry("MCBOT_IDLE_TIMEOUT", "float", "answers",
          "seconds of silence from the pi child before it is treated as wedged; activity resets it", default="1800"),
    Entry("MCBOT_QUEUE_MAX", "int", "answers", "asks a channel may hold while a run is in flight", default="3"),
    Entry("MCBOT_STEER_MAX_LEN", "int", "answers",
          "how short a follow-up has to be to count as a correction steered into the running turn", default="140"),
    Entry("MCBOT_RESUME_MAX_AGE", "float", "answers", "how old a queued ask may be and still be resumed",
          default="900"),

    # --- runtime: paths, ports, logging --------------------------------------------------------------
    Entry("MCBOT_DATA", "path", "runtime", "writable state: db, sessions, audit, the clone", default="/data"),
    Entry("MCBOT_APP_ROOT", "path", "runtime",
          "where the installed program lives; overridable so the startup contract is testable against a checkout",
          default="/opt/mcbot"),
    Entry("MCBOT_WORKSPACE", "path", "runtime", "the working directory pi runs in", default="/data/workspace"),
    Entry("MCBOT_DB", "path", "runtime", "sqlite for asks, confirmations and the queue", default="/data/mcbot.db"),
    Entry("MCBOT_AUDIT_DIR", "path", "runtime", "directory for the decision and gate logs", default="/data/audit"),
    Entry("MCBOT_SSH_DIR", "path", "runtime",
          "scratch directory for the 0600 copy of the deploy key; should be a tmpfs, not the PVC",
          default="/tmp/.ssh"),
    Entry("MCBOT_HEALTH_PORT", "int", "runtime", "port serving /healthz", default="8080"),
    Entry("MCBOT_LOG_LEVEL", "str", "runtime", "Python log level", default="INFO"),
]

# `MCBOT_APPROVER_MODE_NOTE` is a doc placeholder, not a variable: it exists so the group reads properly and is
# excluded from the derived lists. Test coverage asserts nothing reads it.
_PLACEHOLDERS = {"MCBOT_APPROVER_MODE_NOTE"}

NAME_RE = "MCBOT_"


def declared() -> list[Entry]:
    return [e for e in TABLE if e.name not in _PLACEHOLDERS]


def names() -> set[str]:
    return {e.name for e in declared()}


def entry(name: str) -> Entry:
    for e in TABLE:
        if e.name == name:
            return e
    raise KeyError(name)


def value_of(name: str, values=None):
    """The value this program will actually use for one variable: what is set, else the declared default,
    run through the declared type. Every reader in mcbot goes through here so that no caller has its own
    idea of what "unset" means."""
    e = entry(name)
    if e.kind == "path":
        return resolve(name, values)
    raw = _raw(name, values) or e.default
    return parse(e, raw)


def required() -> tuple[str, ...]:
    """The variables with no fallback, in table order. `bot.py`'s startup check is this list."""
    return tuple(e.name for e in declared() if e.required)


# (variable, why it is needed, what makes it needed). A condition rather than a hard requirement: asking for
# an RCON password from a deployment that switched the in-game path off is its own kind of unhelpful.
CONDITIONS = (
    ("MCBOT_INGAME_NAMESPACE", "the in-game ask path reads that pod's log as chat",
     lambda v: value_of("MCBOT_INGAME", v)),
    ("MCBOT_INGAME_POD", "the in-game ask path reads that pod's log as chat",
     lambda v: value_of("MCBOT_INGAME", v)),
    ("MCBOT_RCON_HOST", "answers, and any change an approved turn allows, go to the server console",
     lambda v: value_of("MCBOT_INGAME", v)),
    ("MCBOT_RCON_PASSWORD", "the server console connection is authenticated",
     lambda v: value_of("MCBOT_INGAME", v)),
    # Authority is only demanded where there is something to authorise. An empty allowlist in a deployment that
    # arms nothing is the pure-oracle case, and it is safe: it authorises nobody and mcbot answers questions.
    # The refusal that matters is the other one - a deployment that arms writes and names nobody to press the
    # button, which is a capability with no owner and looks identical to a bot that is merely shy.
    ("MCBOT_APPROVER_IDS", "the approver mode is an allowlist, no approver roles are named, and this deployment "
                           "arms changes, so somebody has to be allowed to confirm them",
     lambda v: value_of("MCBOT_APPROVER_MODE", v).lower() == "allowlist"
     and not value_of("MCBOT_APPROVER_IDS", v) and not value_of("MCBOT_APPROVER_ROLES", v)
     and bool(value_of("MCBOT_SERVER_ACTIONS", v) or value_of("MCBOT_INGAME_ACTIONS", v))),
    ("MCBOT_VISION_MODEL", "an image endpoint is configured",
     lambda v: bool(value_of("MCBOT_VISION_URL", v))),
)


def missing_required(values=None) -> list[str]:
    """Required variables that are absent. `MCBOT_GUILD_ID` satisfies the guild requirement: it predates the list."""
    out = []
    for name in required():
        if _raw(name, values):
            continue
        if name == "MCBOT_GUILDS" and _raw("MCBOT_GUILD_ID", values):
            continue
        out.append(name)
    return out


def missing_conditions(values=None) -> list[tuple[str, str]]:
    """Variables a deployment has asked for by switching something on, and did not supply."""
    out = []
    for name, why, active in CONDITIONS:
        try:
            if not active(values if values is not None else os.environ):
                continue
        except Exception:  # noqa: BLE001 - a broken switch is not this function's problem to guess at
            continue
        if not _raw(name, values):
            out.append((name, why))
    return out


def host_specific() -> tuple[str, ...]:
    return tuple(e.name for e in declared() if e.host_specific)


def conditional() -> tuple[Entry, ...]:
    return tuple(e for e in declared() if e.when and not e.required)


def _raw(name: str, values=None) -> str:
    values = os.environ if values is None else values
    return (values.get(name) or "").strip()


def resolve(name: str, values=None) -> str:
    """A path-typed variable's value: what is set, or the default moved under `MCBOT_DATA` if that is set.

    Every writable path mcbot uses defaults under `/data`. One variable - `MCBOT_DATA` - relocates all of them,
    which is what a deployment with a different mount point should have to do. The alternative is six path
    variables, one of which is forgotten and points back at `/data`, which on a container with a read-only root
    filesystem is a crash nobody can read.
    """
    e = entry(name)
    value = _raw(name, values)
    if value:
        return value
    default = e.default
    root = _raw("MCBOT_DATA", values)
    if root and default.startswith("/data/"):
        return root.rstrip("/") + default[len("/data"):]
    return default


def parse(entry: Entry, raw: str):
    """Turn a string from the environment into the type the program wants.

    Kept here rather than inline in `bot.py` so `test-config.py` can exercise each kind directly: a bool that
    accepts only "1" is a deployment that silently stays off, which is the failure mode the fail-closed
    capability switches were chosen for.
    """
    text = (raw or "").strip()
    if entry.kind == "bool":
        return text.lower() not in ("0", "false", "off", "no", "")
    if entry.kind == "int":
        return int(text) if text else 0
    if entry.kind == "float":
        return float(text) if text else 0.0
    if entry.kind == "list":
        return tuple(part.strip() for part in text.split(",") if part.strip())
    if entry.kind == "path" and not text:
        return resolve(entry.name)
    return text


def render_markdown() -> str:
    """`docs/configuration.md`, generated from the table so the document and the code cannot disagree."""
    lines = [
        "# Configuration",
        "",
        "Generated by `python3 config_table.py > docs/configuration.md`. `test-config.py` fails if the committed",
        "copy is not what the table produces, so this document cannot drift from the code that reads these names.",
        "",
        "Two kinds of line below. **Required** means mcbot stops at startup and names the variable, because the",
        "value describes a deployment and guessing it is worse than refusing. Everything else has a default that",
        "is meant to be safe: capability switches default off, the plugin allowlist defaults empty, and no default",
        "names a host, a path inside somebody's repository, a Discord snowflake or a person's name.",
        "",
    ]
    for group in GROUPS:
        rows = [e for e in declared() if e.group == group]
        if not rows:
            continue
        lines.append(f"## {group}\n")
        lines.append("| variable | kind | default | required | what it is for |")
        lines.append("| --- | --- | --- | --- | --- |")
        for e in rows:
            default = f"`{e.default}`" if e.default else "—"
            if e.required:
                req = "yes"
            elif e.when:
                req = e.when
            else:
                req = "no"
            lines.append(f"| `{e.name}` | {e.kind} | {default} | {req} | {e.purpose} |")
    lines += [
        "",
        "## Conditions worth knowing",
        "",
        "- `MCBOT_INGAME=0` disables the in-game path, and with it the RCON and pod requirements.",
        "- `MCBOT_APPROVER_MODE=anyone` means any member of `MCBOT_CHANNELS` may press a confirmation button. That",
        "  is a deliberate choice for a family server, and a bad one for anything else.",
        "- `MCBOT_VISION_URL` empty means image questions are refused in public. mcbot does not send a child's",
        "  screenshot to an endpoint nobody configured.",
        "- `MCBOT_SERVER_ACTIONS` and `MCBOT_INGAME_ACTIONS` are capabilities, not permissions. A pressed button",
        "  authorises one change in one turn; it does not move a switch.",
        "- `MCBOT_PLUGIN_ALLOWLIST` empty is the intended state. A plugin jar is an unreadable binary that joins",
        "  the server with op permissions, and it should arrive in a commit a person wrote.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    # `write` rather than `print`: this file is generated by redirecting stdout, and `test-config.py` compares
    # the result byte for byte, so the trailing newline has to be exactly the one the renderer produced.
    sys.stdout.write(render_markdown().rstrip() + "\n")
