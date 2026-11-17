#!/usr/bin/env python3
"""mcbot - Discord front door for a family Minecraft server.

An `@mcbot` mention in an allowed channel becomes a Pi prompt. Pi runs in this same container and
only ever gets `read/bash/edit/write`, but its two dangerous edges are physical rather than
advisory, which is the whole design:

  * `git` earlier on PATH than the real binary is bin/git-shim, which refuses any commit or push
    touching a path outside the prefix the deployment declares in `MCBOT_ALLOWED_PREFIX`
  * `kubectl` uses this pod's ServiceAccount, whose RBAC covers only the namespace the server lives
    in - no kubeconfig, no other context, nothing else to reach

Phase 1 = gateway + replies. Phase 2 = Pi bridge (this file). Phase 3 = the confirmation gate,
which is the `[[CONFIRM <kind>: <detail>]]` marker below: the model describes a destructive change
and stops; a human presses a button; only then does the model get told to proceed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import sys
import time
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional

import discord
from aiohttp import ClientError, ClientSession, ClientTimeout
from aiohttp import web

# bin/ holds the wrappers that are installed as `git`, `kubectl` and `mc-rcon` in the image, and the rules
# they enforce. bot.py reads the same functions the wrappers read: a persona that promised something the gate
# would refuse, or refused something the gate would allow, is a bug in one file that the other file would
# never show. One definition, two users.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin"))
from gate import capabilities, ingame_actions_on, plugin_allowlist, server_actions_on  # noqa: E402

from pi_rpc import PiError, PiReply, PiRpc

# `config_table` is where every default and the required-variable set live. Putting them in one file rather than
# inline in 2,100 lines is what makes it possible to test that nothing here reads a variable nobody declared,
# and that no default in a public repository points at somebody else's cluster.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_table as cfg  # noqa: E402
from rcon import Rcon, RconError

logging.basicConfig(
    level=str(cfg.value_of("MCBOT_LOG_LEVEL")).upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mcbot")


def _ids(name: str) -> set[int]:
    return {int(x) for x in os.environ.get(name, "").split(",") if x.strip().isdigit()}


class ConfigError(RuntimeError):
    """Configuration is missing or unusable. Reported as one line and a non-zero exit, not a traceback."""


@dataclass
class ConfigModel:
    """Startup configuration.

    Every field is populated by `load_config()`, not at class-definition time. That is not tidiness: the
    class used to carry `token: str = os.environ["DISCORD_BOT_TOKEN"]` as a default, which made merely
    importing `bot.py` require a live Discord token, so the build-time `import bot` smoke test died with
    a KeyError, and any tool wanting to reuse these regexes would have to supply production credentials
    to do it.

    The defaults are deliberately inert rather than permissive. `token=""` cannot connect, and empty
    `approvers` makes the approval and allowlist checks refuse everyone, so a path that forgets to call
    `load_config()` yields a bot that answers nobody rather than a bot that answers anybody.
    """

    loaded: bool = False
    token: str = ""
    guild_ids: set[int] = field(default_factory=set)
    channels: set[int] = field(default_factory=set)
    approvers: set[int] = field(default_factory=set)
    approver_roles: set[int] = field(default_factory=set)
    # Fail-closed, matching `config_table`, which is where this default actually lives. Two defaults for one
    # variable is how a security setting gets a second, untested value: an unloaded Config that defaults to
    # `anyone` authorises every stranger in the channel, and only `test-config.py` has to notice.
    approver_mode: str = "allowlist"
    workspace: str = "/data/workspace"
    history_len: int = 12
    # How much of the channel Discord hands us for each ask. 20 because that is roughly "what were we
    # just talking about" for a family channel, and one `history(limit=N)` request covers it.
    context_messages: int = 20
    # Which of mcbot's OWN past answers may re-enter the prompt as context, and which may never do so at all.
    # A retired policy sentence sitting in the window is an example, and the model performs examples.
    context_self_max: int = 3
    context_scratch: tuple[str, ...] = ("grown", "ask a parent", "find an adult",
                                        "make me change the server", "only the adults")
    # Asks waiting in a channel while a run is in flight, and how short a follow-on has to be to count as a
    # correction worth steering into the running turn instead of queueing as its own ask.
    queue_max: int = 3
    steer_max_len: int = 140
    # Durable scratch. strategy: Recreate stops the container outright on every deploy, and that takes the Pi
    # child, the ask in flight and everything waiting behind it down with it.
    db_path: str = "/data/mcbot.db"
    resume_max_age: int = 900
    # Empty means image questions are refused out loud. A baked-in default here would send a child's screenshot
    # to an endpoint nobody configured in the installation that shipped this repository.
    vision_url: str = ""
    vision_model: str = ""
    vision_timeout: float = 60.0
    vision_max_bytes: int = 4_000_000
    vision_max_images: int = 3
    vision_max_tokens: int = 400
    # A watchdog for a wedged child, not a limit on thinking: the clock restarts on every event Pi emits, so
    # a long git-and-rollout grind is allowed to take as long as it needs. Only silence is fatal.
    idle_timeout: float = 1800.0
    provider: str = ""
    model: str = ""
    # In-game asks: `!mcbot <question>` typed into Minecraft chat, answered back into the game over RCON.
    ingame: bool = True
    ingame_namespace: str = ""
    ingame_pod: str = ""
    # Named rather than left to kubectl's default: the server pod is one container today, and the day a
    # sidecar joins it an unnamed tail would interleave two logs into one fake chat.
    ingame_container: str = "server"
    ingame_triggers: tuple[str, ...] = ("!mcbot", "@mcbot")
    ingame_max_len: int = 400
    # Minecraft has no text wrap, so an answer is split into short lines and capped. Truncating in game is
    # deliberate: a 40-line wall of text in a child's chat window is unreadable and scrolls away the game.
    ingame_line_len: int = 110
    ingame_max_lines: int = 6
    ingame_cooldown: float = 10.0
    # Public-only answers, on both surfaces. In game every line is a broadcast rather than a whisper: a
    # private answer is how one player gets an edge nobody else asked for, and it is also an answer nobody
    # else can see the bot give. On Discord the same rule means a refusal is posted in the channel instead of
    # sent to one person, and a direct message is turned away rather than answered privately.
    public_only: bool = True
    dm_cooldown: float = 3600.0
    # The two capability switches, in the server owner's own words. They decide what mcbot is *capable* of at
    # all; the approver allowlist and the confirmation button decide who may use a capability, and an in-game
    # ask may not use either one. Three separate questions, three separate settings, because collapsing them
    # is how "anyone can press it" quietly becomes "anyone can do anything".
    server_actions: bool = False
    ingame_actions: bool = False
    # What kind of turn the agent is in, written before every ask so the kubectl wrapper can enforce it
    # instead of the persona asking nicely. The Pi child is one long-lived process, so this is a file the
    # wrapper reads rather than an environment variable set per ask.
    turn_mode_path: str = "/data/turn.json"
    rcon_host: str = ""
    rcon_port: int = 25575
    rcon_password: str = ""
    rcon_quiet: float = 0.75
    health_port: int = 8080
    audit_dir: str = "/data/audit"
    readonly_words: tuple[str, ...] = ()
    # How long a confirmation button stays pressable. It is a field rather than a literal because the bot
    # says the number out loud in the message that carries the button.
    confirm_ttl: float = 300.0


Config = ConfigModel()

REQUIRED_ENV = cfg.required()

def env(name: str):
    """One environment variable, with its default taken from `config_table` rather than a literal in here.

    Every default is declared once, in the table, where a test can see it and where a deployment can be checked
    against it. `cfg.value_of` applies the declared type, so a switch is a switch rather than a string some
    caller compares to "1", and it relocates a default path under MCBOT_DATA rather than leaving a straggler
    pointed at a read-only root filesystem.
    """
    return cfg.value_of(name)


def load_config() -> ConfigModel:
    """Populate `Config` from the environment. Must be called before anything reads it.

    The refusal is one message carrying both lists. Half a configuration discovered over two restarts is how a
    deployment ends up fixed by somebody reading code rather than reading the log.
    """
    missing = cfg.missing_required()
    conditional = cfg.missing_conditions()
    if missing or conditional:
        parts = []
        if missing:
            parts.append("required environment variable(s) not set: " + ", ".join(missing))
        if conditional:
            parts.append("required by the settings in force: "
                         + "; ".join(f"{name} ({why})" for name, why in conditional))
        raise ConfigError(" | ".join(parts)
                          + " - see docs/configuration.md. mcbot will not guess a host, a path it may write to,"
                            " or a place it is allowed to speak")

    Config.loaded = True
    Config.token = os.environ["DISCORD_BOT_TOKEN"]
    Config.guild_ids = _ids("MCBOT_GUILDS") | _ids("MCBOT_GUILD_ID")
    # `MCBOT_GUILDS` is the list, `MCBOT_GUILD_ID` still works for a single server. This is where the
    # first live test actually broke, and it was configuration rather than code: the bot had been invited
    # into one server while the ConfigMap named a different one, so every message in front of it was
    # declined as "wrong server" - and, until the decision log existed, with nothing to show for it. A
    # refusal with no reason recorded is indistinguishable from a bug, which is why `decide()` logs.
    Config.channels = _ids("MCBOT_CHANNELS")              # mentions accepted only here
    Config.approvers = _ids("MCBOT_APPROVER_IDS")         # used in allowlist mode
    Config.approver_roles = _ids("MCBOT_APPROVER_ROLES")  # or hold one of these roles
    # Who may make mcbot change something. Ed's call, and it is the product rather than a knob: mcbot
    # exists so the boys run their own server and he supplies the hosting, so an age gate on "restart it"
    # was the wrong control - it made the bot say no to the exact people it is for.
    #
    # What limits damage is therefore not who is asking but where and how: MCBOT_GUILDS and
    # MCBOT_CHANNELS decide where mcbot answers at all, MCBOT_ALLOWED_PREFIX decides which files the agent
    # may edit, the git wrapper enforces that from outside the agent, this pod's kubectl RBAC is scoped to
    # the minecraft namespace, and a destructive action still needs a deliberate second press. Those are
    # the rails that hold regardless of mode. `allowlist` restores the original gate.
    # Fail-closed by default: an installation that says nothing gets the allowlist gate. Our own deployment sets
    # `anyone` explicitly, in the open, because that is the control Ed chose for his own boys - the product, not
    # an oversight.
    Config.approver_mode = (env("MCBOT_APPROVER_MODE") or "allowlist").strip().lower()
    if Config.approver_mode not in ("anyone", "allowlist"):
        log.warning("MCBOT_APPROVER_MODE %r is neither anyone nor allowlist; using allowlist",
                    Config.approver_mode)
        Config.approver_mode = "allowlist"
    Config.workspace = env("MCBOT_WORKSPACE")
    Config.history_len = env("MCBOT_HISTORY")
    Config.context_messages = env("MCBOT_CONTEXT_MESSAGES")
    Config.context_self_max = env("MCBOT_CONTEXT_SELF_MAX")
    Config.queue_max = env("MCBOT_QUEUE_MAX")
    Config.steer_max_len = env("MCBOT_STEER_MAX_LEN")
    Config.db_path = env("MCBOT_DB")
    Config.resume_max_age = max(30, env("MCBOT_RESUME_MAX_AGE"))
    Config.context_scratch = tuple(w.strip().lower() for w in env("MCBOT_CONTEXT_SCRATCH") if w.strip())
    Config.vision_url = env("MCBOT_VISION_URL")
    Config.vision_model = env("MCBOT_VISION_MODEL")
    Config.vision_timeout = env("MCBOT_VISION_TIMEOUT")
    Config.vision_max_bytes = env("MCBOT_VISION_MAX_BYTES")
    Config.vision_max_images = env("MCBOT_VISION_MAX_IMAGES")
    Config.vision_max_tokens = env("MCBOT_VISION_MAX_TOKENS")
    Config.idle_timeout = env("MCBOT_IDLE_TIMEOUT") or env("MCBOT_TIMEOUT")
    Config.provider = env("PI_PROVIDER")
    # `PI_MODEL` is a list because a provider file may declare several ids; the gateway talks to the first one.
    # Empty is not an error here: pi_rpc resolves it through the table the same way, and a name is a deployment
    # choice rather than something this file is allowed to invent.
    _models = env("PI_MODEL")
    Config.model = _models[0] if _models else ""
    Config.health_port = env("MCBOT_HEALTH_PORT")
    Config.audit_dir = env("MCBOT_AUDIT_DIR")
    # Phrases, not bare words, wherever a phrase is what a parent actually types. The first live test
    # run got "is the server up?" refused: the list had the phrase `is it up`, and "up" alone was not
    # in it, so a plain status question read as a change request and needed an approver that does not
    # exist yet. Adding bare `up` would weaken the fail-closed rule (it appears inside change phrasing),
    # so the widening is in multi-word entries only.
    Config.readonly_words = tuple(w.strip().lower() for w in env("MCBOT_READONLY_WORDS") if w.strip())
    Config.confirm_ttl = env("MCBOT_CONFIRM_TTL")
    Config.ingame = env("MCBOT_INGAME")
    Config.ingame_namespace = env("MCBOT_INGAME_NAMESPACE")
    Config.ingame_pod = env("MCBOT_INGAME_POD")
    Config.ingame_container = env("MCBOT_INGAME_CONTAINER")
    Config.ingame_triggers = tuple(t.strip().lower() for t in env("MCBOT_INGAME_TRIGGERS") if t.strip())
    Config.ingame_max_len = env("MCBOT_INGAME_MAX_LEN")
    Config.ingame_line_len = env("MCBOT_INGAME_LINE_LEN")
    Config.ingame_max_lines = env("MCBOT_INGAME_MAX_LINES")
    Config.ingame_cooldown = env("MCBOT_INGAME_COOLDOWN")
    Config.public_only = env("MCBOT_PUBLIC_ONLY")
    Config.dm_cooldown = env("MCBOT_DM_COOLDOWN")
    Config.turn_mode_path = env("MCBOT_TURN_MODE")
    Config.server_actions = server_actions_on()
    Config.ingame_actions = ingame_actions_on()
    Config.rcon_host = env("MCBOT_RCON_HOST")
    Config.rcon_port = env("MCBOT_RCON_PORT")
    Config.rcon_password = env("MCBOT_RCON_PASSWORD")
    # How long an RCON answer may go silent before it is treated as finished. This server gives no end-of-
    # response marker and punishes the trick that used to provide one, so the window is the only signal.
    Config.rcon_quiet = env("MCBOT_RCON_QUIET")

    if Config.approver_mode == "allowlist" and not Config.approvers and not Config.approver_roles:
        log.warning(
            "MCBOT_APPROVER_MODE is allowlist and both approver lists are empty, so mcbot answers "
            "questions and refuses every write, including every confirmation button."
        )
    elif Config.approver_mode == "anyone":
        log.info("approver mode: anyone - anyone in a permitted server and channel may ask and confirm; "
                 "the limits are the guild list, the channel list, MCBOT_ALLOWED_PREFIX, the git wrapper, "
                 "namespace-scoped kubectl RBAC and the confirmation press")
    else:
        log.info("approver mode: allowlist - %d user id(s) and %d role id(s) may confirm",
                 len(Config.approvers), len(Config.approver_roles))
    return Config


WRITE_HINT_RE = re.compile(
    r"\b(delete|deleting|wipe|wiping|remove|removing|destroy|drop|dropping|restart|restarting|reboot|"
    r"stop|stopping|start|starting|boot|deploy|deploying|rollback|revert|reverting|commit|committing|"
    r"push|pushing|pull|merge|apply|applying|patch|patching|edit|editing|change|changing|update|"
    r"updating|bump|bumping|upgrade|upgrading|modify|modifying|create|creating|make|making|set|sets|"
    r"reset|resetting|restore|restoring|scale|scaling|install|installing|kubectl|helm|git)\b",
    re.IGNORECASE,
)

# Splitting on these is not a parser; it is a cheap way to say "this sentence is asking for more than
# one thing, so it is not a status check".
MULTI_CLAUSE = (";", "\n", " then ", " and ", " but ", " after ", " also ", " plus ")


def is_readonly(text: str) -> bool:
    """Whether this message is nothing but a status question.

    Deliberately conservative, because a message that classifies as read-only is allowed through from
    anyone - and it then reaches the *same* Pi session with the same `git` and `kubectl` tools as an
    approver's. The classifier is the only thing between a six-year-old's phrasing and a write, so it
    errs towards 'not read-only': any write verb, any second clause, or a message that merely happens
    to begin with a read-only word fails.

    The previous version was `text.lower().startswith(readonly_words)`, which let "logs; now delete the
    world" through as a status request.
    """
    low = (text or "").lower().strip().strip("?!. ")
    if not low:
        return False
    if WRITE_HINT_RE.search(low):
        return False
    if any(sep in low for sep in MULTI_CLAUSE):
        return False
    return any(
        re.search(r"\b" + re.escape(w) + r"\b", low) for w in Config.readonly_words
    )


CONFIRM_RE = re.compile(
    r"\[\[CONFIRM\s+([a-z][a-z _-]{1,30})\s*:?\s*([^\]]{0,400})?\]\]", re.IGNORECASE
)
MENTION_RE = re.compile(r"<@!?(\d+)>|<@&(\d+)>")

# --------------------------------------------------------------------------- state


@dataclass
class Pending:
    kind: str
    detail: str
    question: str
    author_id: int
    channel_id: int
    store_id: int = 0
    created: float = field(default_factory=time.time)
    ttl: float = 300.0

    @property
    def expired(self) -> bool:
        return time.time() - self.created > self.ttl


# "stop" on its own means stop. Kids type it when a bot has gone off on a ten second restart, and it has to
# mean something or the only escape hatch is waiting.
CANCEL_RE = re.compile(r'^(stop|stop it|cancel|abort|never mind|nevermind|nvm|forget it|forget that|'
                       r'scratch that)\b[.!]?$', re.I)
# A follow-on that reads like a correction of the ask currently running, rather than a new job.
MODIFIER_RE = re.compile(r"^(no[, ]|actually|wait|also|and\b|plus|but\b|i mean|not that|one more|instead|"
                         r"use\b|change it|make it|with\b|without\b|add\b|remove\b|instead of)", re.I)

# --------------------------------------------------------------------------- in-game chat
#
# What the server writes to stdout for a player talking. Two formats appear in this image's log, depending on
# which component wrote the line, and both carry ANSI cursor-control junk from the server console in front of
# them (`>....\x1b[K[06:21:44 INFO]: <SomePlayer> hi`), so the match is on the `]: <name> ` seam rather than
# on an anchored line shape. Read from the real log, not from the documentation.
#
# What the pattern refuses is the safety property, so it is written down instead of left as regex:
#   - a join or quit line (`Notch joined the game`) has no `<name>`, so it cannot be an ask;
#   - a player issuing a command is logged as `Steve issued server command: /plugins`, which has no closing
#     `>`, so it is not chat either;
#   - `[Not Secure]` is allowed as an optional prefix rather than treated as a reason to refuse: Paper writes
#     it for a client that skipped chat signing, and an unsigned ask is still an ask from a player who is
#     in the game. It is the one prefix that has to be matched by hand, which is why the name rule below is
#     what stops it from also matching the bot's own traffic.
#   - `<name>` is `[A-Za-z0-9_]{1,16}` because that is the name set Minecraft itself allows, so no other
#     bracketed prefix can read as a player - including `[Server] `, which is how a console or RCON `say` is
#     printed back. That is what stops the tail from answering its own broadcasts: every answer this bot gives
#     in game arrives on this same stdout, and the pattern that accepts a player's line refuses the bot's.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# The optional `[Not Secure] ` is Paper marking an unsigned or unencrypted source - it appears on player
# chat from a client that skipped chat signing, and on the bot's own RCON traffic. Accepting it cannot
# make the bot read itself: after it, the pattern still needs `<name>`, and what follows a broadcast is
# `[Rcon]` or `<[Server]>`, neither of which is a name.
CHAT_LINE_RE = re.compile(r"\]:\s*(?:\[Not Secure\]\s*)?<(?P<name>[A-Za-z0-9_]{1,16})>\s(?P<text>.*)$")
# In-game answers to an open offer. `yes` alone in family chat must never approve a destructive change, so an
# in-game press has to carry the trigger like every other in-game ask.
INGAME_YES_RE = re.compile(r"^(yes|y|do it|confirm|go ahead|do it)\b[.!]?$", re.I)
INGAME_NO_RE = re.compile(r"^(no|n|drop it|cancel|stop|never mind|nevermind|nvm)\b[.!]?$", re.I)


def journal_mode_for(path: str, mounts: Optional[list] = None) -> str:
    """WAL or DELETE, decided by what the database file is actually sitting on.

    WAL hands the write-ahead log to a shared memory map between the writers. A local filesystem can do that;
    an NFS mount cannot do it reliably, and SQLite's own documentation says WAL over NFS risks a corrupted
    database. This app's volume is Longhorn RWX, which is NFS behind a share-manager, so the rollback journal
    is the correct mode here - and the check is by mount table rather than by belief about the volume.
    """
    target = os.path.abspath(path)
    if mounts is None:
        mounts = []
        try:
            with open("/proc/mounts") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3:
                        mounts.append((parts[1], parts[2]))
        except OSError:
            return "DELETE"
    best_len, best_type = -1, ""
    for mount_point, fstype in mounts:
        if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) and len(mount_point) > best_len:
            best_len, best_type = len(mount_point), fstype
    return "DELETE" if best_type in ("nfs", "nfs4", "cifs", "fuse", "fuse.nfs", "fuse.sshfs") else "WAL"


class Store:
    """What has to survive a rollout, on the PVC the audit log already lives on.

    A deploy stops this container completely (strategy: Recreate, and it has to be Recreate while a workspace
    and a git checkout are shared), so the Pi child dies mid-task and anything waiting behind it vanishes
    with it. SQLite is already in the image, needs no service to be healthy, and it is one writer on one
    volume - which is also why the journal mode is chosen from the filesystem it lands on rather than
    assumed: this volume is RWX, and RWX on Longhorn means NFS, and WAL on NFS is the documented way to
    corrupt a database.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._db: Optional[sqlite3.Connection] = None
        self.error: str = ""
        self.journal_mode: str = ""

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._db = sqlite3.connect(self.path, timeout=10)
            self._db.row_factory = sqlite3.Row
            self.journal_mode = journal_mode_for(self.path)
            self._db.execute(f"PRAGMA journal_mode={self.journal_mode}")
            self._db.execute("PRAGMA busy_timeout=10000")
            if self.journal_mode != "WAL":
                # On NFS there is no reliable shared mmap for SQLite's locking, so it takes the slow path
                # and waits for every commit to reach the server.
                self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS queue(
                  id INTEGER PRIMARY KEY, channel_id INTEGER, text TEXT, note TEXT, user_id INTEGER,
                  approved INTEGER, created REAL, status TEXT);
                CREATE INDEX IF NOT EXISTS queue_open ON queue(status, channel_id, id);
                CREATE TABLE IF NOT EXISTS run(
                  channel_id INTEGER PRIMARY KEY, text TEXT, started REAL);
                CREATE TABLE IF NOT EXISTS confirm(
                  id INTEGER PRIMARY KEY, channel_id INTEGER, kind TEXT, detail TEXT, question TEXT,
                  author_id INTEGER, created REAL, status TEXT);
                """)
        return self._db

    def _safe(self, what: str, fn):
        """A PVC that has gone wrong must not take the chat down with it: report it and carry on."""
        try:
            return fn(self._conn())
        except (sqlite3.Error, OSError) as exc:
            self.error = f"{what}: {type(exc).__name__}: {str(exc)[:180]}"
            log.warning("store %s", self.error)
            return None

    def _write(self, sql: str, args: tuple = ()) -> int:
        """Execute and commit, and return the row count. Committing matters: a queue row the next pod cannot
        see is the same as no queue at all."""
        def go(db: sqlite3.Connection) -> int:
            cur = db.execute(sql, args)
            db.commit()
            return cur.rowcount if cur.rowcount is not None else 0
        return self._safe("write", go) or 0

    def queue_add(self, channel_id: int, text: str, note: str, user_id: int, approved: bool) -> None:
        self._write(
            "INSERT INTO queue(channel_id, text, note, user_id, approved, created, status) VALUES(?,?,?,?,?,?,?)",
            (channel_id, text, note, user_id, int(approved), time.time(), "waiting"))

    def queue_count(self, channel_id: int) -> int:
        return self._safe("queue_count", lambda db: db.execute(
            "SELECT COUNT(*) FROM queue WHERE channel_id=? AND status='waiting'", (channel_id,)).fetchone()[0]) or 0

    def queue_take(self, channel_id: int) -> Optional[dict]:
        row = self._safe("queue_take", lambda db: db.execute(
            "SELECT * FROM queue WHERE channel_id=? AND status='waiting' ORDER BY id LIMIT 1",
            (channel_id,)).fetchone())
        if row is None:
            return None
        self._write("DELETE FROM queue WHERE id=?", (row["id"],))
        return dict(row)

    def queue_drop(self, channel_id: int) -> int:
        return self._write("DELETE FROM queue WHERE channel_id=? AND status='waiting'", (channel_id,))

    def queue_counts(self) -> dict:
        rows = self._safe("queue_counts", lambda db: db.execute(
            "SELECT channel_id, COUNT(*) n FROM queue WHERE status='waiting' GROUP BY channel_id").fetchall())
        return {int(r["channel_id"]): int(r["n"]) for r in rows} if rows else {}

    def channels_with_queue(self) -> list:
        rows = self._safe("channels_with_queue", lambda db: db.execute(
            "SELECT DISTINCT channel_id FROM queue WHERE status='waiting'").fetchall())
        return [int(r["channel_id"]) for r in rows] if rows else []

    def queue_oldest_age(self, channel_id: int) -> float:
        row = self._safe("queue_oldest_age", lambda db: db.execute(
            "SELECT MIN(created) t FROM queue WHERE channel_id=? AND status='waiting'", (channel_id,)).fetchone())
        return (time.time() - row["t"]) if row and row["t"] else 0.0

    def run_note(self, channel_id: int, text: str) -> None:
        self._write("INSERT OR REPLACE INTO run(channel_id, text, started) VALUES(?,?,?)",
                    (channel_id, text[:600], time.time()))

    def run_clear(self, channel_id: int) -> None:
        self._write("DELETE FROM run WHERE channel_id=?", (channel_id,))

    def runs_interrupted(self) -> list:
        rows = self._safe("runs_interrupted", lambda db: db.execute("SELECT * FROM run").fetchall())
        return [dict(r) for r in rows] if rows else []

    def confirm_save(self, channel_id: int, kind: str, detail: str, question: str, author_id: int) -> int:
        def go(db: sqlite3.Connection) -> int:
            cur = db.execute(
                "INSERT INTO confirm(channel_id, kind, detail, question, author_id, created, status) "
                "VALUES(?,?,?,?,?,?,?)", (channel_id, kind, detail, question[:1800], author_id, time.time(), "open"))
            db.commit()
            return int(cur.lastrowid or 0)
        return self._safe("confirm_save", go) or 0

    def confirm_close(self, store_id: int, status: str) -> None:
        if not store_id:
            return
        self._write("UPDATE confirm SET status=? WHERE id=?", (status, store_id))

    def confirm_active(self) -> list:
        rows = self._safe("confirm_active", lambda db: db.execute(
            "SELECT * FROM confirm WHERE status='open' ORDER BY id").fetchall())
        return [dict(r) for r in rows] if rows else []

    def prune(self, max_age: int) -> int:
        """Rows nobody will ever answer again ("restart the server" from three hours ago) are worse than lost:
        they arrive late, out of context, and with nobody remembering asking."""
        return self._write("DELETE FROM queue WHERE created < ?", (time.time() - max_age,))

    def stats(self, busy: Optional[set] = None) -> dict:
        """`interrupted` means "cut off", so a channel that is busy right now is not an interruption. Counting
        the run in flight as one would put '1 run interrupted by a restart' in front of the model while it
        was sitting in the middle of that very run."""
        rows = self.runs_interrupted()
        interrupted = [r for r in rows if not busy or r["channel_id"] not in busy]
        return {"path": self.path, "journal": self.journal_mode or journal_mode_for(self.path),
                "queued": self.queue_counts(), "interrupted": len(interrupted),
                "confirmations_open": len(self.confirm_active()), "error": self.error or None}


class State:
    def __init__(self) -> None:
        self.history: dict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=Config.history_len))
        self.pending: dict[int, Pending] = {}
        self.busy: set[int] = set()
        self.started = time.time()
        self.last_error: Optional[str] = None
        self.counts: dict[str, int] = defaultdict(int)
        self.last_decision: Optional[dict] = None
        # Bodies mcbot has just sent per channel, for the duplicate check in `McBot._unique`.
        self.recent_bodies: dict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=6))
        # Set when Discord refuses a reaction (usually a missing Add Reactions permission), so the missing
        # lifecycle emoji is visible in healthz instead of being inferred from an absent reaction.
        self.reaction_error: Optional[str] = None
        # In-game asks are queued in memory, never in the durable store: see GameChannel.
        self.game_queue: dict[int, list[tuple]] = {}
        # Last inbound ask per player, for the cooldown.
        self.game_last: dict[str, float] = {}
        # Synthetic channel id -> player name, so a queued ask can be answered to the right person.
        self.game_names: dict[int, str] = {}
        # Last time a given Discord user was pointed out of a DM. Answering a DM once is guidance; answering
        # every one of them is a private conversation, which is the thing the public rule exists to prevent.
        self.dm_last: dict[int, float] = {}
        # How many times a DM has been turned away with a pointer to the right channel. Its own counter, not
        # part of `counts`: `counts` is decisions by kind and the audit file's line count is asserted against
        # its sum, so a delivery tally belongs somewhere else.
        self.dm_pointers = 0
        self.game_seen: int = 0


state = State()
client: Optional["McBot"] = None
# The McBot instance keeps its own reference, but the health handler is a module-level function with no
# instance to read, so the store has to be reachable the same way `state` and `client` are.
STORE: Optional["Store"] = None


# ------------------------------------------------------------------------ decisions

# Rotated rather than repeated: Discord refuses to deliver identical content twice in one channel
# within a few seconds ("Duplicate message body, probably got already sent"), and that is a real
# failure mode here, not a curiosity - mcbot answered the first question in a channel and then looked
# dead for the next two. Every variant says the same thing and points at a question that does work.
REFUSALS = (
    "that one reads as a change to me and nobody is allowlisted yet, so nothing happens. "
    "Try \"server status\".",
    "still a change request as far as I can tell, and the allowlist is empty, so nothing happens. "
    "Ask me \"who is online\" or \"server status\" and I'll answer that.",
    "can't act on that one - allowlist mode with no ids configured. \"server status\" works.",
)


DM_REFUSAL = ("I only answer in the Minecraft channels, so everyone gets the same answer. "
              "Ask me there and I'll put it in front of everybody.")

IN_GAME_RULE = (
    "That ask came from inside the game. Two things follow, and neither is negotiable: every line you say "
    "is broadcast to every player on the server, and you may not change anything. Read as much as you like - "
    "status, logs, versions, who is whitelisted, config values, where structures generated, how a mechanic "
    "works - and `mc-rcon \"list\"` and `kubectl get` both work in this turn. A command that alters something "
    "is refused by the tool that would run it, so do not try one and do not promise one. If someone in game wants something changed, say in one line what it would do and that changes "
    "are asked for in the Discord channel, where there is a confirmation button. Do not ask for permission, "
    "do not offer to do it, do not emit a [[CONFIRM]] marker. And because every line is public, do not state "
    "a player's own coordinates, base location, or what is in their inventory: those are not server facts, "
    "and broadcasting them is how a base gets found."
)


def capability_note() -> str:
    """What mcbot is allowed to change, said to the model in the same words the wrappers enforce it in.

    The point is not to make the model obedient. It is to stop it offering a thing the tool will refuse five
    seconds later, which is the failure that reads to a nine year old as "the bot is broken".
    """
    caps = capabilities()
    yes, no = "allowed", "switched off in config"
    plugins = ", ".join(caps["plugins"]) if caps["plugins"] else "none named"
    return (
        f"What you may change, and this is not a preference: out-of-game operations (whitelist, server "
        f"properties, restarts) are {yes if caps['server_actions'] else no}; in-game actions (give, tp, "
        f"summon, gamemode, kick, ban) are {yes if caps['ingame_actions'] else no}; plugins you may install: "
        f"{plugins}. The kubectl and mc-rcon wrappers refuse the rest whatever anybody asks, so do not offer "
        f"a thing you cannot do - say once, plainly, what mcbot does not do and what the useful version is."
    )


def dm_refusal_due(now: float, last: float, cooldown: float) -> bool:
    """Whether to answer this direct message at all.

    One pointer is helpful. Replying to every DM turns the bot into a private channel, which is exactly the
    fairness and visibility property the public rule removes, so the pointer is rate limited per user.
    """
    return now - last >= cooldown


def write_turn_mode(path: str, *, readonly: bool, approved: bool = False, surface: str,
                    text: str = "") -> bool:
    """Record what kind of turn is about to run, for the kubectl wrapper to enforce.

    The Pi child is one process that outlives every ask, so there is no per-ask environment to set; this file
    is the channel between the bot's decision and the wrapper's refusal. It is advisory to the model and
    binding on the wrapper, which is the whole point - a rule that only holds while the model agrees with it
    is not a rule.
    """
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "readonly": bool(readonly),
           "approved": bool(approved), "surface": surface, "text": (text or "")[:200]}
    try:
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, sort_keys=True)
        os.replace(tmp, path)  # a wrapper reading mid-write must never see half a record
    except OSError as exc:
        log.warning("cannot write the turn mode file %s: %s", path, exc)
        return False
    return True


def read_turn_mode(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def decision(kind: str, reason: str, *, guild=None, channel=None, author=None, text: str = "",
             readonly: Optional[bool] = None, approver: Optional[bool] = None) -> None:
    """Record what mcbot did with an inbound message - in the log as well as on the PVC.

    Every decline path used to `return` in silence, which made the bot look broken when it was working
    exactly as configured: a message from the wrong server, a channel outside MCBOT_CHANNELS, a message
    that never actually mentioned the bot, and a refused change request all produced zero output. The
    only artefact on disk was git.jsonl, written by the wrapper, so `kubectl logs` showed nothing but
    readiness probes and there was no way to tell "not hearing me" apart from "heard me and said no".

    Writes are best-effort: an audit log that cannot be written must not swallow the message it was
    describing, so a failure is recorded in `state.last_error` and reported once, not raised.
    """
    state.counts[kind] += 1
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": kind,
        "reason": reason,
        "guild": guild,
        "channel": channel,
        "author": author,
        "readonly": readonly,
        "approver": approver,
        "text": (text or "")[:120],
    }
    state.last_decision = rec
    log.info("decision %s: %s (guild=%s channel=%s author=%s readonly=%s approver=%s) %r",
             kind, reason, guild, channel, author, readonly, approver, rec["text"])
    try:
        os.makedirs(Config.audit_dir, exist_ok=True)
        with open(os.path.join(Config.audit_dir, "decisions.jsonl"), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError as exc:
        state.last_error = f"decision log write failed: {exc}"
        log.warning("cannot write %s/decisions.jsonl: %s", Config.audit_dir, exc)


def can_approve(user: discord.abc.User) -> bool:
    """Whether this user may make mcbot change something.

    `MCBOT_APPROVER_MODE` picks the rule:

    - `anyone` (deployed): everyone who got past the guild and channel gates. The adult filter was
      removed on purpose - mcbot exists so the boys can run their own server, and a bot that tells them
      "only grown-ups can change the server" is refusing the users it was built for while answering the
      parent who is trying to hand the job over.
    - `allowlist`: the original gate, unchanged. An empty `MCBOT_APPROVER_IDS` and `MCBOT_APPROVER_ROLES`
      means *no one*, not everyone. That history is worth keeping in words: the same set gates the tier-3
      confirmation button, so an unconfigured allowlist in this mode means any 6-year-old who can
      @-mention the bot can approve a world wipe. Reach for this mode if mcbot ever runs somewhere Ed is
      not the one accepting the consequences.

    In `anyone` mode the guard against a wiped world is not identity, it is the technical rails named in
    load_config, and the confirmation press that turns "delete the world" into two deliberate actions.
    """
    if Config.approver_mode != "allowlist":
        return True
    if not Config.approvers and not Config.approver_roles:
        return False
    if user.id in Config.approvers:
        return True
    roles = getattr(user, "roles", None) or []
    return bool({r.id for r in roles} & Config.approver_roles)


# --------------------------------------------------------------------------- discord


class GameChannel:
    """One player's in-game chat, treated as a channel.

    mcbot's pipeline is written around a channel: a queue per channel, one run per channel, one open
    confirmation per channel. Rather than fork that pipeline for Minecraft, an in-game ask gets its own channel
    id and a `send()` that writes into the game over RCON. Queueing, steering, abort, duplicate suppression and
    the busy set are then shared code, which is the whole point: an in-game ask is the same ask with a
    different window.

    The id is negative and derived from the name, because a Discord snowflake is always positive. Stable per
    player rather than random, so two asks from the same kid land in the same queue while two kids cannot
    cancel each other's run.

    `durable = False` is the one deliberate difference. A Discord ask can be re-posted hours after a restart
    because the channel is still there and somebody can press the button again. An in-game ask belongs to a
    player standing in a world, and answering it after a rollout means replying into a chat window that
    scrolled past it an hour ago. In-game asks therefore live in memory only.
    """

    durable = False
    ingame = True

    def __init__(self, bot: "McBot", player: str) -> None:
        self.bot = bot
        self.player = player
        self.id = -(1 + zlib.crc32(player.encode()))
        self.guild = None

    async def send(self, content: str, **_kwargs) -> None:
        await self.bot.game_say(self.player, content)


class McBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents, log_handler=None)
        # `discord.app_commands.CommandTree`, not `discord.CommandTree`. The latter does not exist in
        # discord.py 2.x - it was a name from the 1.x `ext.commands` era - and the bot died at startup
        # with `AttributeError: module 'discord' has no attribute 'CommandTree'`. Client already builds
        # a tree of its own; this is explicit rather than implicit so the tests can see the object.
        self.tree = discord.app_commands.CommandTree(self)
        self.tree.error(self._on_tree_error)
        # Durable across a restart, so a deploy in the middle of a restart does not silently lose the ask.
        self.store = Store(Config.db_path)
        global STORE
        STORE = self.store
        self.pi: Optional[PiRpc] = None
        self.rcon: Optional[Rcon] = None
        self.game_task: Optional[asyncio.Task] = None
        self.game_tailing = False
        self.game_error: Optional[str] = None

    async def _on_tree_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
        """10062 (Unknown interaction) is the signature of a second client sharing this bot token.

        Discord hands an interaction to whichever gateway claims it first; the loser gets 10062. A leftover
        `docker run` of an old build did exactly that for nine hours: the ghost answered with the retired
        canned refusal while this pod saw its own messages logged as `author is a bot`. Loud here, because
        the alternative is spending an afternoon blaming the model.
        """
        inner = getattr(error, "original", error)
        text = str(inner)
        if getattr(inner, "code", None) == 10062 or "Unknown interaction" in text:
            state.last_error = "10062: another mcbot is answering interactions on this token"
            log.error("interaction %s was already answered - is a second mcbot running with this token "
                      "(a local `docker run` counts)?", interaction.id)
            return
        state.last_error = f"{type(inner).__name__}: {inner}"
        log.exception("app command failed", exc_info=inner)

    async def setup_hook(self) -> None:
        self.pi = PiRpc(cwd=Config.workspace, provider=Config.provider, model=Config.model)
        await self.pi.start()
        log.info("pi bridge started in %s", Config.workspace)
        await self._sync_commands()
        log.info("slash commands synced")
        if Config.ingame:
            can_answer, why_not = game_path_status(Config.rcon_host, Config.rcon_password)
            if can_answer:
                self.rcon = Rcon(Config.rcon_host, Config.rcon_port, Config.rcon_password,
                            quiet=Config.rcon_quiet)
                self.game_task = asyncio.create_task(self._watch_game())
                log.info("in-game asks on %s (server %s/%s:%s, rcon %s:%s)", "/".join(Config.ingame_triggers),
                         Config.ingame_namespace, Config.ingame_pod, Config.ingame_container,
                         Config.rcon_host, Config.rcon_port)
            else:
                # Deliberately not tailing. Reading chat you cannot reply to is the failure mode this whole
                # feature is supposed to avoid, and it is invisible from inside the game.
                self.game_error = why_not
                log.error("%s - a player typing %s would get nothing back, so no chat is read at all",
                          why_not, Config.ingame_triggers[0])
        else:
            log.info("in-game asks are off (MCBOT_INGAME=0)")

    async def _sync_commands(self) -> list:
        """Register slash commands in the server they are actually used in.

        `tree.sync()` with no argument registers **global** commands. Two costs, both paid on every
        deploy: global commands can take up to an hour to appear in a server, and the global command
        endpoint is rate limited per application - the first deploy after a rollout logged
        `We are being rate limited. PUT .../applications/<id>/commands responded with 429`, which is one
        pod spending the app's global budget re-registering a command it already had.

        Guild-scoped sync is immediate, and it is the endpoint a single-server bot is meant to use. The
        trade is that the command only exists in `MCBOT_GUILD_ID`, which is where `/mc` answers anyway -
        every other server already gets "Wrong server."
        """
        if not Config.guild_ids:
            try:
                return await self.tree.sync()
            except discord.HTTPException as exc:
                # Same rule as below: a command registration failure degrades mcbot to mention-only,
                # it does not take the process down.
                state.last_error = f"global slash command sync failed: {exc}"
                log.warning("could not register slash commands globally (%s); @-mentions still work", exc)
                return []

        synced: list = []
        for guild_id in sorted(Config.guild_ids):
            try:
                synced += await self.tree.sync(guild=discord.Object(id=guild_id))
            except discord.HTTPException as exc:
                # A server listed in MCBOT_GUILDS that the bot has not been invited into yet answers
                # `403 50001 Missing Access`, which is the expected state right after a deploy - the invite
                # link is a human action and mcbot cannot complete it. It must not take the pod down: the
                # first deploy with two servers crash-looped on exactly this, because the setup_hook
                # exception propagated out of `login()` and killed the bot for a server it was never in.
                state.last_error = f"slash command sync in server {guild_id}: {exc}"
                log.warning("could not register slash commands in server %s (%s). If I am not in that "
                            "server yet it is because nobody has accepted the invite link there; the "
                            "servers I am in still work.", guild_id, exc)
        return synced

    async def close(self) -> None:
        if self.pi:
            await self.pi.stop()
        await super().close()

    # -- events ------------------------------------------------------------

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("gateway up as %s (id %s)", self.user, self.user.id)
        log.info(
            "invite: %s",
            discord.utils.oauth_url(
                self.user.id,
                permissions=discord.Permissions(
                    send_messages=True, read_messages=True, read_message_history=True,
                    add_reactions=True,
                ),
            ),
        )
        # Wording matters here: until pipeline 292 this said the opposite ("anyone in the allowed
        # channels can approve a write"), which was true then and is a lie now. A log line that misstates
        # who can authorise a world wipe is worse than no log line, so it reads the mode rather than
        # assuming one.
        log.info(
            "approver mode: %s - %s",
            Config.approver_mode,
            "anyone in a permitted server and channel may ask and confirm" if Config.approver_mode != "allowlist"
            else "only MCBOT_APPROVER_IDS or MCBOT_APPROVER_ROLES may write; with both empty, nobody",
        )
        if Config.channels:
            log.info("mention channels: %s", sorted(Config.channels))
        # Which servers the bot is actually in, next to which ones it was told to serve. Every other
        # symptom of this mismatch - no replies, no errors, healthy pod - looks like a broken bot, and
        # this one line distinguishes "wrong invite" from "nothing is working".
        present = sorted(g.id for g in self.guilds)
        log.info("I am in %s; MCBOT_GUILDS is %s",
                 present or "no servers (nobody has accepted the invite link)",
                 sorted(Config.guild_ids) or "unset (any server)")
        missing = sorted(set(Config.guild_ids) - set(present))
        if missing:
            log.warning("MCBOT_GUILDS names %s but I am not in those servers, so I ignore everything in "
                        "them - a human in that server has to open the invite link", missing)
        unwanted = sorted(set(present) - set(Config.guild_ids))
        if unwanted and Config.guild_ids:
            log.warning("I am also in %s, which is not in MCBOT_GUILDS, so I ignore every message there", unwanted)
        await self._resume()

    async def on_message(self, msg: discord.Message) -> None:
        if not msg.guild:
            # A DM is the private surface, and the private surface is what wrecks a shared server: whoever
            # asked gets the answer and nobody else knows the question was even possible. Refused rather than
            # ignored, and rate limited per user - answering every DM is itself a private channel.
            author_id = getattr(msg.author, "id", 0)
            decision("refused", "direct message; mcbot answers in the Minecraft channels so everyone hears it",
                     guild=None, channel=getattr(msg.channel, "id", None), author=author_id,
                     text=(msg.content or "")[:400])
            now = time.time()
            if Config.public_only and dm_refusal_due(now, state.dm_last.get(author_id, 0.0), Config.dm_cooldown):
                state.dm_last[author_id] = now
                state.dm_pointers += 1
                await safe_send(msg.channel, DM_REFUSAL)
            return
        why = self._gate(msg)
        if why:
            decision("ignored", why, guild=getattr(msg.guild, "id", None),
                     channel=getattr(msg.channel, "id", None), author=getattr(msg.author, "id", None),
                     text=self._body(msg) or (msg.content or ""))
            return
        self._record(msg)
        text = self._body(msg)
        if not self._addressed(msg):
            decision("ignored", "does not mention me - @mcbot the member, or use /mc", guild=msg.guild.id,
                     channel=msg.channel.id, author=msg.author.id, text=text)
            return
        if not text:
            # Needs input, which is what the question mark is for. The follow-up goes in a thread: a
            # clarification from a 6-year-old should not sit in #general taking up channel air.
            await self._react(msg, "❓")
            await self._ask_in_thread(
                msg,
                "what do you want me to do? try \"server status\", \"who is online\', "
                "\"the last 10 lines of logs\" or /mc",
            )
            decision("replied", "mention with no question; asked back in a thread", guild=msg.guild.id,
                     channel=msg.channel.id, author=msg.author.id)
            return
        approver = can_approve(msg.author)
        readonly = is_readonly(text)
        if not (readonly or approver):
            decision("refused", "reads as a change request and the sender is not an approver",
                     guild=msg.guild.id, channel=msg.channel.id, author=msg.author.id, text=text,
                     readonly=readonly, approver=approver)
            await self._react(msg, "👎")
            await self._deliver(msg, REFUSALS[(state.counts["refused"] - 1) % len(REFUSALS)])
            return
        # Eyes on the ask itself: this replaces the `working:` message, which needed a whole message slot
        # to say "I'm on it" and produced the duplicate bodies that ate the real answer.
        await self._react(msg, "👀")
        decision("ask", "accepted", guild=msg.guild.id, channel=msg.channel.id, author=msg.author.id,
                 text=text, readonly=readonly, approver=approver)
        context = await self._context(msg)
        images = await self._images(msg)
        await self.handle(msg.channel, text, "", msg.author.id, approved=approver, msg=msg,
                          context=context, images=images)

    async def _deliver(self, msg: discord.Message, text: str) -> None:
        """Reply, and make it visible if Discord refused to deliver the reply."""
        try:
            await msg.reply(self._unique(getattr(msg.channel, "id", 0), text))
        except discord.HTTPException as exc:
            await self._react(msg, "❌")
            decision("error", f"reply not delivered: {exc}", guild=getattr(msg.guild, "id", None),
                     channel=getattr(msg.channel, "id", None), author=getattr(msg.author, "id", None),
                     text=text)

    # -- helpers -----------------------------------------------------------

    async def _react(self, msg: discord.Message, emoji: str) -> None:
        """Acknowledge an ask with a reaction instead of a message.

        The lifecycle Ed asked for, on the message that asked: 👀 when mcbot starts working, 👍 when the
        answer lands, 👎 when it declines, ❌ when it fails, ❓ when it needs something back. Reactions
        are the right carrier here: they do not consume a message slot, cannot collide with the answer,
        and a child can read the state of a request at a glance without scrolling prose.

        Needs the Add Reactions permission (bit 64, so the invite link asks for 68672 now). Without it
        Discord answers 50013 and mcbot continues - a missing emoji is cosmetic and must never cost the
        answer, so the failure is logged once and carried in healthz rather than raised.

        `msg` may be None: a slash command used normally has no message behind it (interaction.message is
        None unless the command came from a message's app menu), and that crashed /mc in the live pod with
        `'NoneType' object has no attribute 'add_reaction'`.
        """
        if msg is None or not hasattr(msg, "add_reaction"):
            return
        try:
            await msg.add_reaction(emoji)
        except Exception as exc:  # noqa: BLE001 - a reaction is decoration; it may never cost the answer
            state.reaction_error = f"{emoji}: {exc}"
            if not getattr(state, "reaction_warned", False):
                state.reaction_warned = True
                log.warning("cannot add reactions (%s) - mcbot needs the Add Reactions permission, which "
                            "means re-authorising with permissions=68672 or granting it to mcbot's role. "
                            "Replies are unaffected.", exc)

    async def _ask_in_thread(self, msg: discord.Message, text: str) -> None:
        """Ask for missing input in a thread, falling back to the channel if threads are not available."""
        try:
            thread = await msg.create_thread(name="what do you need?")
            await thread.send(text)
        except discord.HTTPException as exc:
            log.info("no thread for that clarification (%s), answering in the channel", exc)
            await self._deliver(msg, text)

    def _unique(self, channel_id: int, text: str) -> str:
        """Never send a body this channel has just been sent.

        Discord discards a message whose content matches one sent in the same channel moments earlier -
        `Duplicate message body, probably got already sent` - and this was not hypothetical: mcbot posted
        `working:` twice in #general and the answer that followed never appeared, which is indistinguish
        from a dead bot from outside. Bodies are tracked per channel and a repeat gets Discord's small-text
        footer, which reads as a timestamp rather than noise and makes the content unique.
        """
        recent = state.recent_bodies[channel_id]
        if text in recent:
            text = f"{text}\n-# mcbot {time.strftime('%H:%M:%S')}"
        recent.append(text)
        return text

    def _addressed(self, msg: discord.Message) -> bool:
        """Whether this message is aimed at mcbot.

        A member mention is the obvious case, and it is what the code used to accept - which turned out to
        be too narrow in practice. Six attempts to get an answer from the live server, three of them were
        `@&mcbot`: Discord's autocomplete offers the bot's *role* and children pick it, and a role never
        appears in `msg.mentions`, so mcbot watched those messages arrive, said nothing, and looked dead.

        So a mention of a **bot-managed role** counts as well. `Role.is_bot_managed()` is Discord's own
        "this role belongs to an application", which is what makes it mcbot's tag rather than any role a
        player happens to have; a server can have one and only the bot's integration carries it.
        """
        if self.user is not None and self.user in (msg.mentions or []):
            return True
        return any(r.is_bot_managed() for r in (msg.role_mentions or []))

    def _gate(self, msg: discord.Message) -> str:
        """Why this message is not for mcbot, or "" when it is. Named reason, because "no" on its own
        is what made the bot undebuggable."""
        if msg.author.bot:
            return "author is a bot"
        if not msg.guild:
            return "message is not in a guild"
        if Config.guild_ids and msg.guild.id not in Config.guild_ids:
            return (f"wrong server (message is in {msg.guild.id}, "
                    f"MCBOT_GUILDS is {sorted(Config.guild_ids)})")
        if Config.channels and msg.channel.id not in Config.channels:
            return f"channel {msg.channel.id} is not in MCBOT_CHANNELS"
        return ""

    def _permitted(self, msg: discord.Message) -> bool:
        return not self._gate(msg)

    @staticmethod
    def _body(msg: discord.Message) -> str:
        return MENTION_RE.sub("", msg.content or "").strip()

    @staticmethod
    def _record(msg: discord.Message) -> None:
        body = McBot._body(msg) or (msg.content or "").strip()
        if not body:
            return
        state.history[msg.channel.id].append(f"{msg.author.display_name}: {body[:400]}")

    async def _context(self, msg: discord.Message) -> list[str]:
        """What the channel has been saying, read from Discord rather than from our own memory.

        `state.history` only ever holds what mcbot personally saw since this pod started, which is why
        continuity worked right after a message and vanished after a restart or a rollout. Discord has the
        last N regardless of when mcbot booted, and `history(limit=N)` is a single request for N <= 100, so
        this costs one API call per ask rather than a scan.

        Attachments appear as a labelled placeholder here; the transcription pass (`_images`) is only run
        on the message being answered, so twenty past screenshots do not cost twenty vision calls.
        """
        key = getattr(msg.channel, "id", 0)
        if Config.context_messages <= 0:
            return list(state.history[key])[-Config.history_len:]
        out: list[str] = []
        self_kept = 0
        try:
            async for past in msg.channel.history(limit=Config.context_messages, before=msg):
                mine = bool(getattr(past.author, "bot", False))
                body = self._body(past) or (past.content or "").strip()
                if past.attachments:
                    tag = ", ".join((a.content_type or "attachment") for a in past.attachments[:3])
                    body = (body + " " if body else "") + f"[{len(past.attachments)} attachment(s): {tag}]"
                if not body:
                    continue
                if mine and any(w in body.lower() for w in Config.context_scratch):
                    # A retired policy sentence in this list is not history, it is an example, and a small
                    # instruct model performs examples. mcbot's own stale refusals were the one thing in
                    # #general most likely to be repeated back to the kids as policy.
                    continue
                if mine:
                    if self_kept >= Config.context_self_max:
                        continue
                    self_kept += 1
                    out.append(f"you said earlier: {body[:400]}")
                else:
                    out.append(f"{past.author.display_name}: {body[:400]}")
            out.reverse()
        except Exception as exc:  # noqa: BLE001 - a context read must never be the reason an ask is lost
            state.last_error = f"channel history read failed: {exc}"
            log.warning("could not read channel history (%s); using what I have seen", exc)
            return list(state.history[key])[-Config.history_len:]
        return out or list(state.history[key])[-Config.history_len:]

    async def _images(self, msg: discord.Message) -> list[str]:
        """Turn the pictures in this message into text for Pi.

        The work is farmed out to `MCBOT_VISION_URL` (any OpenAI-compatible vision endpoint), so mcbot
        needs no vision model of its own and the Pi RPC stays text-only: the agent gets a transcript, which
        is what a screenshot of a console or a crash is actually for. Everything about this path is allowed
        to fail: an unreadable image becomes a bracketed note in the prompt and the ask still gets answered.
        """
        attachments = list(msg.attachments or [])
        if not attachments:
            return []
        if not Config.vision_url:
            return [f"[{len(attachments)} attachment(s) in this message; vision is disabled]"]
        notes: list[str] = []
        for att in attachments[: Config.vision_max_images]:
            name = att.filename or "image"
            ctype = (att.content_type or "").lower()
            if not ctype.startswith("image/"):
                notes.append(f"[attachment {name}: {ctype or 'unknown type'}, not an image]")
                continue
            if att.size and att.size > Config.vision_max_bytes:
                notes.append(f"[image {name} is {att.size} bytes, over the {Config.vision_max_bytes} "
                             "limit - send a smaller screenshot]")
                continue
            try:
                async with ClientSession(timeout=ClientTimeout(total=Config.vision_timeout)) as session:
                    async with session.get(att.url) as r:
                        if r.status != 200:
                            notes.append(f"[could not fetch {name}: HTTP {r.status}]")
                            continue
                        blob = base64.b64encode(await r.read()).decode()
                    async with session.post(
                        Config.vision_url,
                        json={
                            "model": Config.vision_model,
                            "max_tokens": Config.vision_max_tokens,
                            "messages": [{
                                "role": "user",
                                "content": [
                                    {"type": "text", "text":
                                     "Describe this image in two sentences, then transcribe any visible "
                                     "text verbatim - log lines, error messages, chat, console output."},
                                    {"type": "image_url",
                                     "image_url": {"url": f"data:{ctype};base64,{blob}"}},
                                ],
                            }],
                        },
                    ) as v:
                        if v.status != 200:
                            notes.append(f"[vision service returned HTTP {v.status} for {name}]")
                            continue
                        data = await v.json()
                    described = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                described = described.strip()
                notes.append(f"[image {name}: {described[:1500]}]" if described
                             else f"[vision service returned nothing for {name}]")
            except Exception as exc:  # noqa: BLE001 - an unreadable image becomes a note, never a failed ask
                state.last_error = f"vision failed for {name}: {exc}"
                log.warning("vision failed for %s: %s", name, exc)
                notes.append(f"[image {name} could not be read: {exc}]")
        if len(attachments) > Config.vision_max_images:
            notes.append(f"[{len(attachments) - Config.vision_max_images} more attachment(s) not read]")
        return notes

    @staticmethod
    def _may_ask(author: discord.abc.User, text: str) -> bool:
        """Anyone may ask a pure status question; anything else needs an approver."""
        if is_readonly(text):
            return True
        return can_approve(author)

    # -- the run -----------------------------------------------------------

    async def handle(self, channel: discord.abc.Messageable, text: str, note: str, user_id: int,
                     approved: bool = False, msg: Optional[discord.Message] = None,
                     context: Optional[list[str]] = None, images: Optional[list[str]] = None,
                     readonly: bool = False) -> None:
        """Take an ask. If one is already running in this channel, queue it instead of refusing it.

        Three asks in a row is not misuse - one kid is impatient, one is adding a detail, one has changed
        their mind. Answering all three with "one thing at a time" made a working bot look broken and pushed
        the impatient one into asking twice more. So: `stop` aborts, a short follow-on that reads like a
        correction goes into the running turn as Pi steering (docs/rpc.md#steer), and everything else waits
        its turn in this channel's queue and is answered on its own.
        """
        key = getattr(channel, "id", 0)
        if key in state.busy:
            await self._while_busy(channel, key, text, note, user_id, approved, msg, readonly)
            return
        await self._run(channel, text, note, user_id, approved=approved, msg=msg, context=context,
                        images=images, readonly=readonly)

    async def _while_busy(self, channel: discord.abc.Messageable, key: int, text: str, note: str,
                          user_id: int, approved: bool, msg: Optional[discord.Message],
                          readonly: bool = False) -> None:
        if CANCEL_RE.match((text or "").strip()):
            await self._cancel(channel, key)
            return
        if getattr(channel, "ingame", False):
            await self._while_busy_game(channel, key, text, note, user_id, approved, readonly)
            return
        if msg is not None and self._is_modifier(text) and await self._steer(msg, text):
            return
        waiting = self.store.queue_count(key)
        if waiting >= Config.queue_max:
            await safe_send(channel, self._unique(
                key, f"my queue in here is full ({waiting} waiting) - say stop if you want them all dropped"))
            return
        self.store.queue_add(key, text, note, user_id, approved)
        decision("ask", f"queued behind the run in progress ({waiting + 1} waiting)",
                 guild=getattr(getattr(channel, "guild", None), "id", None), channel=key,
                 author=getattr(getattr(msg, "author", None), "id", None), text=text)
        if msg is not None:
            await self._react(msg, "👀")
        else:
            await safe_send(channel, self._unique(key, f"got it - that is number {waiting + 1} in the queue here"))

    @staticmethod
    def _is_modifier(text: str) -> bool:
        t = (text or "").strip()
        return bool(t) and len(t) <= Config.steer_max_len and bool(MODIFIER_RE.match(t))

    async def _steer(self, msg: discord.Message, text: str) -> bool:
        """Push a correction into the turn that is already running (docs/rpc.md#steer).

        Delivered after the current assistant turn finishes its tool calls, before the next LLM call, so the
        agent gets "use Paper, not Spigot" without losing the work it was doing. If Pi is not running (or the
        run settled a moment ago) this falls back to queueing, which is always safe.
        """
        if not (self.pi and self.pi.alive):
            return False
        who = getattr(getattr(msg, "author", None), "display_name", "someone")
        try:
            await self.pi.steer(f"{who} added while you were working: {text.strip()[:1200]}")
        except PiError as exc:
            log.warning("steer failed (%s); queueing the message instead", exc)
            return False
        decision("ask", "steered into the run in progress",
                 guild=getattr(getattr(msg.guild, "id", None), "id", None) if getattr(msg, "guild", None) else None,
                 channel=getattr(getattr(msg, "channel", None), "id", 0),
                 author=getattr(getattr(msg, "author", None), "id", None), text=text)
        await self._react(msg, "👀")
        return True

    async def _cancel(self, channel: discord.abc.Messageable, key: int) -> None:
        """Abandon the run in flight and drop everything waiting in this channel."""
        dropped = self.store.queue_drop(key)
        state.game_queue.pop(key, None)
        if self.pi and self.pi.alive:
            try:
                await self.pi.clear_queue()
            except PiError as exc:
                state.last_error = f"clear_queue: {exc}"
            try:
                await self.pi.abort()
            except PiError as exc:
                state.last_error = f"abort: {exc}"
        decision("refused", f"cancelled by the channel ({dropped} queued ask(s) dropped)", channel=key)
        await safe_send(channel, self._unique(
            key, "stopped. nothing waiting in this channel any more" if not dropped
            else f"stopped, and I dropped {dropped} waiting ask(s)"))

    async def _run(self, channel: discord.abc.Messageable, text: str, note: str, user_id: int,
                   approved: bool = False, msg: Optional[discord.Message] = None,
                   context: Optional[list[str]] = None, images: Optional[list[str]] = None,
                   readonly: bool = False) -> None:
        assert self.pi is not None
        key = getattr(channel, "id", 0)
        game = bool(getattr(channel, "ingame", False))
        state.busy.add(key)
        # If this pod is killed mid-ask, this row is the only record that an ask was in flight at all. In-game
        # asks are deliberately not written: there is nothing to resume into.
        if not game:
            self.store.run_note(key, text)
        activity: list[str] = []

        def on_activity(line: str) -> None:
            # Kept for the conversation history; the status *message* is gone. It was the duplicate-body
            # generator (`working:` twice in one channel) and a 👀 reaction on the ask says the same thing
            # for free, with 👍 / 👎 / ❌ closing the loop on the same message.
            activity.append(line)

        # An in-game ask is read-only whatever the caller passed, because that surface cannot prove who is
        # asking. The mode is written here, immediately before the ask: PiRpc.ask holds a lock, so only one
        # turn is ever in flight and a wrapper reading this file sees the turn it was called from.
        readonly = readonly or game
        told_the_gate = write_turn_mode(Config.turn_mode_path, readonly=readonly, approved=approved,
                                       surface="game" if game else "discord", text=text)
        if not told_the_gate:
            # The wrappers are what make "this turn is read-only" true. If the bot cannot tell them what turn
            # it is in, this turn gets no writes at all: a rule that silently stopped being enforced is worse
            # than one that fails visibly, and the visible failure is an empty `turn_mode` in /healthz.
            readonly, approved = True, False
        try:
            state.counts["asks"] += 1
            reply = await self.pi.ask(
                build_prompt(key, text, note, approved=approved, context=context, images=images,
                             store=self.store, readonly=readonly),
                timeout=Config.idle_timeout
            )
            await self.post(channel, reply, user_id)
            if msg is not None:
                await self._react(msg, "👍")
        except PiError as exc:
            state.counts["errors"] += 1
            state.last_error = f"{type(exc).__name__}: {exc}"
            log.error("pi failed: %s", exc)
            if msg is not None:
                await self._react(msg, "❌")
            await safe_send(channel, self._unique(
                getattr(channel, "id", 0), "that one broke on my side - try /mc or ask again"))
        except Exception as exc:  # noqa: BLE001 - a bad reply must never take the gateway down
            state.counts["errors"] += 1
            state.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("unexpected failure")
            if msg is not None:
                await self._react(msg, "❌")
            await safe_send(channel, self._unique(
                getattr(channel, "id", 0), "that one broke on my side - try /mc or ask again"))
        finally:
            state.busy.discard(key)
            if game:
                if state.game_queue.get(key):
                    asyncio.create_task(self._drain_game(key))
            else:
                self.store.run_clear(key)
                if self.store.queue_count(key):
                    # Drained as a task rather than recursively: a queued ask that explodes must not rewrite the
                    # outcome of the one that just answered, and must not unwind into its caller.
                    asyncio.create_task(self._drain(key))

    async def _drain(self, channel_id: int) -> None:
        """Take the next queued ask in this channel, if any and if nobody is already busy there."""
        if channel_id in state.busy:
            return
        channel = await self._channel_for(channel_id)
        if channel is None:
            dropped = self.store.queue_drop(channel_id)
            log.warning("dropped %s queued ask(s) for channel %s: that channel is not reachable any more",
                        dropped, channel_id)
            return
        nxt = self.store.queue_take(channel_id)
        if nxt is None:
            return
        await self._run(channel, nxt["text"], nxt["note"], nxt["user_id"], approved=bool(nxt["approved"]))

    async def _resume(self) -> None:
        """Pick up where a deploy cut us off.

        The rule that makes this safe is the same read/write split everything else here uses. A read-only ask
        that was cut off is safe to simply do again, so it is done. A write ask is not: the pod may have died
        after the commit and before the reply, and doing it again unasked is how you delete a world twice.
        Those come back as a fresh button, which is the only honest way to ask "did you mean to finish this?"
        """
        now = time.time()
        # Snapshot the open confirmations before anything is written. Without the snapshot, the button this
        # pass creates is picked up by the loop further down and the same offer is posted twice - which is
        # what happened on the first deploy of this code.
        open_confirms = self.store.confirm_active()
        for row in self.store.runs_interrupted():
            self.store.run_clear(row["channel_id"])
            channel = await self._channel_for(row["channel_id"])
            age = int(now - row["started"])
            if channel is None or age > Config.resume_max_age:
                decision("refused", f"dropped an ask cut off by a restart {age}s ago", channel=row["channel_id"],
                         text=row["text"])
                continue
            if is_readonly(row["text"]):
                decision("ask", f"resuming a read-only ask a restart cut off ({age}s ago)",
                         channel=row["channel_id"], text=row["text"])
                await safe_send(channel, self._unique(
                    row["channel_id"], "a restart cut me off in the middle of that one - carrying on with it"))
                await self._run(channel, row["text"], "", 0)
            else:
                pend = Pending(kind="change", detail=row["text"][:180], question=row["text"],
                               author_id=0, channel_id=row["channel_id"])
                pend.store_id = self.store.confirm_save(row["channel_id"], pend.kind, pend.detail, pend.question, 0)
                state.pending[row["channel_id"]] = pend
                decision("ask", f"a write ask was cut off {age}s ago; re-offered the button instead of repeating it",
                         channel=row["channel_id"], text=row["text"])
                await safe_send(channel, self._unique(
                    row["channel_id"], "a restart cut me off while I was changing something, so I do not know "
                    "whether it finished. nothing has been repeated - press the button only if you want it "
                    "done again"))
                await self._offer_confirm(
                    channel,
                    f"**I was cut off before finishing.** {escape(pend.detail)}\n"
                    "Press the button only if you want it done again.",
                    pend,
                )

        for row in [r for r in open_confirms if now - r["created"] <= Config.resume_max_age]:
            self.store.confirm_close(row["id"], "restarted")
            channel = await self._channel_for(row["channel_id"])
            if channel is None:
                continue
            pend = Pending(kind=row["kind"], detail=row["detail"], question=row["question"],
                           author_id=row["author_id"], channel_id=row["channel_id"])
            pend.store_id = self.store.confirm_save(row["channel_id"], row["kind"], row["detail"], row["question"],
                                                   row["author_id"])
            state.pending[row["channel_id"]] = pend
            decision("ask", "re-offered a confirmation button a restart left unanswered",
                     channel=row["channel_id"], text=row["question"])
            await self._offer_confirm(
                channel,
                f"**{escape(row['kind'].upper())} changes the server.** {escape(row['detail'])}\n"
                "the restart cut the button I offered you, so here it is again.",
                pend,
            )

        pruned = self.store.prune(Config.resume_max_age)
        if pruned:
            decision("refused", f"dropped {pruned} queued ask(s) older than {Config.resume_max_age}s after a restart")
        for channel_id in self.store.channels_with_queue():
            decision("ask", f"restored {self.store.queue_count(channel_id)} queued ask(s) after a restart",
                     channel=channel_id)
            asyncio.create_task(self._drain(channel_id))

    def confirm_rows_to_reoffer(self) -> list:
        """Open confirmations young enough that somebody might still mean them."""
        return [r for r in self.store.confirm_active() if time.time() - r["created"] <= Config.resume_max_age]

    async def _channel_for(self, channel_id: int):
        """Resolve a channel id recorded before this process existed.

        `get_channel` only knows what the gateway has cached, and threads are never in that cache - so an ask
        that came in inside a thread was reported as "dropped", which is a polite way of quietly losing it.
        """
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            log.info("channel %s is not reachable after a restart: %s", channel_id, exc)
            return None

    async def _offer_confirm(self, channel: discord.abc.Messageable, body: str, pend: Pending) -> None:
        """Send one confirmation offer and keep the message on the view.

        discord.py 2.7 has no `View.message`, so this is the only thing that lets a view grey its own buttons
        out later - on a press, on a refusal, or when the 300 seconds run out. Without it the buttons stay live
        on a message whose offer has already been spent.
        """
        view = ConfirmView(self, pend)
        message = await channel.send(body, view=view)
        view.target = message

    async def _watch_game(self) -> None:
        """Tail the server's own stdout and pick up `!mcbot ...` from Minecraft chat.

        Why the log rather than a plugin or a mod: the server already prints every chat line, so the capture is
        a read of something that exists, over a permission this pod already has (`pods/log` in the `minecraft`
        namespace), with nothing to install in the game and nothing in the game to keep compiled. The reply side
        is RCON, because writing into a player's chat is the one thing a log tail cannot do.

        `--tail=0` matters: a restarted pod must not answer an hour of old chat. The reconnect loop is because
        the server pod can roll underneath us while this one stays up.

        Read `CHAT_LINE_RE` strictly: a join or quit line has no `<name>`, a command a player issued has no
        closing `>`, and `[Server] ` - how the server prints a console or RCON `say` - is refused, which is what
        keeps this loop from answering its own broadcasts. `[Not Secure]`, which Paper puts in front of an
        unencrypted Java client's chat, is deliberately *not* special-cased: it marks the transport, not the
        line. ANSI cursor junk is stripped before the match, because the server console writes it.

        `MCBOT_INGAME_CONTAINER` is a container **id**, not the name the manifest gives the container. On this
        server the manifest says `minecraft` and the pod says `server`, and `kubectl logs -c minecraft` is
        refused outright: the tail then does not tail, `game_tailing` reports false, and every in-game ask is
        silently unanswered while the Discord half of the bot looks entirely healthy.
        """
        backoff = 5.0
        while not self.is_closed():
            proc: Optional[asyncio.subprocess.Process] = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "kubectl", "logs", Config.ingame_pod, "-n", Config.ingame_namespace,
                    "-c", Config.ingame_container, "--tail=0", "-f",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                self.game_tailing = True
                self.game_error = None
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    if self.is_closed():
                        break
                    line = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).rstrip()
                    match = CHAT_LINE_RE.search(line)
                    if not match:
                        continue
                    try:
                        await self._game_line(match.group("name"), match.group("text"))
                    except Exception as exc:  # noqa: BLE001 - one bad chat line must not end the tail
                        self.game_error = f"{type(exc).__name__}: {exc}"
                        log.exception("in-game ask failed to handle")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a log tail that dies must come back
                self.game_error = f"{type(exc).__name__}: {exc}"
                log.warning("game chat tail failed (%s); retrying in %.0fs", exc, backoff)
            finally:
                self.game_tailing = False
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            if self.is_closed():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    async def _game_line(self, player: str, text: str) -> None:
        """One chat line from one player. Most of them are not for mcbot."""
        text = (text or "").strip()
        if not text:
            return
        asked = ask_request(text, Config.ingame_triggers)
        if asked is None:
            return
        trigger, body = asked
        channel = GameChannel(self, player)
        key = channel.id
        state.game_names[key] = player
        state.game_seen += 1
        state.counts["ingame_asks"] += 1

        if INGAME_YES_RE.match(body) or INGAME_NO_RE.match(body):
            # `!mcbot yes` is what the old in-game offer taught players to type, so they will keep typing it.
            # A player who believes they approved something has to be disabused of it out loud, in the same
            # chat window where they typed it - silence here reads as "the yes worked and nothing happened".
            decision("refused", "in-game yes or no; in-game asks are read-only and nothing was pending",
                     channel=key, author=key, text=body)
            await safe_send(channel, "nothing was waiting for that. in game I only answer questions - "
                                     "changes are asked for in the Discord channel, where there is a button")
            return
        if not body:
            await safe_send(channel, f"what do you want? say it after {trigger}")
            return
        if CANCEL_RE.match(body):
            await self._cancel(channel, key)
            return
        if len(body) > Config.ingame_max_len:
            decision("refused", f"in-game ask too long for chat ({len(body)} chars)", channel=key,
                     author=key, text=body)
            await safe_send(channel, f"that is too long for chat - ask it in Discord and I can write a page")
            return
        since = time.time() - state.game_last.get(player, 0.0)
        if since < Config.ingame_cooldown and key not in state.busy:
            decision("refused", f"in-game cooldown ({since:.0f}s since the last ask)", channel=key,
                     author=key, text=body)
            await safe_send(channel, f"one at a time - give me about {int(Config.ingame_cooldown)}s")
            return
        state.game_last[player] = time.time()
        decision("ask", f"in-game ask from {player} via {trigger}", channel=key, author=key, text=body)
        # Read-only, and not by policy alone: `_run` re-derives readonly from the channel being an in-game
        # one, and the kubectl wrapper refuses the write verbs for that turn, so this line states the rule and
        # the wrapper is what makes it true. Approved is False for the same reason - a name in a chat window
        # proves the speaker can join the server and nothing else, so it cannot carry an approval.
        await self.handle(channel, body, f"asked from inside Minecraft by {player}", key,
                          approved=False, readonly=True)

    async def _while_busy_game(self, channel: GameChannel, key: int, text: str, note: str,
                               user_id: int, approved: bool, readonly: bool = False) -> None:
        waiting = len(state.game_queue.get(key) or [])
        if waiting >= Config.queue_max:
            await safe_send(channel, f"my queue is full ({waiting} waiting) - say stop to drop them all")
            return
        state.game_queue.setdefault(key, []).append((text, note, user_id, approved, readonly))
        decision("ask", f"queued in game behind the run in progress ({waiting + 1} waiting)",
                 channel=key, author=user_id, text=text)
        await safe_send(channel, f"got it - that is number {waiting + 1} in the queue here")

    async def _drain_game(self, key: int) -> None:
        """Next in-game ask for that player, if anybody is still waiting."""
        if key in state.busy:
            return
        waiting = state.game_queue.get(key) or []
        if not waiting:
            return
        text, note, user_id, approved, readonly = waiting.pop(0)
        if not waiting:
            state.game_queue.pop(key, None)
        await self._run(GameChannel(self, state.game_names.get(key, "player")), text, note, user_id,
                        approved=approved, readonly=readonly)

    async def game_say(self, player: str, content: str) -> None:
        """Answer into the game as a public broadcast, de-formatted and capped.

        `say` rather than `msg` is the fairness rule written as one line of code: a whisper is how one player
        gets an edge nobody else asked for, and it is an answer nobody else can see the bot give. The player
        argument stays because the log and the audit need to know whose ask this was, not because the reply
        goes to them alone.
        """
        payloads = self.game_broadcast_lines(content)
        if not payloads:
            return
        if self.rcon is None:
            self.game_error = "rcon is not configured (MCBOT_RCON_HOST / MCBOT_RCON_PASSWORD)"
            log.warning("cannot answer in game: %s", self.game_error)
            return
        sent = 0
        for payload in payloads[: Config.ingame_max_lines]:
            try:
                await self.rcon.command(payload)
                sent += 1
            except RconError as exc:
                self.game_error = f"{type(exc).__name__}: {exc}"
                log.warning("rcon send failed: %s", exc)
                break
        if sent == 0:
            log.warning("nothing reached the game (ask was from %s): %s", player, self.game_error or "no chunks")
        elif len(payloads) > Config.ingame_max_lines:
            # Said out loud, through the same clamp as everything else, so "no command is longer than a chat
            # line" stays true for the apology as well as the answer.
            for note in self.game_broadcast_lines("...the rest is too long for chat - ask me in Discord"):
                try:
                    await self.rcon.command(note)
                except RconError:
                    break

    @staticmethod
    def game_broadcast_lines(content: str) -> list[str]:
        """One answer, as the public RCON commands it becomes.

        One `say` per line: an RCON payload containing a newline is two commands, so the chunker's job is not
        only readability, it is keeping one answer inside one command. The name goes on the first line only so
        players can tell who is talking, and not on all six, because six lines shouting `mcbot:` is noise.
        Asserted in the tests: every payload starts with `say `, none contains `msg `, none contains a newline.
        """
        out: list[str] = []
        for index, chunk in enumerate(McBot._game_chunks(content)):
            out.append(f"say mcbot: {chunk}" if index == 0 else f"say {chunk}")
        return out

    @staticmethod
    def _game_chunks(text: str) -> list[str]:
        """One answer, cut into lines a Minecraft chat window can actually show.

        Markup is removed rather than escaped (a backslash is visible in game), and every newline is a cut point
        rather than something that survives into an RCON payload, where it would be a second command.
        """
        cleaned = strip_markdown(text or "").replace("\x00", "")
        chunks: list[str] = []
        for line in (part.strip() for part in re.split(r"\n+", cleaned) if part.strip()):
            current = ""
            for word in line.split():
                # A single word longer than a chat line (a long URL, a UUID) is cut, because the alternative is
                # a line that wraps in the client and hides the part the player needs to read.
                while len(word) > Config.ingame_line_len:
                    if current:
                        chunks.append(current)
                        current = ""
                    chunks.append(word[: Config.ingame_line_len])
                    word = word[Config.ingame_line_len:]
                if not current:
                    current = word
                elif len(current) + 1 + len(word) <= Config.ingame_line_len:
                    current += " " + word
                else:
                    chunks.append(current)
                    current = word
            if current:
                chunks.append(current)
        return chunks

    async def post(self, channel: discord.abc.Messageable, reply: PiReply, user_id: int) -> None:
        text = (reply.text or "").strip()
        if not text:
            await safe_send(channel, "I didn't get an answer out of that. ask it another way?")
            return
        m = CONFIRM_RE.search(text)
        if m:
            kind = m.group(1).strip().lower()
            detail = (m.group(2) or "").strip()
            before = text[: m.start()].strip()
            if before:
                await safe_send(channel, before)
            if getattr(channel, "ingame", False):
                # The marker arrives anyway - the model has read its own instructions and it sometimes offers
                # a change in game. It is refused in the same chat window it was offered in, so the player
                # hears what was on the table and where it would actually have happened. Nothing is queued,
                # nothing is pending, and no button exists for a later turn to act on.
                decision("refused", f"in-game ask wanted a {kind}; changes are asked for on Discord",
                         channel=channel.id, author=user_id, text=detail[:200] or kind)
                await safe_send(channel, f"that would {kind} something, and in game I only answer questions. "
                                         f"ask it in the Discord channel and I'll put the button up in front "
                                         f"of everybody")
                state.counts["refused"] += 1
                return
            pend = Pending(kind=kind, detail=detail, question=text, author_id=user_id,
                           channel_id=channel.id, ttl=Config.confirm_ttl)
            state.pending[channel.id] = pend
            pend.store_id = self.store.confirm_save(channel.id, kind, detail, text, user_id)
            await self._offer_confirm(
                channel,
                f"**{kind.upper()} changes the server.** {escape(detail)}\n"
                "Nothing has changed yet - press the button only if you meant this.",
                pend,
            )
            state.counts["confirmations"] += 1
            return
        for chunk in chunk_text(text):
            if not await safe_send(channel, chunk):
                break
        state.counts["replies"] += 1


def escape(text: str) -> str:
    return discord.utils.escape_markdown(text)


def strip_markdown(text: str) -> str:
    """Discord formatting removed rather than escaped, for a Minecraft chat window.

    `escape_markdown` is correct for Discord: it backslashes the special characters so they render as literal
    text. In game those backslashes render too, so an answer full of bullet points arrives as a wall of `\\*`.
    This deletes the markup instead of quoting it.
    """
    # Both spellings: chunk_text() already rewrites ``` to ''' before anything reaches a reply, because an
    # unterminated fence in a kids' channel renders as a wall of raw markup. A chat window has no fence to
    # close, so both forms are removed here rather than shown as three stray quotes.
    out = re.sub(r"^(?:```|''')[a-zA-Z0-9_+-]*$", "", text or "", flags=re.M)
    out = re.sub(r"^~~~[a-zA-Z0-9_+-]*$", "", out, flags=re.M)
    out = re.sub(r"`([^`]*)`", r"\1", out)
    out = re.sub(r"\*\*\*([^*]+)\*\*\*", r"\1", out)
    for pattern in (r"\*\*([^*]+)\*\*", r"\*([^*\n]+)\*", r"__([^_]+)__",
                    r"~~([^~]+)~~", r"\|\|([^|]+)\|\|"):
        out = re.sub(pattern, r"\1", out)
    out = re.sub(r"^#+\s*", "", out, flags=re.M)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", out)
    return out.strip()


async def safe_send(channel: discord.abc.Messageable, content: str) -> bool:
    try:
        await channel.send(content)
        return True
    except discord.HTTPException as exc:
        log.warning("send failed (%s): %s", type(exc).__name__, str(exc)[:200])
        return False


def self_state(store: Optional["Store"]) -> str:
    """Measured facts about mcbot's own condition, put in front of the model so it never has to guess them.

    After a rollout mcbot told the channel that "my internal state is a bit out of sync ... the mcbot state
    files appear missing". Nothing was missing - /data was intact, the pod was nine seconds old, and the
    model had no measurement available, so it invented a story about its own filesystem and told a child it
    as fact. Facts in, story out: this block is the only thing mcbot is allowed to say about its internals,
    and everything in it was read a moment ago.
    """
    db = store.stats(state.busy) if store else {"path": "not opened", "queued": {}, "interrupted": 0,
                                     "confirmations_open": 0, "error": "no store"}
    queued = sum(db["queued"].values()) if isinstance(db["queued"], dict) else 0
    audit = os.path.join(Config.audit_dir, "decisions.jsonl")
    bits = [
        f"this process has been up {int(time.time() - state.started)}s",
        f"state db {db['path']}: {'ok' if not db.get('error') else db['error']}",
        f"{queued} ask(s) queued", f"{db['interrupted']} run(s) interrupted by a restart",
        f"{db['confirmations_open']} confirmation(s) still open",
        f"workspace {Config.workspace}: {'present' if os.path.isdir(Config.workspace) else 'MISSING'}",
        f"audit log: {'present' if os.path.exists(audit) else 'MISSING'}",
        f"counters asks={state.counts['asks']} replies={state.counts['replies']} "
        f"refused={state.counts['refused']} errors={state.counts['errors']}",
    ]
    if state.last_error:
        bits.append(f"last error I actually saw: {state.last_error[:200]}")
    return ("[your own condition, measured just now: " + "; ".join(bits)
            + ". These are the only true things you may say about your own internals. Anything not in this "
            "list you have not measured: go run a command and look, or say nothing about it. Never describe "
            "a file you have not read." + "]")


def build_prompt(channel_id: int, text: str, note: str, approved: bool = False,
                 context: Optional[list[str]] = None, images: Optional[list[str]] = None,
                 store: Optional["Store"] = None, readonly: bool = False) -> str:
    """Compose the turn for Pi.

    `approved` is stated in the prompt rather than enforced by it. The enforcement is `_may_ask` and the
    wrapper; this line exists because a read-only turn still runs a session with write tools attached,
    and the model should know which kind of turn it is being asked for. Default is False so a call site
    that forgets the argument gets the restrictive prompt.
    """
    hist = context if context is not None else list(state.history[channel_id])[-Config.history_len:]
    lines = [self_state(store), ""]
    lines += ["Recent messages from this channel:"]
    lines += [f"- {h}" for h in hist] if hist else ["(none)"]
    lines.append("")
    if images:
        lines.append("What the pictures in that message actually contain (read by the vision service):")
        lines += [f"- {i}" for i in images]
        lines.append("")
    # Not a policy statement about the asker, because a small model treats one as a script to perform. It is
    # the one line in this prompt that names no forbidden sentence at all.
    lines.append("Never announce who is allowed to ask you. If something is not possible, say what is.")
    lines.append("")
    lines.append(capability_note())
    lines.append("")
    if readonly:
        # The one place the in-game rules are stated to the model. It is not the enforcement - the kubectl
        # wrapper refuses the write verbs for a read-only turn - but a model that does not know it is being
        # watched will cheerfully offer a change in a chat window that cannot hold a button.
        lines.append(IN_GAME_RULE)
        lines.append("")
    if not approved:
        lines.append(
            "This person is NOT on the approver allowlist. Answer with information only: do not run "
            "git commit/push, do not run kubectl delete/patch/apply/scale or exec that changes state, "
            "and do not emit a [[CONFIRM]] marker. If they want a change, say what it would do and that "
            "it needs the confirmation button."
        )
        lines.append("")
    if note:
        lines.append(note.strip())
        lines.append("")
    lines.append(f"{hist[-1].split(':', 1)[0] if hist else 'Someone'} just said to you: {text}")
    lines.append("")
    lines.append("Answer for a Minecraft chat window: plain text, short lines, no code fences, and everyone "
                 "on the server is reading it." if readonly else
                 "Answer for Discord: plain text, short, no code fences.")
    return "\n".join(lines)


CHUNK = 1800


def game_path_status(rcon_host: str, rcon_password: str) -> tuple[bool, str]:
    """Whether the in-game half can run, and the reason it cannot.

    Both halves or neither. A log tail with no RCON session takes a child's ask and cannot answer it, which
    reads to the child exactly like being ignored - and reads to whoever configured it as being on. So a
    missing password closes the whole half rather than opening the listening half, and says which setting is
    missing. `load_config` cannot catch this one: the password arrives from a Secret applied out of band, so
    a manifest can ship the trigger words and forget the credential, which is what happened on the first
    deploy of this feature.
    """
    missing = [name for name, value in (("MCBOT_RCON_HOST", rcon_host), ("MCBOT_RCON_PASSWORD", rcon_password))
               if not value]
    if missing:
        return False, "in-game asks are off: " + " and ".join(missing) + " are not set"
    return True, ""


def ask_request(text: str, triggers: tuple[str, ...]) -> Optional[tuple[str, str]]:
    """Split a game chat line into (trigger, question), or None when the line is not an ask.

    Prefix matching only, never a substring: two kids arguing about "that mcbot thing" are not asking
    anything, and a bot that answers anyway is noise in their chat. The question may be empty - someone
    typed a bare `!mcbot` - which the caller answers with a question of its own rather than treating as
    nothing, so `None` and `""` are deliberately different outcomes.

    A function rather than a few lines inside `_game_line`, so the rule can be asserted without a cluster,
    a server, or a Discord message, and so there is one place that decides it.
    """
    low = (text or "").strip().lower()
    for trigger in triggers:
        if low.startswith(trigger.lower()):
            return trigger, (text or "").strip()[len(trigger):].strip()
    return None


def chunk_text(text: str) -> list[str]:
    text = text.replace("```", "'''")  # a stray unterminated fence in a kids' channel is embarrassing
    if len(text) <= CHUNK:
        return [text]
    out: list[str] = []
    buf = ""
    for para in re.split(r"\n{2,}", text):
        if len(buf) + len(para) + 2 > CHUNK:
            if buf:
                out.append(buf.strip())
            while len(para) > CHUNK:
                out.append(para[:CHUNK].strip())
                para = para[CHUNK:]
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf.strip():
        out.append(buf.strip())
    return out


class ConfirmView(discord.ui.View):
    """The two buttons that stand between a phrasing and a destructive change.

    It keeps its own reference to the message it was sent with. discord.py 2.7 removed `View.message` - the
    attribute is simply not on the class any more - and the first version of this class reached for it in
    `_disable()`. That raised AttributeError on every press, which in `approve` happened *after* the pending was
    popped and the confirmation row closed but *before* the action ran: the button answered "this interaction
    failed", the change never happened, and pressing again said "that offer expired". The confirmation path is
    the one place where a bug here is indistinguishable from mcbot refusing to do something.
    """

    def __init__(self, bot: McBot, pend: Pending) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.pend = pend
        # Filled in by _offer_confirm() the moment the message exists.
        self.target: object = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.channel_id not in (self.pend.channel_id,):
            await interaction.response.send_message("That's not mine to answer here.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Do it", style=discord.ButtonStyle.danger)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not can_approve(interaction.user):
            await interaction.response.send_message("You're not allowed to approve that.", ephemeral=True)
            return
        # Answer the interaction first. Everything after this point is housekeeping, and housekeeping must not
        # be able to eat somebody's approval - that was the old order, and it did.
        await interaction.response.send_message(
            f"{escape(interaction.user.display_name)} approved - doing it now.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        pend = state.pending.pop(self.pend.channel_id, None)
        self.bot.store.confirm_close(self.pend.store_id, "pressed")
        await self._disable()
        if pend is None or pend.expired:
            await safe_send(interaction.channel, "That offer expired. Ask again.")
            return
        note = (
            f"A human ({interaction.user.display_name}) clicked the confirm button for the "
            f"{pend.kind} you described. Perform it now, exactly as described and nothing more."
        )
        await self.bot.handle(interaction.channel, pend.question.split("[[CONFIRM")[0].strip() or pend.kind,
                             note, interaction.user.id, approved=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            f"{escape(interaction.user.display_name)} said no. Nothing changed.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        state.pending.pop(self.pend.channel_id, None)
        self.bot.store.confirm_close(self.pend.store_id, "denied")
        await self._disable()

    async def on_timeout(self) -> None:
        # Only drop *our* offer. An earlier button timing out must not cancel a newer one in the same channel,
        # and `state.pending` is keyed by channel, so a bare pop() would do exactly that. The row closes either
        # way: a superseded offer left open would be re-offered by the next restart as a live button for a
        # decision somebody already moved on from.
        if state.pending.get(self.pend.channel_id) is self.pend:
            state.pending.pop(self.pend.channel_id, None)
        self.bot.store.confirm_close(self.pend.store_id, "expired")
        await self._disable()

    async def _disable(self) -> None:
        for item in self.children:
            try:
                item.disabled = True
            except Exception:  # noqa: BLE001
                pass
        target = self.target
        if target is None:
            # Nothing to edit: the offer never made it to Discord, or this view only ever got registered. Saying
            # so is better than a traceback out of a housekeeping path.
            log.info("confirm offer for channel %s had no message to grey out", self.pend.channel_id)
            return
        try:
            await target.edit(view=self)
        except discord.DiscordException as exc:
            log.info("could not grey out the confirm buttons in channel %s: %s", self.pend.channel_id, exc)


# --------------------------------------------------------------------------- health


def health_payload() -> dict:
    """What /healthz reports, as a plain function.

    It is a module-level function on purpose: the aiohttp handler has no instance to read, and the last time
    this dictionary was edited as though it did, every liveness probe in the cluster got a 500 out of
    `NameError: name 'self' is not defined`. Being a function also means it can be asserted on without a
    server running.
    """
    bot = client
    return {
        "up_seconds": round(time.time() - state.started),
        "gateway_ready": bool(bot and bot.is_closed() is False and bot.user),
        "pi_running": bool(bot and bot.pi and bot.pi.alive),
        "busy_channels": sorted(state.busy),
        "queued_asks": {str(k): v for k, v in (STORE.queue_counts() if STORE else {}).items()},
        "store": STORE.stats(state.busy) if STORE else {"error": "no store opened"},
        "pending_confirmations": len(state.pending),
        # Which surface rules are live, and what the kubectl wrapper was told about the turn in progress. If
        # "in game is read-only" ever looks untrue, this is the first place to look: it says what the bot
        # decided and what the wrapper was told, which are not the same thing.
        "public_only": Config.public_only,
        "capabilities": capabilities(),
        "turn_mode": {k: v for k, v in read_turn_mode(Config.turn_mode_path).items() if k != "text"},
        "dm_pointers": state.dm_pointers,
        "counts": dict(state.counts),
        "last_decision": state.last_decision,
        "approvers_configured": bool(Config.approvers or Config.approver_roles),
        "approver_mode": Config.approver_mode,
        # Which brain is answering, because "pin it to sushi so the desktop GPU stays free" is only a
        # change you can trust if the running pod will tell you what it is actually asking for.
        "pi_provider": Config.provider,
        "pi_model": Config.model,
        "reaction_error": state.reaction_error,
        "readonly_words": list(Config.readonly_words),
        # Whether the in-game path is alive, as two separate facts: the log tail that captures the ask, and the
        # RCON session that can answer it. A working capture with a dead RCON looks like a bot ignoring a child.
        "ingame": {
            "enabled": Config.ingame,
            "triggers": list(Config.ingame_triggers),
            "tailing": bool(client and client.game_tailing),
            "rcon_configured": bool(Config.rcon_host and Config.rcon_password),
            "rcon_connected": bool(client and client.rcon and client.rcon.connected),
            "rcon_commands": client.rcon.commands if (client and client.rcon) else 0,
            "asks_seen": state.game_seen,
            "players_queued": {state.game_names.get(k, str(k)): len(v)
                               for k, v in state.game_queue.items() if v},
            "error": client.game_error if client else None,
        },
        "last_error": state.last_error,
    }


async def healthz(_: web.Request) -> web.Response:
    """Never let a reporting bug take the bot out of rotation.

    /healthz is both the readiness and the liveness probe. A field that raises here would 500 the probe, pull
    the pod out of the service, and stop the bot answering children because of a bug in the *report* rather
    than in the bot. So the payload is guarded, and the failure is reported as the payload.
    """
    try:
        payload = health_payload()
    except Exception as exc:  # noqa: BLE001 - a broken metric must not be an outage
        state.last_error = f"healthz: {type(exc).__name__}: {exc}"
        log.exception("health payload failed")
        payload = {"error": f"{type(exc).__name__}: {exc}",
                   "up_seconds": round(time.time() - state.started),
                   "gateway_ready": False}
    return web.json_response(payload)


async def serve_health(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("health on :%d/healthz", port)
    return runner


async def slash_ask(bot: "McBot", interaction: discord.Interaction, question: str, origin: str) -> None:
    """Every slash command, in one place, into the same pipeline a mention uses.

    Two deliberate choices here. The commands are registered guild-only with `app_commands.guild_only()`, which
    is what this version of discord.py provides - there is no `dm_permission` to set on a tree registration -
    and yet the DM refusal below is still checked here rather than assumed, because a DM interaction reaching
    this code means the registration changed. The refusals are public rather than ephemeral for the same reason
    the answer is: a private "no" in a shared server is a rule only one person knows about.
    """
    guild_id = interaction.guild_id
    channel_id = interaction.channel_id
    author_id = getattr(interaction.user, "id", None)
    if not interaction.guild:
        decision("refused", f"{origin} outside a server; mcbot answers in the Minecraft channels",
                 guild=None, channel=channel_id, author=author_id, text=question)
        await interaction.response.send_message(DM_REFUSAL)
        return
    if Config.guild_ids and guild_id not in Config.guild_ids:
        decision("ignored", f"{origin} from the wrong server", guild=guild_id, channel=channel_id,
                 author=author_id, text=question)
        await interaction.response.send_message("Wrong server.")
        return
    if Config.channels and channel_id not in Config.channels:
        decision("ignored", f"{origin} in channel {channel_id}, not in MCBOT_CHANNELS", guild=guild_id,
                 channel=channel_id, author=author_id, text=question)
        await interaction.response.send_message("Use the Minecraft channels.")
        return
    if not McBot._may_ask(interaction.user, question):
        decision("refused", "reads as a change request and the sender is not an approver", guild=guild_id,
                 channel=channel_id, author=author_id, text=question,
                 readonly=is_readonly(question), approver=can_approve(interaction.user))
        await interaction.response.send_message(REFUSALS[(state.counts["refused"] - 1) % len(REFUSALS)])
        return
    decision("ask", f"{origin} accepted", guild=guild_id, channel=channel_id, author=author_id,
             text=question, readonly=is_readonly(question), approver=can_approve(interaction.user))
    await interaction.response.defer()
    state.history[channel_id].append(f"{interaction.user.display_name}: {question[:400]}")
    # Same lifecycle as a mention. A slash command with no message behind it gets no reaction, which is
    # fine: the deferred response is the ack.
    await bot._react(interaction.message, "👀")
    await bot.handle(interaction.channel, question, "", interaction.user.id,
                     approved=can_approve(interaction.user))


def main() -> None:
    global client
    load_config()
    client = McBot()

    # Guild-only, via the decorator this library version provides. `dm_permission=` reads like the obvious
    # kwarg and is not one `CommandTree.command()` accepts in discord.py 2.7, which is a startup TypeError and
    # a CrashLoopBackOff - the reason the build runs this registration through the real library.
    @client.tree.command(name="mc", description="Ask mcbot about the Minecraft server")
    @discord.app_commands.guild_only()
    @discord.app_commands.describe(question="What do you want to know or change?")
    async def mc(interaction: discord.Interaction, question: str) -> None:
        await slash_ask(client, interaction, question, "/mc")

    # The four things the boys actually want, without having to type them well. Each one is a canned question
    # handed to the same pipeline as a mention, so the tiering, the approval and the confirmation button are
    # the same code paths - `/restart-server` is not a privileged action, it is `restart the server` typed in
    # a channel, with a button in front of it.
    for name, description, canned in (
        ("server-status", "What the Minecraft server is doing right now",
         "server status: who is online, what version, how is the tick rate"),
        ("whitelist", "Who is on the whitelist", "who is on the whitelist?"),
        ("restart-server", "Restart the Minecraft server", "restart the minecraft server"),
        ("reload-server", "Reload the server config and whitelist", "reload the server config and whitelist"),
    ):
        def bind(canned: str = canned, origin: str = f"/{name}") -> Callable[..., Any]:
            async def command(interaction: discord.Interaction) -> None:
                await slash_ask(client, interaction, canned, origin)
            # Applied as a function rather than a decorator because these are registered in a loop. It sets the
            # same flag the decorator sets, and guild-only is what stops a slash command being used in a DM -
            # the same rule the mention path enforces by refusing to answer in one.
            return discord.app_commands.guild_only(command)

        client.tree.command(name=name, description=description)(bind())

    asyncio.run(_amain(client))


async def _amain(bot: McBot) -> None:
    if not Config.loaded:
        # Belt and braces: the defaults refuse everyone anyway, but name the actual reason instead of
        # leaving a running bot that silently declines every request.
        raise ConfigError("load_config() was never called; refusing to run on defaults")
    await serve_health(Config.health_port)
    # `log_handler=None` belongs on the constructor (see McBot.__init__). discord.py 2.x moved it out of
    # start(), and passing it here raised `Client.start() got an unexpected keyword argument 'log_handler'`
    # on the way to the gateway.
    await bot.start(Config.token)


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        # One clear line for whoever reads `kubectl logs`, rather than a traceback whose last frame is
        # an os.environ lookup.
        log.error("mcbot cannot start: %s", exc)
        raise SystemExit(1)
