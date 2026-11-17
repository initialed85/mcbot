#!/usr/bin/env python3
"""Policy self-test for bot.py's gates - no Discord, no cluster, no credentials.

    python3 containers/minecraft-bot/test-bot-policy.py

Why this exists as a separate file from the git-shim suite: the authorization rules are pure functions
over one module-level Config, so they are cheap to assert and were, until now, asserted nowhere. That
matters more than it looks - `can_approve()` used to return True when the approver lists were empty,
and the same function gates the tier-3 "Do it" button, so an unconfigured ConfigMap meant any member of
a server full of children could approve a world wipe.

discord and aiohttp are stubbed with MagicMock so the module imports without the image's dependency
set. Nothing here should be read as evidence the real Discord plumbing works; it is evidence the
decision function decides the right way.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
from unittest.mock import MagicMock

FAILED: list[str] = []
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name: str, got, want) -> None:
    if got == want:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name}: expected {want!r}, got {got!r}")
        FAILED.append(name)


def stub_modules() -> None:
    """Stand in for discord and aiohttp well enough to import bot.py and keep its classes real.

    MagicMock cannot be a base class: `class McBot(discord.Client)` on a Mock produces something whose
    own methods cannot be looked up, which silently turns a policy test into a test of the stub. So the
    pieces bot.py subclasses or decorates with are hand-written, and anything else falls through to a
    Mock via module __getattr__.
    """
    mod = types.ModuleType("discord")

    class Client:
        def __init__(self, intents=None, **kw):
            self.intents = intents
            self._closed = False

        def is_closed(self):
            # Real discord.py has this; health_payload reads it. A stub missing it turns a health check into an
            # AttributeError that looks like a broken bot.
            return self._closed

        async def start(self, *a, **k):
            raise AssertionError("stub Client.start called")

    class CommandTree:
        def __init__(self, client):
            self.client = client

        def error(self, coro):
            # discord.py 2.7.1 really does have this (dir(CommandTree) shows both `error` and `on_error`);
            # the stub needs it or `self.tree.error(...)` fails in the stub and only in the stub.
            self.on_error = coro
            return coro

        def command(self, **kw):
            def deco(fn):
                return fn
            return deco

    class View:
        def __init__(self, timeout=None, **kw):
            self.timeout = timeout
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class Button:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def button(**kw):
        def deco(fn):
            fn.__button_kwargs__ = kw
            return fn
        return deco

    def describe(**kw):
        def deco(fn):
            return fn
        return deco

    class Object:
        """discord.Object is what guild-scoped sync is handed, so the test can read the id back out."""

        def __init__(self, id=None, **kw):
            self.id = id

    ui = types.ModuleType("discord.ui")
    ui.View, ui.Button, ui.button = View, Button, button
    abc = types.ModuleType("discord.abc")
    abc.User, abc.Messageable = object, object
    ac = types.ModuleType("discord.app_commands")
    ac.describe, ac.Command = describe, object
    ac.CommandTree = CommandTree
    err = types.ModuleType("discord.errors")
    # DiscordException is the base, HTTPException the specific one, exactly as the real library nests them.
    # bot.py catches the base in the button housekeeping, so the stub has to make the same relationship or a
    # raised HTTPException sails straight through a test that is supposed to swallow it.
    err.DiscordException = type("DiscordException", (Exception,), {})
    err.HTTPException = type("HTTPException", (err.DiscordException,), {})
    utils = types.ModuleType("discord.utils")
    utils.escape_markdown = lambda text, **k: text
    utils.oauth_url = lambda client_id, **k: f"https://example.invalid/{client_id}"

    mod.__dict__.update(
        Client=Client,
        Intents=types.SimpleNamespace(none=lambda: types.SimpleNamespace(
            guilds=False, messages=False, message_content=False)),
        ButtonStyle=types.SimpleNamespace(danger="danger", secondary="secondary"),
        AllowedMentions=types.SimpleNamespace(none=lambda **k: None),
        Embed=type("Embed", (), {}),
        Activity=type("Activity", (), {}),
        Game=type("Game", (), {}),
        Interaction=object,
        Message=object,
        Object=Object,
        # discord.py re-exports its errors at the top level and bot.py catches `discord.HTTPException`.
        # A catch-all module __getattr__ would hand that name a Mock, and `except <Mock>` raises
        # TypeError instead of catching, so the name has to be real here too.
        HTTPException=err.HTTPException,
        DiscordException=err.DiscordException,
        Permissions=types.SimpleNamespace(),
        ui=ui, abc=abc, app_commands=ac, errors=err, utils=utils,
    )
    # A catch-all __getattr__ that hands out a Mock for any name is exactly how this suite failed to
    # catch the startup crash: bot.py used `discord.CommandTree`, which discord.py 2.7 does not export,
    # and the stub produced one happily. Names the real library does not have are refused here, so a
    # wrong attribute fails the way the library fails it - at import/construct time, not in a pod at
    # 03:00. `CommandTree` is the one that actually bit; the rest are the same family of plausible lie.
    NOT_IN_DISCORD = ("CommandTree", "SlashCommand", "Cog", "slash", "client")

    def module_getattr(name: str):
        if name in NOT_IN_DISCORD:
            raise AttributeError(f"module 'discord' has no attribute {name!r}")
        return MagicMock(name=f"discord.{name}")

    mod.__getattr__ = module_getattr

    aio = types.ModuleType("aiohttp")
    aio.web = MagicMock(name="aiohttp.web")

    # bot.py imports these three for the vision path. They are hand-written rather than left to the
    # module __getattr__ so a test cannot pass by accidentally mocking a real call away.
    class ClientError(Exception):
        pass

    aio.ClientError = ClientError
    aio.ClientSession = MagicMock(name="aiohttp.ClientSession")
    aio.ClientTimeout = MagicMock(name="aiohttp.ClientTimeout")

    for name, m in (("discord", mod), ("discord.ui", ui), ("discord.abc", abc),
                    ("discord.app_commands", ac), ("discord.errors", err),
                    ("discord.utils", utils), ("aiohttp", aio)):
        sys.modules[name] = m


# Fixture values for the variables mcbot insists on. Nothing here is a real guild, channel, user, host or
# repository: an id that looks real in a public test suite is somebody's deployment published as documentation.
FIXTURE_ENV = {
    "MCBOT_GUILDS": "111111111111111111",
    "MCBOT_CHANNELS": "222222222222222222",
    "MCBOT_REPO_URL": "git@example.invalid:someone/manifests.git",
    "MCBOT_ALLOWED_PREFIX": "apps/minecraft/",
    "MCBOT_PI_BASE_URL": "http://inference.example.invalid/v1",
    "MCBOT_INGAME_NAMESPACE": "games",
    "MCBOT_INGAME_POD": "paper-0",
    "MCBOT_RCON_HOST": "127.0.0.1",
    "MCBOT_RCON_PASSWORD": "fixture-rcon",
}


class FakeUser:
    def __init__(self, uid: int, roles=()):
        self.id = uid
        self.roles = [types.SimpleNamespace(id=r) for r in roles]
        self.display_name = f"user{uid}"


def reset_config() -> None:
    """Return Config to the inert defaults, so each case starts from a known state.

    Approver mode is pinned to `allowlist` here on purpose: sections 1-5 are the gate tests written
    against allowlist semantics (empty lists refuse everyone). Leaving them on the deployed default of
    `anyone` made every "this must be refused" check pass for the wrong reason - can_approve said yes to
    everyone - which is the same vacuous-pass trap as the leftover approver ids. The code default itself
    is asserted separately, with the variable unset.
    """
    import bot
    for key in list(os.environ):
        if key.startswith("MCBOT_") or key in ("DISCORD_BOT_TOKEN", "PI_PROVIDER", "PI_MODEL"):
            del os.environ[key]
    # A deployment that is configured but has named nobody: the approver lists stay empty, which is the state
    # sections 1-5 are about. Everything else is filled in with fixture values because mcbot now refuses to
    # start at all without them - a suite that wants to argue about *who may approve* has to get past the
    # startup check first, and the startup check itself is tested in test-config.py.
    os.environ["DISCORD_BOT_TOKEN"] = "test-token"
    os.environ["MCBOT_APPROVER_MODE"] = "allowlist"
    os.environ.update(FIXTURE_ENV)
    bot.load_config()


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    stub_modules()
    import bot
    # --- 1. unconfigured allowlist refuses everyone -------------------------------
    reset_config()
    os.environ.pop("MCBOT_APPROVER_MODE", None)
    bot.load_config()
    check("the code default is an allowlist, so an unconfigured deployment authorises nobody",
          bot.Config.approver_mode, "allowlist")
    # `anyone` is a deployment choice, and it is a real one: a family server where the children run the game
    # and the hosting is somebody else's is exactly the case an age gate would get wrong. Asserted here
    # because the mode is a switch, and a switch nobody tests in both positions is a rumour.
    os.environ["MCBOT_APPROVER_MODE"] = "anyone"
    bot.load_config()
    check("anyone mode: a stranger in a permitted channel may authorise a write",
          bot.can_approve(FakeUser(4, roles=[])), True)
    os.environ.pop("MCBOT_APPROVER_MODE", None)
    bot.load_config()
    reset_config()
    check("empty approvers -> Config.approvers is empty", bot.Config.approvers, set())
    check("empty approvers: admin cannot approve", bot.can_approve(FakeUser(1)), False)
    check("empty approvers: a child cannot approve", bot.can_approve(FakeUser(2)), False)
    check("empty approvers: cannot approve by role", bot.can_approve(FakeUser(3, roles=[99])), False)
    check("write refused when unconfigured",
          bot.McBot._may_ask(FakeUser(1), "wipe the world and restart"), False)
    check("read-only still allowed when unconfigured",
          bot.McBot._may_ask(FakeUser(1), "status"), True)
    check("read-only phrasing is matched on the prefix",
          bot.McBot._may_ask(FakeUser(1), "who is online?"), True)

    # A read-only word used as the front of a write request is the obvious way past a `startswith`
    # allowlist, and this is exactly the case the old classifier let through. Each of these must now
    # need an approver.
    for attempt in (
        "logs; now delete the world",
        "status then restart the server",
        "who is online and can you bump paper",
        "please wipe the world",
        "delete the nether",
        "change the servermotd",
        "restart it",
        "git commit the change",
        "kubectl delete pod minecraft-0",
    ):
        check(f"not treated as read-only: {attempt!r}",
              bot.McBot._may_ask(FakeUser(1), attempt), False)

    reset_config()
    for phrase in ("status", "is it up", "who is online", "logs", "what version are we on",
                   "ping", "uptime, please"):
        check(f"genuine status phrase allowed: {phrase!r}",
              bot.McBot._may_ask(FakeUser(1), phrase), True)

    # --- 1b. the prompt tells the model which kind of turn it is -------------------
    state = bot.State()
    bot.state = state
    check("prompt marks an unapproved asker",
          "NOT on the approver allowlist" in bot.build_prompt(0, "status", "", approved=False), True)
    check("prompt does not restrict an approver by default",
          "NOT on the approver allowlist" in bot.build_prompt(0, "bump paper", "", approved=True), False)
    check("approved defaults to the restrictive prompt",
          "NOT on the approver allowlist" in bot.build_prompt(0, "status", ""), True)

    # --- 2. configured: exactly the listed ids ------------------------------------
    os.environ["MCBOT_APPROVER_IDS"] = "1001,1002"
    os.environ["MCBOT_APPROVER_ROLES"] = "55"
    bot.load_config()
    check("listed user may approve", bot.can_approve(FakeUser(1001)), True)
    check("unlisted user may not", bot.can_approve(FakeUser(2002)), False)
    check("role holder may approve", bot.can_approve(FakeUser(3003, roles=[55])), True)
    check("wrong role may not", bot.can_approve(FakeUser(3003, roles=[56])), False)
    check("unlisted user cannot ask for a write",
          bot.McBot._may_ask(FakeUser(2002), "bump paper to build 254"), False)
    check("listed user can ask for a write",
          bot.McBot._may_ask(FakeUser(1001), "bump paper to build 254"), True)

    # --- 3. junk in the id list is dropped, not half-loaded -----------------------
    os.environ["MCBOT_APPROVER_IDS"] = "1001, ,not-a-number,1002"
    bot.load_config()
    check("non-numeric ids dropped", bot.Config.approvers, {1001, 1002})

    # --- 4. the confirmation marker the whole tier-3 flow depends on --------------
    m = bot.CONFIRM_RE.search("I would wipe it.\n[[CONFIRM wipe: replaces the overworld, builds lost]]")
    check("confirm marker parsed", bool(m), True)
    if m:
        check("confirm kind", m.group(1).strip(), "wipe")
        check("confirm detail", (m.group(2) or "").strip(), "replaces the overworld, builds lost")
    check("confirm marker in prose without brackets is not a confirmation",
          bool(bot.CONFIRM_RE.search("I will [[CONFIRM]] not do that")), False)

    # --- 5. mention matching must not depend on Discord's current wire form -------
    for form, want in (("<@123>", True), ("<@!123>", True), ("<@#mcbot>123", False)):
        check(f"mention regex on {form}", bool(bot.MENTION_RE.search(form)), want)

    # --- 6. missing required env must be a clean ConfigError ----------------------
    del os.environ["DISCORD_BOT_TOKEN"]
    try:
        bot.load_config()
        check("load_config without a token raises", "no error", "ConfigError")
    except bot.ConfigError as exc:
        check("load_config without a token raises ConfigError", "DISCORD_BOT_TOKEN" in str(exc), True)

    # --- 7. the stub must be no more permissive than the library it replaces -----
    # The AttributeError that killed the bot was invisible here for one reason: the stub had the name
    # the code wanted. These checks keep the stub honest and pin the two ssh/Git mistakes from the same
    # deploy to source text, because neither is visible to a test of decision functions.
    import discord as stub
    check("CommandTree lives in discord.app_commands", hasattr(stub.app_commands, "CommandTree"), True)
    try:
        stub.CommandTree
        check("stub refuses discord.CommandTree", "handed out a Mock", "AttributeError")
    except AttributeError:
        check("stub refuses discord.CommandTree", "AttributeError", "AttributeError")

    def source(*parts) -> str:
        with open(os.path.join(HERE, *parts), encoding="utf-8") as fh:
            return fh.read()

    def code(text: str) -> str:
        """Drop comment lines. These files carry their own post-mortems, and a text guard that reads them
        as code reports the explanation as the defect - which is how this check first failed, on the
        sentence describing the ssh option it was looking for.
        """
        return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))

    bot_src = source("bot.py")
    check("bot.py builds the tree from discord.app_commands",
          "discord.app_commands.CommandTree(" in bot_src, True)
    check("bot.py never names the nonexistent discord.CommandTree",
          "discord.CommandTree(" in bot_src, False)
    # The startup crash this suite could not see: `dm_permission` reads like the obvious kwarg for "not in DMs"
    # and discord.py 2.7's `CommandTree.command()` does not accept it. The image's smoke test is what catches
    # signature drift, so the static guard here is that the mistake is not back in the source and that the
    # smoke test still inspects the tree.
    # Not a text search: bot.py *explains* this mistake in a docstring and a comment, and a guard that reads an
    # explanation as a defect reports the post-mortem as the bug. What is checked is the call sites themselves -
    # the same instrument the image's smoke test applies against the live library, here without the library.
    tree_kwargs = set()
    for node in ast.walk(ast.parse(bot_src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "command" and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "tree"):
            tree_kwargs.update(k.arg for k in node.keywords if k.arg)
    check("the slash registrations pass only keywords CommandTree.command accepts",
          tree_kwargs and tree_kwargs <= {"name", "description"}, True)
    check("slash commands are guild-only through the mechanism this library version provides",
          code(bot_src).count("app_commands.guild_only") >= 2, True)
    check("the image's smoke test inspects command-tree call sites, not only client methods",
          "CommandTree" in open(os.path.join(HERE, "test-image-smoke.py"), encoding="utf-8").read(), True)

    entry = source("entrypoint.sh")
    dockerfile = source("Dockerfile")
    manifest = source("deploy", "example", "deployment.yaml")
    for name, text in (("entrypoint.sh", entry), ("Dockerfile", dockerfile), ("deployment.yaml", manifest)):
        check(f"{name} uses no invented ssh option 'reject_hosts'", "reject_hosts" in code(text), False)
    check("entrypoint pins StrictHostKeyChecking=yes", "StrictHostKeyChecking=yes" in code(entry), True)
    check("deploy key mount is 0440, readable by the pod's uid", "defaultMode: 0440" in code(manifest), True)
    check("the root filesystem is read-only, so the agent cannot edit the code that gates it",
          "readOnlyRootFilesystem: true" in code(manifest), True)
    mounts = [l.split(":", 1)[1].strip() for l in code(manifest).splitlines() if "mountPath:" in l]
    check("exactly two mounts: the writable data volume and the read-only deploy key",
          sorted(mounts), ["/data", "/mnt/git-key"])
    check("and the only writable one is MCBOT_DATA",
          [m for m in mounts if m != "/data"], ["/mnt/git-key"])
    check("the deploy key mount is readOnly", "readOnly: true" in code(manifest), True)
    check("the gateway is not allowed to roll two copies over one queue",
          "type: Recreate" in code(manifest), True)
    check("entrypoint copies the key to 0600 before ssh reads it",
          'chmod 600 "$SSH_DIR/id_ed25519"' in code(entry), True)

    # --- 7b. the sandbox is assembled in the image, so assert the image assembles it -------
    # bin/gate.py is only a file until the image puts it on PATH in front of the binaries it governs. Every one
    # of these checks is about a failure that would leave the pod perfectly healthy: a COPY that misses a
    # wrapper, a chmod that misses an +x, a link written where PATH will not look, a wrapper pointed at itself
    # instead of the binary it wraps.
    docker_raw = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    check("the image copies bin/ whole rather than one wrapper at a time",
          "COPY bin/ /opt/mcbot/bin/" in docker_raw, True)
    chmod_block = " ".join(docker_raw.split())
    for wrapper in ("bin/git-shim", "bin/kubectl-shim", "bin/mc-rcon", "bin/gate.py"):
        check(f"the image chmods {wrapper}", f"/opt/mcbot/{wrapper}" in chmod_block, True)
    check("the image asserts the gate at build time", "python3 /opt/mcbot/test-gate.py" in docker_raw, True)
    check("the image asserts what PATH resolves to, after the links exist",
          "python3 /opt/mcbot/test-image-smoke.py --path" in docker_raw, True)
    # The same shape for both ways out of the game: the name on PATH is the wrapper, the wrapper is told where
    # the real binary lives, and the real binary is not installed where the wrapper is.
    check("kubectl on PATH is the wrapper",
          "ln -sf /opt/mcbot/bin/kubectl-shim /usr/local/bin/kubectl" in docker_raw, True)
    check("the real kubectl is installed out of the wrapper's way",
          ("-o /usr/bin/kubectl" in docker_raw, "/usr/local/bin/kubectl && chmod" not in docker_raw), (True, True))
    check("the wrapper is told the absolute path of the real kubectl",
          "ENV MCBOT_KUBECTL_REAL=/usr/bin/kubectl" in docker_raw, True)
    check("mc-rcon on PATH is the gate-checked wrapper",
          "ln -sf /opt/mcbot/bin/mc-rcon /usr/local/bin/mc-rcon" in docker_raw, True)
    check("git on PATH is still the wrapper (the original sandbox is not silently dropped)",
          ("ln -sf /opt/mcbot/bin/git-shim /usr/local/bin/git" in docker_raw,
           "ENV MCBOT_GIT_REAL=/usr/bin/git" in docker_raw), (True, True))
    smoke_raw = open(os.path.join(HERE, "test-image-smoke.py"), encoding="utf-8").read()
    check("the image's smoke test asserts each wrapper is what PATH finds",
          "def check_path_wiring()" in smoke_raw and 'shutil.which(name)' in smoke_raw, True)
    check("and it asserts the real binary is not the wrapper (recursion guard)",
          "recursion guard" in smoke_raw, True)
    check("entrypoint.sh resets the turn record to read-only and unapproved on every start",
          ('"approved":false,"readonly":true' in entry, "MCBOT_TURN_MODE" in entry), (True, True))
    config_map = source("deploy", "example", "configmap.yaml")
    check("the capability switches are declared in the ConfigMap, where a reader can see them",
          all(k in config_map for k in ("MCBOT_SERVER_ACTIONS", "MCBOT_INGAME_ACTIONS",
                                        "MCBOT_PLUGIN_ALLOWLIST", "MCBOT_TURN_MODE")), True)
    check("and declared with the safe values: in-game actions off, no plugin jar allowed",
          ('MCBOT_INGAME_ACTIONS: "0"' in config_map, 'MCBOT_PLUGIN_ALLOWLIST: ""' in config_map), (True, True))

    # --- 8. every message leaves a trace, including the ones mcbot declines -------
    # Behavioural on purpose. The static guards above could not see the complaint that started this:
    # "not much in the pod logs". Four paths in on_message returned without writing a single line -
    # wrong server, channel outside MCBOT_CHANNELS, no real mention, and a refused change request - so
    # a bot doing exactly what it was configured to do was indistinguishable from one that was dead.
    audit_dir = os.path.join(tempfile.mkdtemp(prefix="mcbot-audit-"), "audit")
    os.environ["DISCORD_BOT_TOKEN"] = "test-token"  # section 6 removed it on purpose
    # The live ConfigMap has both approver lists empty, and sections 2-3 filled them. Model the state
    # the deployed bot is actually in, or "refused" cases get approved by leftover ids and pass vacuously.
    os.environ.pop("MCBOT_APPROVER_IDS", None)
    os.environ.pop("MCBOT_APPROVER_ROLES", None)
    # Section 8 is about the allowlist gate refusing, so it has to run in allowlist mode. Deployed
    # mode is `anyone` (section 10), which would otherwise approve everything and make these checks
    # pass by accident.
    os.environ["MCBOT_APPROVER_MODE"] = "allowlist"
    os.environ["MCBOT_GUILD_ID"] = "111"
    os.environ["MCBOT_CHANNELS"] = "222,333"
    os.environ["MCBOT_AUDIT_DIR"] = audit_dir
    bot.load_config()
    bot.state = bot.State()

    seen: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    bot.log.setLevel(logging.INFO)
    bot.log.addHandler(Capture())

    b = bot.McBot()
    b.user = types.SimpleNamespace(id=999, display_name="mcbot")
    handled: list[str] = []

    async def fake_handle(channel, text, note, user_id, approved=False, msg=None, context=None,
                          images=None):
        handled.append(text)

    b.handle = fake_handle

    THREADS: list = []

    class FakeMsg:
        def __init__(self, content, *, guild=111, channel=222, mentions=True, author_bot=False,
                     role_mentions=None, attachments=None):
            self.content = content

            async def _channel_send(content=None, **kw):
                self.sent.append(content or "")

            self.channel = types.SimpleNamespace(id=channel, send=_channel_send)
            self.author = types.SimpleNamespace(id=1001, display_name="ed", bot=author_bot)
            self.guild = types.SimpleNamespace(id=guild) if guild is not None else None
            self.mentions = [b.user] if mentions else []
            self.role_mentions = role_mentions or []
            self.attachments = attachments or []
            self.sent: list[str] = []
            self.reactions: list[str] = []

        async def reply(self, content=None, **kw):
            self.sent.append(content or "")

        async def add_reaction(self, emoji):
            self.reactions.append(emoji)

        async def create_thread(self, *, name=""):
            thread = types.SimpleNamespace(id=999, name=name, sent=[])

            async def send(content=None, **kw):
                thread.sent.append(content or "")
                return thread

            thread.send = send
            THREADS.append(thread)
            return thread

    class BoomMsg(FakeMsg):
        """Discord refusing to deliver identical content twice in a row."""

        async def reply(self, content=None, **kw):
            raise stub.HTTPException("Duplicate message body, probably got already sent")

    def run(msg) -> dict:
        seen.clear()
        asyncio.run(b.on_message(msg))
        return bot.state.last_decision or {}

    d = run(FakeMsg("server status", guild=999))
    check("wrong-server message is recorded", d.get("kind"), "ignored")
    check("wrong-server reason names both ids", "wrong server" in d.get("reason", ""), True)
    check("wrong-server message produced a log line", len(seen) > 0, True)

    d = run(FakeMsg("server status", channel=777))
    check("channel outside MCBOT_CHANNELS is recorded as such",
          "MCBOT_CHANNELS" in d.get("reason", ""), True)

    dm = FakeMsg("how do i get diamonds?", guild=None)
    d = run(dm)
    check("a direct message is refused rather than silently ignored", d.get("kind"), "refused")
    check("the refusal is a directive about where to ask, sent in the DM",
          any("Minecraft channels" in s for s in dm.sent), True)
    check("it is counted, so /healthz can show how often people try",
          bot.state.dm_pointers, 1)
    dm2 = FakeMsg("hello?", guild=None)
    d2 = run(dm2)
    check("a second DM in a row is still recorded", d2.get("kind"), "refused")
    check("and it is not answered again: a bot that replies to every DM has a private channel",
          bot.state.dm_pointers, 1)
    check("the DM path never reaches the model", bot.state.counts["asks"], 0)

    d = run(FakeMsg("is the server up?", mentions=False))
    check("a message with no real mention is recorded, not dropped",
          d.get("kind"), "ignored")
    check("and the reason says how to get through", "mention" in d.get("reason", ""), True)

    texts: list[str] = []
    for _ in range(3):
        m = FakeMsg("wipe the world")
        run(m)
        texts += m.sent
    check("refused change request is recorded", bot.state.last_decision["kind"], "refused")
    check("refusal was delivered every time", len(texts), 3)
    check("consecutive refusals differ, so Discord cannot discard them as duplicates",
          len(set(texts)), 3)
    check("a refusal offers a question that does work", "server status" in " ".join(texts[1:]), True)

    m = FakeMsg("is the server up?")
    run(m)
    check("a plain status question is read-only (it was refused before this)",
          bot.state.last_decision["kind"], "ask")
    check("and it reached the Pi bridge", handled[-1], "is the server up?")

    m = BoomMsg("wipe the world")
    run(m)
    check("an undeliverable reply is recorded instead of vanishing",
          bot.state.last_decision["kind"], "error")
    check("the failure names itself", "not delivered" in bot.state.last_decision["reason"], True)
    check("undeliverable replies show up in the healthz counts", bot.state.counts["error"], 1)

    lines = open(os.path.join(audit_dir, "decisions.jsonl"), encoding="utf-8").read().splitlines()
    kinds = sorted({json.loads(line)["kind"] for line in lines})
    check("decisions.jsonl line count matches the decisions made", len(lines),
          sum(bot.state.counts.values()))
    check("decisions.jsonl kinds", kinds, ["ask", "error", "ignored", "refused"])

    check("healthz reports the last decision and whether an allowlist exists at all",
          ("last_decision" in bot_src, "approvers_configured" in bot_src), (True, True))

    # --- 9. which servers it serves, and how commands get registered there -------
    os.environ.pop("MCBOT_GUILDS", None)
    os.environ["MCBOT_GUILD_ID"] = "111"
    bot.load_config()
    check("MCBOT_GUILD_ID alone still means one server", bot.Config.guild_ids, {111})

    os.environ["MCBOT_GUILDS"] = "111,222"
    os.environ.pop("MCBOT_GUILD_ID", None)
    bot.load_config()
    check("MCBOT_GUILDS takes a list", bot.Config.guild_ids, {111, 222})

    d = run(FakeMsg("server status", guild=222))
    check("a message in the second configured server is accepted", d.get("kind"), "ask")
    check("and it reached the Pi bridge", handled[-1], "server status")

    os.environ["MCBOT_GUILDS"] = "111"
    bot.load_config()

    sync_calls: list = []

    async def fake_sync(*, guild=None):
        sync_calls.append(getattr(guild, "id", None))
        return []

    b.tree.sync = fake_sync
    asyncio.run(b._sync_commands())
    check("slash commands sync into the configured server, not globally", sync_calls, [111])

    bot.Config.guild_ids = {111, 222}
    sync_calls.clear()
    asyncio.run(b._sync_commands())
    check("every configured server gets its own guild-scoped sync", sync_calls, [111, 222])

    bot.Config.guild_ids = set()
    sync_calls.clear()
    asyncio.run(b._sync_commands())
    check("with no servers configured it falls back to a global sync", sync_calls, [None])
    bot.Config.guild_ids = {111, 222}
    bot.state.last_error = None
    sync_calls.clear()

    async def refusing_sync(*, guild=None):
        gid = getattr(guild, "id", None)
        sync_calls.append(gid)
        if gid == 222:
            # What Discord answers for a server the bot has not been invited into: 403 / 50001.
            raise stub.HTTPException("403 Forbidden (error code: 50001): Missing Access")
        return []

    b.tree.sync = refusing_sync
    asyncio.run(b._sync_commands())  # must not raise: this killed the pod on the first two-server deploy
    check("a server that refuses slash sync does not stop the other one", sync_calls, [111, 222])
    check("and the refusal is recorded for healthz", bool(bot.state.last_error), True)
    check("the record names the server so the log is not a riddle",
          "222" in (bot.state.last_error or ""), True)

    bot.Config.guild_ids = {111}
    bot.state.last_error = None

    check("setup_hook delegates to _sync_commands", "await self._sync_commands()" in bot_src, True)
    check("the only global sync left is the fallback branch inside _sync_commands",
          bot_src.count("await self.tree.sync()"), 1)
    check("on_ready names the servers it is in against the ones it was told to serve",
          "I am in %s; MCBOT_GUILDS is %s" in bot_src, True)

    # --- 10. no adult filter, reactions instead of status messages, no dupe bodies --
    THREADS.clear()
    os.environ["MCBOT_APPROVER_MODE"] = "anyone"
    os.environ.pop("MCBOT_APPROVER_IDS", None)
    os.environ.pop("MCBOT_APPROVER_ROLES", None)
    bot.load_config()

    check("deployed approver mode is anyone", bot.Config.approver_mode, "anyone")
    check("anyone mode authorises a stranger who reached a permitted channel",
          bot.can_approve(types.SimpleNamespace(id=7, roles=[])), True)

    m = FakeMsg("wipe the world")
    d = run(m)
    check("a change request from a child is accepted rather than told to find an adult",
          d.get("kind"), "ask")
    check("and it reached the Pi bridge", handled[-1], "wipe the world")
    check("the ask carries eyes on the original message", m.reactions, ["👀"])

    m = FakeMsg("<@111111111111111111>")
    run(m)
    check("a mention with no question is a question mark", m.reactions, ["❓"])
    check("asked back in a thread, not in the middle of #general", THREADS[-1].name, "what do you need?")
    check("the thread asks something a 6-year-old can answer",
          "server status" in THREADS[-1].sent[0], True)

    first = b._unique(222, "It's up and healthy")
    second = b._unique(222, "It's up and healthy")
    check("a repeated body is changed rather than discarded by Discord", first != second, True)
    check("as Discord small text, not visible noise",
          second.startswith("It's up and healthy\n-# mcbot "), True)
    check("a body nobody has seen yet is sent untouched",
          b._unique(333, "server status: up") == "server status: up", True)

    check("is the server currently working? is read-only (it was refused live)",
          bot.is_readonly("is the server currently working?"), True)
    check("asking for the ip is read-only (it was asked live and refused)",
          bot.is_readonly("what is the ip?"), True)
    check("the grown-ups wording is gone from every refusal",
          any("grown" in s.lower() or "adult" in s.lower() for s in bot.REFUSALS), False)
    check("the failure reply stopped telling children to find an adult",
          "tell an adult" in bot_src, False)
    check("healthz reports the mode and any reaction failure",
          ('"approver_mode"' in bot_src, '"reaction_error"' in bot_src), (True, True))

    os.environ["MCBOT_APPROVER_MODE"] = "allowlist"
    bot.load_config()
    check("allowlist mode still refuses everyone when both lists are empty",
          bot.can_approve(types.SimpleNamespace(id=7, roles=[])), False)

    # ------------------------------------------- 11. being addressed: role mentions count now
    # The gate accepted only a member mention, and that turned out to be the single biggest reason mcbot
    # looked dead. Audit tally from the live server: three of six attempts to get an answer were `@&mcbot`,
    # because Discord's autocomplete puts the bot's role first, and each one was logged "does not mention
    # me" while the boys waited. A bot-managed role mention is being addressed; a player's role is not.
    os.environ["MCBOT_APPROVER_MODE"] = "anyone"
    os.environ.pop("MCBOT_APPROVER_IDS", None)
    os.environ.pop("MCBOT_APPROVER_ROLES", None)
    bot.load_config()
    m = FakeMsg("<@&555> server status", mentions=False,
                role_mentions=[types.SimpleNamespace(id=555, is_bot_managed=lambda: True)])
    d = run(m)
    check("mcbot's own role mention counts as being addressed", d.get("kind"), "ask")
    check("the role mention token is stripped before the Pi reads the text", b._body(m), "server status")

    m = FakeMsg("<@&777> change the motd", mentions=False,
                role_mentions=[types.SimpleNamespace(id=777, is_bot_managed=lambda: False)])
    d = run(m)
    check("a player's role mention is not being addressed", d.get("kind"), "ignored")
    check("and the reason is named rather than silence", "does not mention me" in d.get("reason", ""),
          True)

    # ------------------------------ 12. context read from Discord, images transcribed to text
    # Continuity used to be only what mcbot personally saw since the pod booted, so a rollout wiped the
    # conversation and "what were we just talking about" stopped being answerable. It now pulls the last N
    # messages from Discord per ask. Images are transcribed by `MCBOT_VISION_URL` and handed to Pi as text:
    # the vision model is a separate service, so the Pi RPC stays text-only and mcbot ships no model of its
    # own.
    check("context depth is configured", bot.Config.context_messages, 20)
    # The shipped default names no vision service at all. A default endpoint is somebody else's host, and a
    # stranger's installation that never sets the variable would otherwise send their screenshots to it.
    check("with nothing configured there is no vision endpoint", bot.Config.vision_url, "")
    check("and no model is named either", bot.Config.vision_model, "")
    os.environ["MCBOT_VISION_URL"] = "http://vision.example:8080/v1/chat/completions"
    os.environ["MCBOT_VISION_MODEL"] = "a-vision-model"
    bot.load_config()
    check("a configured endpoint is used verbatim", bot.Config.vision_url,
          "http://vision.example:8080/v1/chat/completions")
    check("a configured model is used verbatim", bot.Config.vision_model, "a-vision-model")

    class HistChannel:
        def __init__(self, past, *, boom=False):
            self.id = 222
            self.past, self.boom = past, boom
            self.seen: list = []

        def history(self, *, limit=None, before=None):
            self.seen.append((limit, before))
            past, boom = self.past, self.boom

            async def gen():
                if boom:
                    raise bot.discord.HTTPException("403")
                for item in past:
                    yield item
            return gen()

    def att(name, ctype, size):
        return types.SimpleNamespace(filename=name, content_type=ctype, size=size, url="https://cdn/x")

    # Built newest-first, because that is the order Discord's history() yields in and the code reverses it.
    past = [FakeMsg("mic check"),
            FakeMsg("look at this", attachments=[att("crash.png", "image/png", 1234)]),
            FakeMsg("who is online?")]
    past[0].author = types.SimpleNamespace(id=1, display_name="mason", bot=False)
    past[1].author = types.SimpleNamespace(id=2, display_name="mcbot", bot=True)
    past[2].author = types.SimpleNamespace(id=1, display_name="mason", bot=False)
    m = FakeMsg("what was I saying?")
    ch = HistChannel(past)
    m.channel = ch
    ctx = asyncio.run(b._context(m))
    check("the ask is answered with Discord's history, not just what mcbot has seen", len(ctx), 3)
    check("history() is called once, with the configured limit and this message as the cursor",
          ch.seen, [(20, m)])
    check("it comes back oldest first", ctx[0], "mason: who is online?")
    check("mcbot's own earlier answers are labelled as its own", ctx[1].startswith("you said earlier: look at this"),
          True)
    check("an image in the history is labelled without spending a vision call on it",
          "[1 attachment(s): image/png]" in ctx[1], True)

    m = FakeMsg("server status")
    m.channel = HistChannel([], boom=True)
    bot.state.history[222].append("ed: something from before the crash")
    ctx = asyncio.run(b._context(m))
    check("a failed history read falls back to local memory instead of failing the ask",
          ctx[-1], "ed: something from before the crash")
    check("and the failure is visible in healthz", "history" in (bot.state.last_error or ""), True)

    # Every vision failure mode has to end as a bracketed note in the prompt, never as a dropped ask.
    m = FakeMsg("what does this say?", attachments=[att("note.txt", "text/plain", 40)])
    check("a non-image attachment is named rather than pushed at the vision model",
          "not an image" in asyncio.run(b._images(m))[0], True)

    m = FakeMsg("big screenshot", attachments=[att("huge.png", "image/png", 9_000_000)])
    check("an oversized image is refused with the limit stated",
          "over the 4000000 limit" in asyncio.run(b._images(m))[0], True)

    m = FakeMsg("server status")
    check("no attachments means no vision work at all", asyncio.run(b._images(m)), [])

    os.environ["MCBOT_VISION_URL"] = ""
    bot.load_config()
    m = FakeMsg("screenshot", attachments=[att("a.png", "image/png", 10)])
    check("vision can be switched off without losing the ask",
          "vision is disabled" in asyncio.run(b._images(m))[0], True)
    os.environ.pop("MCBOT_VISION_URL", None)
    bot.load_config()

    prompt = bot.build_prompt(222, "what does this say?", "", approved=True,
                              context=["mason: look at this", "mcbot: [1 attachment(s): image/png]"],
                              images=["[image crash.png: NullPointerException at net.minecraft]"])
    check("the prompt carries the Discord context", "- mason: look at this" in prompt, True)
    check("the prompt carries the transcription", "NullPointerException" in prompt, True)
    check("the prompt no longer tells children to find a parent", "ask a parent" in prompt, False)
    check("an unapproved turn points at the button instead",
          "confirmation button" in bot.build_prompt(222, "wipe it", "", approved=False), True)

    # ------------------------------------------- 13. which brain answers, and which box runs it
    # Which model answers is a deployment choice, and `auto` in the source is a placeholder meaning "whatever
    # the configured provider calls default". A pin in the code would be a deployment baked into a library; a
    # pin that silently reverted would look identical to a working bot. Both directions are asserted, and the
    # provider file the child is given is generated from the same two variables rather than checked in.
    PKG = os.path.dirname(os.path.abspath(__file__))
    os.environ["PI_PROVIDER"] = "example-provider"
    os.environ["PI_MODEL"] = "example-org/Example-Model-1.2bpw"
    bot.load_config()
    check("PI_MODEL reaches Config", bot.Config.model, "example-org/Example-Model-1.2bpw")
    check("PI_PROVIDER reaches Config", bot.Config.provider, "example-provider")
    os.environ.pop("PI_MODEL", None)
    bot.load_config()
    check("the code default stays auto", bot.Config.model, "auto")
    check("healthz reports the provider and model in use",
          ('"pi_provider"' in bot_src, '"pi_model"' in bot_src), (True, True))

    # The generator is run the way the entrypoint runs it: with the deployment's own provider and model in the
    # environment. The two variables above were popped by the default check just before this, and a generator
    # tested with them missing is a generator tested against the table's placeholder rather than a deployment.
    rendered = subprocess.run([sys.executable, os.path.join(PKG, "render_pi_models.py")],
                              capture_output=True, text=True,
                              env={**os.environ, "PI_PROVIDER": "example-provider",
                                   "PI_MODEL": "example-org/Example-Model-1.2bpw",
                                   "MCBOT_PI_BASE_URL": "http://infer.example:8080/v1"})
    check("the provider file is generated from configuration", rendered.returncode, 0)
    check("the pinned id is declared in it, so --model does not fail at startup",
          '"id": "example-org/Example-Model-1.2bpw"' in rendered.stdout, True)
    check("and the provider name is the configured one",
          '"example-provider"' in rendered.stdout, True)
    check("the generator emits one provider and it is the configured one",
          (rendered.stdout.count('"baseUrl"'), '"example-provider"' in rendered.stdout), (1, True))

    persona = open(os.path.join(PKG, "workspace", "AGENTS.md")).read()
    # A rule written as "never say X" hands a small instruct model the sentence X, verbatim, in the most
    # salient place in its context. Live proof: 03:41:17 `ask accepted` for "do you have the capability to
    # install mods?" - gate open, tier fine - and one second later mcbot posting the retired refusal word
    # for word. The gate never fired; the model repeated a phrase the persona had quoted in order to ban it.
    forbidden = ["grown", "ask a parent", "find an adult", "make me change the server"]
    hits = [w for w in forbidden if w in persona.lower()]
    check("the persona contains none of the wording it is trying to forbid", hits, [])
    check("the persona states the rule positively instead of quoting the old one",
          "whoever asked is the one who presses" in persona, True)
    check("and no refusal string from bot.py is quoted in the persona either",
          [r for r in bot.REFUSALS if r.lower() in persona.lower()], [])

    # ------------------------------------ 15. a retired refusal is not context, it is an example
    # Two channels feed the model its own past wording: the Discord history read, and Pi's continuing
    # in-memory session. Both now have a stopcock.
    stale = [FakeMsg("make me a grown up"),
             FakeMsg("I can answer questions here, but only the grown-ups can make me change the server."),
             FakeMsg("Server's up with 0 of 20 players online right now."),
             FakeMsg("restart it"),
             FakeMsg("Restarted! mc-0 came back up fresh."),
             FakeMsg("ask a parent if you want that one"),
             FakeMsg("who is online?")]
    for i in range(len(stale)):
        if i in (1, 2, 4, 5):
            stale[i].author = types.SimpleNamespace(id=9, display_name="mcbot", bot=True)
        else:
            stale[i].author = types.SimpleNamespace(id=1, display_name="mason", bot=False)
    m = FakeMsg("and the line after that?")
    m.channel = HistChannel(stale)
    ctx = asyncio.run(b._context(m))
    joined = "\n".join(ctx)
    check("mcbot's retired policy refusals never re-enter the prompt as precedent",
          [l for l in ctx if "grown-ups" in l or "ask a parent" in l], [])
    check("what its users said is still in there in full",
          [w for w in ("make me a grown up", "restart it", "who is online?") if w not in joined], [])
    check("its own useful answers survive, capped and attributed as its own (oldest first)",
          [l for l in ctx if l.startswith("you said earlier:")],
          ["you said earlier: Restarted! mc-0 came back up fresh.",
           "you said earlier: Server's up with 0 of 20 players online right now."])
    check("the self cap is configurable", bot.Config.context_self_max, 3)
    many = [FakeMsg(f"answer {n}.") for n in range(6)]
    for p in many:
        p.author = types.SimpleNamespace(id=9, display_name="mcbot", bot=True)
    m = FakeMsg("anything else?")
    m.channel = HistChannel(many)
    kept = [l for l in asyncio.run(b._context(m)) if l.startswith("you said earlier:")]
    check("the cap keeps its most recent answers and drops the older ones", kept,
          ["you said earlier: answer 2.", "you said earlier: answer 1.", "you said earlier: answer 0."])
    check("and the scratch list is configurable", "grown" in bot.Config.context_scratch, True)

    # Pi is one long-lived child per pod; measured in the pod, the second prompt recalled a word from the
    # first (same pid, same sessionId, messageCount 5). ask() now resets it per ask.
    pi_src = open(os.path.join(PKG, "pi_rpc.py")).read()
    check("ask() starts a fresh Pi session", '"type": "new_session"' in pi_src, True)
    check("the fresh session is switchable", "MCBOT_PI_FRESH_SESSION" in pi_src, True)
    check("the docstring no longer claims --no-session isolates the conversation",
          "in-memory conversation" in pi_src, True)

    # A second client on the same token is what actually caused the phantom refusals: an old local build
    # answered with its canned text and this pod could only see the result as `author is a bot`.
    check("an interaction stolen by another instance is reported as such", "10062" in bot_src, True)
    check("and there is a handler registered for it", hasattr(bot.McBot, "_on_tree_error"), True)

    # --------------------------- 16. queue the asks, steer the corrections, let them say stop
    # "one thing at a time - I'm still finishing the last one" was mcbot's answer to a group of kids who are
    # simply impatient. It read as a broken bot and produced more messages, not fewer.
    class FakeRpc:
        def __init__(self):
            self.alive = True
            self.prompts: list = []
            self.commands: list = []
            self.replies = ["answer one", "answer two", "answer three"]

        async def ask(self, prompt, timeout=300.0):
            self.prompts.append(prompt)
            await asyncio.sleep(0.02)
            r = bot.PiReply()
            r.text = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
            return r

        async def steer(self, message):
            self.commands.append(("steer", message))
            return {"disposition": "queued"}

        async def clear_queue(self):
            self.commands.append(("clear_queue",))
            return {"steering": [], "followUp": []}

        async def abort(self):
            self.commands.append(("abort",))
            return {}

    class QChannel:
        id = 777
        guild = None

        def __init__(self):
            self.messages: list = []

        async def send(self, content, **kw):
            self.messages.append(content)
            return types.SimpleNamespace(id=1)

    def qmsg(content):
        m = types.SimpleNamespace(content=content, attachments=[], id=len(content))
        m.author = types.SimpleNamespace(id=11, display_name="mason", bot=False)
        m.guild = types.SimpleNamespace(id=22)
        m.channel = types.SimpleNamespace(id=777)

        async def add_reaction(emoji):
            return None
        m.add_reaction = add_reaction
        return m

    sent: list = []

    async def fake_safe_send(channel, content, **kw):
        sent.append(content)
        return True

    real_safe_send = bot.safe_send
    bot.safe_send = fake_safe_send
    # The queue lives in SQLite on the PVC now, so the test needs its own file rather than /data.
    bot.Config.db_path = os.path.join(audit_dir, "queue-test.db")
    b.store = bot.Store(bot.Config.db_path)
    # Earlier sections replaced `b.handle` with a recorder that only logs the text. This section needs the
    # real method, and shadowing is exactly the trap that makes a test pass while testing nothing.
    b.handle = bot.McBot.handle.__get__(b)
    try:
        async def drive():
            fake = FakeRpc()
            b.pi = fake
            ch = QChannel()
            b.get_channel = lambda cid: ch  # a real client resolves a channel id; this one has no gateway
            h1 = asyncio.create_task(b.handle(ch, "restart the server", "", 11, msg=qmsg("restart the server")))
            for _ in range(100):
                if 777 in bot.state.busy:
                    break
                await asyncio.sleep(0.001)
            await b.handle(ch, "and use the new seed", "", 11, msg=qmsg("and use the new seed"))
            await b.handle(ch, "what plugins are installed?", "", 11, msg=qmsg("what plugins are installed?"))
            await h1
            for _ in range(200):
                if len(fake.prompts) >= 2 and 777 not in bot.state.busy and len(ch.messages) >= 2:
                    break
                await asyncio.sleep(0.01)
            return fake, ch

        fake, ch = asyncio.run(drive())
        answers = [m for m in sent if m.startswith("answer ")]
        check("a correction mid-run is steered into the running turn",
              [c for c in fake.commands if c[0] == "steer" and "use the new seed" in c[1]][:1] != [], True)
        check("it is not treated as a separate job", len(fake.prompts), 2)
        check("the independent ask waited its turn and got its own answer", answers, ["answer one", "answer two"])
        check("order is preserved: queued asks drain oldest first",
              "what plugins are installed?" in fake.prompts[1], True)
        check("the old shrug is gone from every string mcbot sends",
              [l for l in bot_src.splitlines() if "one thing at a time" in l and "send(" in l], [])

        async def drive2():
            fake = FakeRpc()
            b.pi = fake
            b.store.queue_drop(777)
            ch = QChannel()
            b.get_channel = lambda cid: ch
            h1 = asyncio.create_task(b.handle(ch, "restart the server", "", 11, msg=qmsg("restart the server")))
            for _ in range(100):
                if 777 in bot.state.busy:
                    break
                await asyncio.sleep(0.001)
            fills = ["what is the ip?", "who is online?", "is the server working?", "what mods are installed?"]
            texts = []
            for t in fills:
                await b.handle(ch, t, "", 11, msg=qmsg(t))
                texts.append(t)
            queued_now = b.store.queue_count(777)
            await b.handle(ch, "stop", "", 11, msg=qmsg("stop"))
            await h1
            await asyncio.sleep(0.05)
            return fake, ch, queued_now

        fake2, ch2, queued_now = asyncio.run(drive2())
        check("the queue has a ceiling so a channel cannot stall the bot forever", queued_now, bot.Config.queue_max)
        check("and the ceiling is explained rather than silently dropped",
              any("queue in here is full" in m for m in sent), True)
        check("stop clears Pi's own queue and aborts the run",
              [c[0] for c in fake2.commands if c[0] in ("clear_queue", "abort")], ["clear_queue", "abort"])
        check("stop also drops what was waiting in this channel", b.store.queue_count(777), 0)
        check("queue depth is visible in healthz", "queued_asks" in open(os.path.join(PKG, "bot.py")).read(), True)
        check("queue depth is configurable", bot.Config.queue_max, 3)
    finally:
        bot.safe_send = real_safe_send
        b.store.queue_drop(777)
        bot.state.busy.discard(777)

    # ---- 17. a deploy may not lose an ask, and may not repeat a change it might already have made
    st = bot.Store(os.path.join(audit_dir, "store-test.db"))
    st.queue_add(500, "what is the ip?", "", 11, False)
    st.queue_add(500, "who is online?", "", 11, False)
    check("queued asks live on the volume, not in the pod", st.queue_count(500), 2)
    check("and they come back oldest first", st.queue_take(500)["text"], "what is the ip?")
    st.run_note(500, "restart the server")
    st2 = bot.Store(os.path.join(audit_dir, "store-test.db"))  # a brand new process, same volume
    check("a restarted process can see the ask it was in the middle of",
          st2.runs_interrupted()[0]["text"], "restart the server")
    st2.run_clear(500)
    st2.run_note(502, "restart the server")
    bot.state.busy.add(502)
    check("a run that is in flight right now is not reported as an interruption",
          st2.stats(bot.state.busy)["interrupted"], 0)
    check("only a run a killed pod left behind is", st2.stats()["interrupted"], 1)
    bot.state.busy.discard(502)
    st2.run_clear(502)
    cid = st2.confirm_save(500, "restart", "restart the server", "restart the server", 11)
    check("an unanswered confirmation button survives too", cid > 0, True)
    st2.confirm_close(cid, "pressed")
    check("and it is closed once it has been used", st2.confirm_active(), [])
    st2.queue_add(501, "what mods are installed?", "", 11, False)
    check("stale queued asks are dropped rather than delivered hours late", st2.prune(-1), 2)
    check("the database defaults onto the mounted volume", bot.ConfigModel().db_path.startswith("/data/"), True)

    async def drive3():
        bot.Config.readonly_words = ("status",)  # pinned so the classification below cannot drift
        mc = bot.McBot()
        mc.store = bot.Store(os.path.join(audit_dir, "resume.db"))
        mc.pi = FakeRpc()
        ch = QChannel()
        # The gateway cache knows one channel; a thread is not in it and has to be fetched from Discord.
        mc.get_channel = lambda cid: ch if cid == 777 else None

        async def fake_fetch(cid):
            return ch
        mc.fetch_channel = fake_fetch
        mc.store.run_note(777, "what is the server status?")  # read-only, cached channel
        mc.store.run_note(779, "server status")               # read-only, asked in a thread
        mc.store.run_note(778, "delete the world")            # a write, cut off by the same restart
        before = len(sent)
        await mc._resume()
        await asyncio.sleep(0.05)
        return mc, mc.pi, ch, sent[from_index:]

    from_index = len(sent)
    bot.safe_send = fake_safe_send
    try:
        mc3, fake3, ch3, announced = asyncio.run(drive3())
    finally:
        bot.safe_send = real_safe_send
        bot.state.pending.pop(777, None)
        bot.state.pending.pop(778, None)
        bot.state.pending.pop(779, None)

    check("a read-only ask cut off mid-run is simply done again", len(fake3.prompts), 2)
    check("both of them, not just the one that was convenient",
          sum(1 for p in fake3.prompts if "status" in p), 2)
    check("an ask that came in inside a thread is resumed rather than dropped",
          sum(1 for m in announced if "carrying on with it" in m), 2)
    check("and it is the one that was cut off", "what is the server status?" in fake3.prompts[0], True)
    check("a write ask cut off mid-run is never repeated", any("delete the world" in p for p in fake3.prompts),
          False)
    check("it comes back as one button, not two",
          sum(1 for m in ch3.messages if "Press the button" in m), 1)
    check("with wording that says what actually happened",
          any("I was cut off before finishing" in m for m in ch3.messages), True)
    check("and does not invent a category name for it",
          any("changes the server" in m for m in ch3.messages), False)
    check("the bot says out loud that it did not repeat anything",
          any("nothing has been repeated" in m for m in announced), True)
    check("resume runs when the gateway is up", "await self._resume()" in bot_src, True)

    # --- 18. the volume this queue lives on, and the journal mode that goes with it -----
    pvc_yaml = source("deploy", "example", "pvc.yaml")
    # Which access mode a deployment should ask for is a property of the storage, not of mcbot, so this asserts
    # the thing that *is* the program's business: a sqlite file on a network filesystem must not be put in WAL
    # mode, and the example has to say why anybody would care.
    check("the example volume states its access mode rather than inheriting one",
          "accessModes:" in pvc_yaml, True)
    check("and it points at the journal-mode consequence instead of leaving it to luck",
          "journal" in pvc_yaml, True)
    check("a database on NFS is not put in WAL mode", bot.journal_mode_for("/data/mcbot.db", [("/data", "nfs4")]),
          "DELETE")
    check("the mount that actually contains the file decides it, not the nearest guess",
          bot.journal_mode_for("/data/mcbot.db", [("/", "ext4"), ("/data", "nfs4")]), "DELETE")
    check("a local filesystem still gets WAL", bot.journal_mode_for("/data/mcbot.db", [("/", "ext4")]), "WAL")
    check("and the store says which mode it used", mc3.store.stats()["journal"] in ("WAL", "DELETE"), True)

    # --- 19. the confirm buttons themselves -------------------------------------------
    # From the live pod: `Task exception was never retrieved ... AttributeError: 'ConfirmView' object has no
    # attribute 'message'`. discord.py 2.7 removed View.message, and the first _disable() reached for it. In
    # approve() that ran after the pending was popped and the confirmation row closed, but before the approved
    # action - so a press answered "this interaction failed", the change never happened, and pressing again said
    # "that offer expired". The stub View above has no `message` either, which is what makes these checks mean
    # something rather than everything passing in the stub and failing in the pod.
    conf_src = inspect.getsource(bot.ConfirmView)
    check("nothing in the button housekeeping reaches for the attribute discord.py removed",
          "self.message" in conf_src, False)
    check("the view keeps its own reference to the message it was sent on",
          "self.target" in conf_src, True)

    class ConfMessage:
        def __init__(self, content, view, events=None, break_edit=False):
            self.content = content
            self.view = view
            self.events = events if events is not None else []
            self.edits: list = []
            self.break_edit = break_edit

        async def edit(self, *, view=None, **kw):
            if self.break_edit:
                raise bot.discord.HTTPException("10008: Unknown Message")
            self.edits.append(view)
            self.events.append("edit")
            return self

    class ConfChannel:
        def __init__(self, cid=880, break_edit=False):
            self.id = cid
            self.msgs: list = []
            self.events: list = []
            self.break_edit = break_edit

        async def send(self, content=None, *, view=None, **kw):
            msg = ConfMessage(content or "", view, self.events, self.break_edit)
            self.msgs.append(msg)
            self.events.append("send")
            return msg

    class ConfInteraction:
        """Enough of a discord.Interaction to see what the buttons do, and in which order they do it."""

        def __init__(self, channel, uid=11):
            self.channel = channel
            self.channel_id = channel.id
            self.user = FakeUser(uid)
            self.response = types.SimpleNamespace()

            async def send_message(content=None, **kw):
                channel.events.append("respond")

            self.response.send_message = send_message

    class FakeConfRpc:
        def __init__(self):
            self.alive = True
            self.prompts: list = []

        async def ask(self, prompt, timeout=300.0):
            self.prompts.append(prompt)
            r = bot.PiReply()
            r.text = "done"
            return r

    def conf_bot(channel):
        mb = bot.McBot()
        mb.store = bot.Store(os.path.join(audit_dir, "confirm.db"))
        mb.pi = FakeConfRpc()
        mb.user = types.SimpleNamespace(id=1)
        ran: list = []

        async def fake_handle(channel, text, note, user_id, approved=False):
            ran.append({"text": text, "note": note, "approved": approved})
        mb.handle = fake_handle
        return mb, ran

    bot.Config.approver_mode = "anyone"  # what is deployed; the gate itself is sections 1-5

    def run_ok(coro):
        """Run a button callback and hand back the exception it raised, if any.

        The old code raised AttributeError out of `View._disable`, and a test that let that escape would show up
        as a crashed suite rather than a failed check - which is easy to mistake for a harness problem.
        """
        try:
            asyncio.run(coro)
            return None
        except Exception as exc:  # noqa: BLE001
            return exc

    # Offer, press, and the order they happen in.
    ch4 = ConfChannel()
    mb4, ran4 = conf_bot(ch4)
    r4 = bot.PiReply()
    r4.text = "I can do that, but say the word first. [[CONFIRM wipe: delete the whole world]]"
    asyncio.run(mb4.post(ch4, r4, 11))
    check("a flagged reply sends the plain words, then the button", len(ch4.msgs), 2)
    check("the preamble is not held hostage to the button", "say the word first" in ch4.msgs[0].content, True)
    check("the button message says nothing has changed yet", "Nothing has changed yet" in ch4.msgs[-1].content, True)
    view4 = ch4.msgs[-1].view
    check("and the offer path is what put the message on the view", getattr(view4, "target", None) is ch4.msgs[-1],
          True)

    inter4 = ConfInteraction(ch4)
    check("pressing Do it does not raise", run_ok(view4.approve(inter4, None)), None)
    ev4 = ch4.events[2:]
    check("a press answers Discord before it touches anything else", ev4[:1], ["respond"])
    check("then greys the buttons, then does the work", ev4[1:2], ["edit"])
    check("a press runs the thing that was pressed", len(ran4), 1)
    check("with the human named to the model as the approver",
          "clicked the confirm button" in ran4[0]["note"], True)
    check("and only the ask, not the marker scaffolding", "[[CONFIRM" in ran4[0]["text"], False)
    check("the offer is closed in the database too", mb4.store.confirm_active(), [])

    # A Discord edit failure must not cost anybody their approval.
    ch5 = ConfChannel(break_edit=True)
    mb5, ran5 = conf_bot(ch5)
    r5 = bot.PiReply()
    r5.text = "[[CONFIRM restart: restart the server]]"
    asyncio.run(mb5.post(ch5, r5, 11))
    inter5 = ConfInteraction(ch5)
    check("a Discord edit failure does not raise out of the press", run_ok(ch5.msgs[-1].view.approve(inter5, None)),
          None)
    check("a failed button-grey-out still does the approved work", len(ran5), 1)

    # Refusal.
    ch6 = ConfChannel()
    mb6, ran6 = conf_bot(ch6)
    r6 = bot.PiReply()
    r6.text = "[[CONFIRM delete: remove player Notch from the whitelist]]"
    asyncio.run(mb6.post(ch6, r6, 11))
    inter6 = ConfInteraction(ch6)
    check("pressing No does not raise either", run_ok(ch6.msgs[-1].view.deny(inter6, None)), None)
    check("no means nothing runs", len(ran6), 0)
    check("and no means the buttons go grey as well", len(ch6.msgs[-1].edits), 1)

    # Expiry, which is where the live traceback came from.
    ch7 = ConfChannel()
    mb7, ran7 = conf_bot(ch7)
    r7 = bot.PiReply()
    r7.text = "[[CONFIRM restart: restart the server]]"
    asyncio.run(mb7.post(ch7, r7, 11))
    old_pend = bot.state.pending[ch7.id]
    check("expiry does not raise - this is the traceback that came out of the live pod",
          run_ok(ch7.msgs[-1].view.on_timeout()), None)
    check("an expired offer stops being pending", bot.state.pending.get(ch7.id) is None, True)
    conn = sqlite3.connect(mb7.store.path, timeout=10)
    check("and it is recorded as expired rather than silently vanished",
          list(conn.execute("select status from confirm where id=?", (old_pend.store_id,)))[0][0], "expired")
    conn.close()
    check("expiry greys the buttons so nobody presses a dead one", len(ch7.msgs[-1].edits), 1)

    # A view whose message is gone (restart, deleted message) must fail quietly, not as a traceback.
    orphan = bot.ConfirmView(mb7, bot.Pending(kind="wipe", detail="gone", question="gone", author_id=0,
                                             channel_id=881))
    orphan.target = None
    check("a button with no message to edit does not raise", run_ok(orphan.on_timeout()), None)

    # An older button timing out must not cancel a newer offer in the same channel.
    ch8 = ConfChannel()
    mb8, ran8 = conf_bot(ch8)
    r8 = bot.PiReply()
    r8.text = "[[CONFIRM restart: restart the server]]"
    asyncio.run(mb8.post(ch8, r8, 11))
    stale_view = ch8.msgs[-1].view
    stale_pend = bot.state.pending[ch8.id]
    new_pend = bot.Pending(kind="wipe", detail="delete the world", question="delete the world", author_id=11,
                           channel_id=ch8.id)
    bot.state.pending[ch8.id] = new_pend
    check("a newer offer surviving an older button's expiry does not raise", run_ok(stale_view.on_timeout()), None)
    check("a stale button cannot cancel the offer that replaced it",
          bot.state.pending.get(ch8.id) is new_pend, True)
    check("a stale button cannot cancel the offer that replaced it",
          bot.state.pending.get(ch8.id) is new_pend, True)
    conn8 = sqlite3.connect(mb8.store.path, timeout=10)
    check("the superseded offer is closed rather than left open to be re-offered",
          list(conn8.execute("select status from confirm where id=?", (stale_pend.store_id,)))[0][0], "expired")
    conn8.close()
    check("so nothing is left pretending to be pending", mb8.store.confirm_active(), [])
    bot.state.pending.pop(ch8.id, None)
    reset_config()

    # -- 18. it may not invent its own internals, and it is not on a clock
    persona = open(os.path.join(PKG, "workspace", "AGENTS.md")).read()
    prompt = bot.build_prompt(1, "what is the ip?", "", approved=True, context=[], store=st2)
    check("the prompt carries a measured description of mcbot itself",
          "your own condition, measured just now" in prompt, True)
    check("and it is a measurement rather than a mood", st2.path in prompt, True)
    check("the persona makes a dated copy before anything git cannot undo",
          ("Copy first" in persona and "data-old-2026-11-17" in persona), True)
    check("the copy rule is tied to the irreversible cases, not bolted to every change",
          ("git revert" in persona and "Do not copy the world for those" in persona), True)
    check("the copy is checked for size before the destructive step, not assumed",
          ("plausible size" in persona and "df -h" in persona), True)
    check("no room for the copy means asking first rather than doing it anyway",
          "ask before going on" in persona, True)
    check("a copy never replaces the confirmation press",
          "not a substitute for the confirmation" in persona, True)
    check("the persona sends the model to look instead of guess", "run `ls /data`" in persona, True)
    check("the persona names the failure that actually happened", "appear missing" in persona, True)
    check("the persona takes the clock off it", "never report a timeout you did not see" in persona, True)

    pi_src = open(os.path.join(PKG, "pi_rpc.py")).read()
    check("a long grind is no longer cut off by a wall clock", "exceeded the time limit" in pi_src, False)
    check("only silence aborts a run", "pi went silent for" in pi_src, True)
    check("the watchdog is generous", bot.Config.idle_timeout >= 900, True)
    check("the old wall limit is gone from config", hasattr(bot.Config, "timeout"), False)

    # /mc crashed in the live pod: `interaction.message` is None for a normally invoked slash command and the
    # 👀 ack assumed there was always a message to put it on.
    asyncio.run(b._react(None, "👀"))
    asyncio.run(b._react(object(), "👀"))
    check("the guard that stops that crash is in place",
          'if msg is None or not hasattr(msg, "add_reaction")' in bot_src, True)

    # /healthz is a module-level aiohttp handler. The last time this dictionary was edited as if it were a
    # method, every liveness probe in the cluster got a 500 out of `NameError: name 'self' is not defined`.
    payload = bot.health_payload()
    check("healthz reports queue depth with no instance in scope", isinstance(payload["queued_asks"], dict), True)
    check("and it reports the store it is actually using", payload["store"]["path"], bot.STORE.path)
    check("nothing in the health payload reaches for self",
          [l for l in bot_src.splitlines() if l.strip().startswith(('"queued_asks', '"store"')) and "self." in l],
          [])

    check("no age gate wording survives in what mcbot says out loud",
          [w for w in ("needs an adult", "ask an adult", "only the grown-ups", "an adult can press")
           if w in bot_src], [])

    # --- 20. asks from inside Minecraft ------------------------------------------------
    # The in-game path has two halves with different failure modes: a capture that never fires (the log format
    # is not what the parser assumed) and an answer that never lands (RCON down, or a reply whose newlines
    # turn one command into two). Both are checked against the real log lines and the real client's contract.
    real_lines = [
        # Taken from a live `kubectl logs` of a Paper 1.21.11 server in this image, including the ANSI cursor
        # junk the server console writes in front of every line. A parser written against the documentation
        # fails here; one written against the log does not.
        ">....\u001b[K[06:21:44 INFO]: <Steve> WWWWWWWWWW",
        ">....\u001b[K[06:40:33 INFO]: <Notch> nha",
        ">....\u001b[K[06:53:01 INFO]: Steve issued server command: /plugins",
        ">....\u001b[K[06:52:14 INFO]: [voicechat] Sent secret to Notch",
        ">....\u001b[K[06:52:41 INFO]: System chat: Notch has made the advancement \u001b[92m[Getting an Upgrade]\u001b[0m",
        "[06:52:16 INFO] [Server thread/INFO]: <Steve> !mcbot what is the ip",
    ]
    parsed = [bot.CHAT_LINE_RE.search(bot.ANSI_RE.sub("", l)) for l in real_lines]
    check("a real chat line is parsed for the player and the text",
          (parsed[0]["name"], parsed[0]["text"]) if parsed[0] else None,
          ("Steve", "WWWWWWWWWW"))
    check("a chat line with a component prefix in front of it is still parsed",
          (parsed[5]["name"], parsed[5]["text"]) if parsed[5] else None,
          ("Steve", "!mcbot what is the ip"))
    check("a command a player issued is not treated as chat",
          any(m and m["name"] == "Steve" for m in parsed[2:5]), False)
    check("voicechat and advancement noise does not look like chat", parsed[3], None)
    check("a player joining is not chat", bot.CHAT_LINE_RE.search("Notch joined the game"), None)
    check("the server's own broadcast is not chat, so the tail cannot answer itself",
          bot.CHAT_LINE_RE.search("[06:53:05 INFO] [Server thread/INFO]: <[Server]> mcbot: Java: 1.2.3.4"), None)
    # The `[Not Secure] ` shape is taken from a live log line this server wrote for its own RCON traffic
    # (`]: [Not Secure] [Rcon] mcbot: ...`), which is the only place this cluster has produced the tag. Paper
    # puts the same tag in front of chat from a client that skipped chat signing, and that is a player.
    check("an unsigned client's chat line is still chat",
          bot.CHAT_LINE_RE.search("[06:53:06 INFO] [Server thread/INFO]: [Not Secure] <Notch> hello").group("name"),
          "Notch")
    check("the same tag in front of the bot's own RCON traffic is still not chat",
          bot.CHAT_LINE_RE.search("[06:53:07 INFO]: [Not Secure] [Rcon] mcbot: Java: 203.0.2.5:25565"), None)

    # --- 20a. the tail has to name the container the pod actually has -------------------------------
    # `kubectl logs -c` matches a container id, not whatever the manifest calls the container. On the server
    # this bot was written against, those two words are `server` and `minecraft`, and getting them the other way
    # round makes every ask fail in silence: no chat lines, `tailing: false` in healthz, Discord still fine.
    with open(os.path.join(HERE, "deploy", "example", "configmap.yaml")) as fh:
        example_cm = fh.read()
    check("the shipped example names the container id, not the manifest's container name",
          "MCBOT_INGAME_CONTAINER: server" in example_cm, True)
    with open(os.path.join(HERE, "config_table.py")) as fh:
        check("the knob that decides which container is tailed is a declared variable",
              "MCBOT_INGAME_CONTAINER" in fh.read(), True)

    class GameRpc:
        def __init__(self, replies, delay=0.05):
            self.replies = list(replies)
            self.prompts: list = []
            self.alive = True
            # A real ask takes long enough that a second ask arrives while it is running. A fake that answers
            # instantly can never be busy, so the queueing paths would never be exercised.
            self.delay = delay

        async def ask(self, prompt, timeout=300.0):
            self.prompts.append(prompt)
            await asyncio.sleep(self.delay)
            r = bot.PiReply()
            r.text = self.replies.pop(0) if self.replies else "done"
            return r

        async def clear_queue(self):
            self.prompts.append("<queue cleared>")

        async def abort(self):
            self.prompts.append("<aborted>")

    class GameRcon:
        """Stands in for the RCON session, recording exactly what would have gone to the server."""

        def __init__(self, fail=False):
            self.commands: list[str] = []
            self.connected = True
            self.fail = fail
            self.commands_sent = 0

        async def command(self, text):
            self.commands.append(text)
            if self.fail:
                raise bot.RconError("rcon is down")
            return "ok"

    game_db = os.path.join(audit_dir, "game.db")
    for key in list(bot.state.pending) + list(bot.state.game_queue):
        bot.state.pending.pop(key, None)
        bot.state.game_queue.pop(key, None)
    bot.state.game_last.clear()
    bot.Config.approver_mode = "anyone"
    bot.Config.ingame = True
    bot.Config.ingame_cooldown = 0.0
    bot.Config.ingame_max_len = 60
    bot.Config.ingame_line_len = 40
    bot.Config.ingame_max_lines = 6
    bot.Config.confirm_ttl = 300.0
    # The wrappers enforce a turn from this file, so the game half of the suite needs one it can actually
    # read back. Pointing it at /data would make every run here depend on whether the machine running it has
    # a /data.
    bot.Config.turn_mode_path = os.path.join(audit_dir, "turn.json")

    def game_bot(replies, fail=False):
        mb = bot.McBot()
        mb.store = bot.Store(game_db)
        mb.pi = GameRpc(replies)
        mb.rcon = GameRcon(fail=fail)
        # A connected discord.py client always has .user; health_payload reads it. A harness that omits it
        # reports a healthy bot as broken.
        mb.user = types.SimpleNamespace(id=1)
        return mb

    g = game_bot(["**192.0.2.10** `minecraft` straight up"])
    asyncio.run(g._game_line("Notch", "!mcbot what is the server ip"))
    sent = g.rcon.commands
    check("an in-game answer is broadcast to everybody, never whispered to the player who asked",
          (bool(sent), all(c.startswith("say ") for c in sent), any(c.startswith("msg ") for c in sent)),
          (True, True, False))
    check("the answer is the answer, not a Discord post",
          any("192.0.2.10" in c for c in sent), True)
    check("markdown that means nothing in a chat window is removed, not escaped",
          any("*" in c or "`" in c for c in sent), False)
    check("the ask is labelled as coming from the game, so the model does not answer as if it were Discord",
          any("inside Minecraft" in p for p in g.pi.prompts), True)
    check("and it is told out loud what that means: a public answer, and no changes",
          any("broadcast to every player" in p and "may not change anything" in p for p in g.pi.prompts), True)
    check("the capability switches reach the prompt too, so the model cannot offer what the wrappers refuse",
          any("switched off in config" in p for p in g.pi.prompts), True)
    turn_rec = bot.read_turn_mode(bot.Config.turn_mode_path)
    check("the turn record the kubectl and mc-rcon wrappers read says: read-only, unapproved, from the game",
          (turn_rec.get("readonly"), turn_rec.get("approved"), turn_rec.get("surface")),
          (True, False, "game"))
    check("one answer becomes one `say` per chat line, name on the first line only",
          bot.McBot.game_broadcast_lines("line one\nline two\nline three"),
          ["say mcbot: line one", "say line two", "say line three"])
    check("a turn mode that cannot be written is reported, not swallowed",
          bot.write_turn_mode(os.path.join(audit_dir, "no-such-dir", "turn.json"),
                              readonly=False, approved=True, surface="test"), False)
    conn = sqlite3.connect(game_db)
    tables = {t: conn.execute(f"select count(*) from {t}").fetchone()[0]
              for t in ("run", "queue", "confirm")}
    check("an in-game ask is never written to the durable queue",
          [v for v in tables.values() if v], [])

    quiet_before = (len(g.rcon.commands), len(g.pi.prompts))
    asyncio.run(g._game_line("Steve", "hello there"))
    check("ordinary chat the bot is not mentioned in is ignored completely",
          (len(g.rcon.commands), len(g.pi.prompts)), quiet_before)
    asyncio.run(g._game_line("Steve", "what is !mcbot"))
    check("a trigger that appears mid-sentence is not a trigger", len(g.pi.prompts), quiet_before[1])

    # The same rule, stated once as a function rather than inferred from what the bot did with a line.
    # `None` (not an ask) and `""` (trigger with nothing after it) are different outcomes and must stay so:
    # the first is silence, the second is the bot asking the player to say what they want.
    check("an ask splits into the trigger word and the question",
          bot.ask_request("!mcbot who is online", bot.Config.ingame_triggers), ("!mcbot", "who is online"))
    check("the other trigger word works too",
          bot.ask_request("@mcbot whats the tps", bot.Config.ingame_triggers), ("@mcbot", "whats the tps"))
    check("a bare trigger word is an ask with an empty question, not a non-ask",
          bot.ask_request("!mcbot", bot.Config.ingame_triggers), ("!mcbot", ""))
    check("a chat line that only mentions the bot is not an ask",
          bot.ask_request("that mcbot thing is silly", bot.Config.ingame_triggers), None)
    check("the trigger is case insensitive, because children type in caps",
          bot.ask_request("!MCBOT is the server up", bot.Config.ingame_triggers), ("!mcbot", "is the server up"))
    check("and leading whitespace in a chat line does not hide the trigger",
          bot.ask_request("  @mcbot   who is online", bot.Config.ingame_triggers), ("@mcbot", "who is online"))
    check("the tail handler uses that one decision rather than its own copy",
          "ask_request(text, Config.ingame_triggers)" in inspect.getsource(bot.McBot._game_line), True)

    # Shape, not behaviour: a helper inserted at the wrong indentation level ends a class and silently moves
    # every method after it into the helper. Python accepts it, the pod does not, and the failure otherwise
    # reads as an unrelated AttributeError a hundred lines away from the mistake.
    needed = ("post", "handle", "_run", "_resume", "_drain", "_cancel", "game_say", "_game_chunks",
              "_game_line", "_watch_game", "game_broadcast_lines", "_drain_game", "_while_busy_game")
    check("McBot is still one class holding every method the runtime calls on it",
          [n for n in needed if not hasattr(bot.McBot, n)], [])
    check("and the trigger decision is module level rather than hidden inside a method",
          hasattr(bot, "ask_request"), True)

    # The in-game half runs as one unit or not at all. The first deploy of it shipped the trigger words in a
    # ConfigMap and no password in the pod (the manifest had the comment and not the env entry), so the bot
    # was reading every chat line and able to answer none of them - the one outcome a player cannot tell apart
    # from being ignored. `load_config` cannot catch that: the credential is applied out of band.
    ok, why = bot.game_path_status("", "")
    check("with no way to answer, the game half is closed rather than half open", ok, False)
    check("and it names both settings that could fix it",
          ("MCBOT_RCON_HOST" in why, "MCBOT_RCON_PASSWORD" in why), (True, True))
    check("a host with no password is still closed, because the password is the out-of-band one",
          bot.game_path_status("mc.example", "")[0], False)
    check("a password with no host is closed too", bot.game_path_status("", "hunter2")[0], False)
    check("both halves present opens it, with nothing to explain",
          bot.game_path_status("mc.example", "hunter2"), (True, ""))
    check("and the startup path asks that function instead of guessing",
          "game_path_status(Config.rcon_host, Config.rcon_password)" in inspect.getsource(bot.McBot.setup_hook),
          True)

    long_bot = game_bot(["nope"])
    asyncio.run(long_bot._game_line("Steve", "!mcbot " + "x" * 200))
    check("an ask too long for chat is refused with somewhere better to ask it",
          any("too long for chat" in c for c in long_bot.rcon.commands), True)
    check("a too-long in-game ask never reaches the model", long_bot.pi.prompts, [])

    wrap = game_bot(["\n".join(f"line {i} of the explanation" for i in range(1, 12))])
    asyncio.run(wrap._game_line("Steve", "!mcbot explain the whitelist"))
    check("a long answer is split into chat-sized lines",
          all(len(c) <= len("say mcbot: ") + bot.Config.ingame_line_len for c in wrap.rcon.commands), True)
    answer_lines = [c for c in wrap.rcon.commands if "of the explanation" in c]
    check("and capped, with the truncation said out loud instead of silently dropped",
          (len(answer_lines), any("too long for chat" in c for c in wrap.rcon.commands)),
          (bot.Config.ingame_max_lines, True))
    check("no RCON command ever contains a newline, because a newline is a second command",
          [c for c in wrap.rcon.commands if "\n" in c or "\r" in c], [])

    fence = game_bot(["here:\n```\nstop the server\n```\ndone"])
    asyncio.run(fence._game_line("Steve", "!mcbot how do i restart"))
    check("everything sent into the game is said to everybody, never issued as a command to the server",
          bool(fence.rcon.commands) and all(c.startswith("say ") for c in fence.rcon.commands), True)
    check("a fenced block arrives as chat lines, never as a second command",
          (len(fence.rcon.commands), "say stop the server" in fence.rcon.commands), (3, True))
    # chunk_text() rewrites a fence to triple quotes so an unterminated fence cannot break a kids channel.
    # A chat window has no fence to close, so that punctuation must not arrive as noise either.
    check("the fence punctuation Discord needs does not reach the game as noise",
          [c for c in fence.rcon.commands if "\'\'\'" in c or "```" in c], [])

    # What a destructive in-game ask does, and it is a different thing from what it used to do. There is no
    # offer, no `!mcbot yes`, and no pending row: a chat window proves only that the speaker can join the
    # server, and a whisper to the person who asked is an edge nobody else asked for. The ask is answered in
    # public, with a pointer to the one surface that has a confirmation button.
    c1 = game_bot(["that would wipe the world. [[CONFIRM wipe: delete the world]]"])
    key_notch = -(1 + __import__("zlib").crc32(b"Notch"))
    key_steve = -(1 + __import__("zlib").crc32(b"Steve"))
    asyncio.run(c1._game_line("Notch", "!mcbot delete the world"))
    check("a destructive in-game ask opens nothing: no offer, no pending row, no button",
          (key_notch in bot.state.pending,
           conn.execute("select count(*) from confirm").fetchone()[0]), (False, 0))
    check("nothing was sent to the model beyond the ask itself", len(c1.pi.prompts), 1)
    check("the refusal is recorded as a refusal, and names what was wanted",
          (bot.state.last_decision["kind"], "wipe" in bot.state.last_decision["reason"]),
          ("refused", True))
    check("the player is told, in the same chat window, that in game means questions only",
          any("only answer questions" in c for c in c1.rcon.commands), True)
    check("and pointed at the surface that has a button",
          any("Discord" in c for c in c1.rcon.commands), True)
    check("the [[CONFIRM]] marker is taken out of what everybody reads",
          any("[[CONFIRM" in c for c in c1.rcon.commands), False)

    asyncio.run(c1._game_line("Notch", "!mcbot yes"))
    check("`!mcbot yes` is answered rather than left hanging, because players were taught to type it",
          any("nothing was waiting" in c for c in c1.rcon.commands), True)
    check("and it approves nothing: no second ask, nothing pending",
          (len(c1.pi.prompts), key_notch in bot.state.pending), (1, False))
    asyncio.run(c1._game_line("Steve", "!mcbot no"))
    check("so does `!mcbot no`, which now means nothing and says so", len(c1.pi.prompts), 1)

    # The read-only rule is stated to the model *and* written where the wrappers can enforce it. Asserting
    # only the prompt would let the enforcement rot; asserting only the file would let the model keep offering
    # things it cannot do.
    turn_rec = bot.read_turn_mode(bot.Config.turn_mode_path)
    check("a destructive in-game ask is still a read-only turn in the record the wrappers read",
          (turn_rec.get("readonly"), turn_rec.get("approved")), (True, False))

    q = game_bot(["first answer", "second answer"])

    async def queue_case():
        task = asyncio.create_task(q._game_line("Steve", "!mcbot first thing"))
        await asyncio.sleep(0.01)
        await q._game_line("Steve", "!mcbot second thing")
        await task
        await asyncio.sleep(0.05)
    asyncio.run(queue_case())
    check("a second in-game ask queues instead of being refused",
          any("number 1 in the queue" in c for c in q.rcon.commands), True)
    check("the queued ask is answered to the same player",
          [c for c in q.rcon.commands if c.startswith("msg Steve second") or "second answer" in c] != [], True)
    check("nothing is left waiting for that player", bot.state.game_queue.get(key_steve), None)

    s = game_bot(["working on it"])

    async def stop_case():
        task = asyncio.create_task(s._game_line("Steve", "!mcbot do the thing"))
        await asyncio.sleep(0.01)
        await s._game_line("Steve", "!mcbot also do the other thing")
        await s._game_line("Steve", "!mcbot stop")
        await task
    asyncio.run(stop_case())
    check("stop in game drops everything that player had queued", bot.state.game_queue.get(key_steve), None)

    dead = game_bot(["an answer"], fail=True)
    asyncio.run(dead._game_line("Steve", "!mcbot status"))
    check("a dead RCON session is reported rather than looking like a bot ignoring a child",
          bool(dead.game_error), True)

    bot.client = g
    ingame_health = bot.health_payload()["ingame"]
    check("healthz reports the two halves of the in-game path separately",
          ("tailing" in ingame_health, "rcon_connected" in ingame_health, "rcon_configured" in ingame_health),
          (True, True, True))
    check("healthz counts the in-game asks it has seen", ingame_health["asks_seen"] > 0, True)
    check("the in-game trigger words are reported, so a config mistake is visible",
          ingame_health["triggers"], [t for t in bot.Config.ingame_triggers])

    game_persona = open(os.path.join(PKG, "workspace", "AGENTS.md")).read()
    check("the persona knows the game is a place asks come from",
          "!mcbot" in game_persona and "inside Minecraft" in game_persona, True)
    # The persona is not the enforcement, but it is what decides whether mcbot *offers* something the tools
    # will refuse. A persona that still describes an in-game `yes` is a persona that promises a button nobody
    # can press, so the wording is asserted here rather than read once by eye.
    check("the persona says plainly that in game there are no changes, because there are no buttons",
          "In game there are no buttons, so in game there are no changes" in game_persona, True)
    check("the persona says `!mcbot yes` and `!mcbot no` mean nothing",
          "`!mcbot yes` and `!mcbot no` mean nothing" in game_persona, True)
    check("no leftover wording offers an in-game press",
          "!mcbot yes` from that same player" in game_persona, False)
    check("the persona names the sanctioned console bridge, and says mcbot does not whisper",
          ("mc-rcon" in game_persona, "mcbot does not whisper" in game_persona), (True, True))
    check("the persona knows kubectl is wrapped as well as scoped",
          "kubectl` on your PATH is a wrapper too" in game_persona, True)
    check("the persona knows a commit in this prefix is a deploy, and needs the button",
          "it is the deploy" in game_persona, True)
    check("the persona names the two capability switches, so a why-cant-you question has an answer",
          ("MCBOT_SERVER_ACTIONS" in game_persona, "MCBOT_INGAME_ACTIONS" in game_persona), (True, True))
    check("the persona is told an in-game answer is public, and what that rules out",
          "broadcast to everyone on the server" in game_persona, True)

    # /healthz is both the readiness and the liveness probe. A metric that raises must not become an outage.
    class PoisonedClient:
        def is_closed(self):
            raise RuntimeError("a health field blew up")

    saved_client = bot.client
    bot.client = PoisonedClient()
    try:
        raised = None
        try:
            asyncio.run(bot.healthz(None))
        except Exception as exc:  # noqa: BLE001 - the point is whether anything escapes
            raised = type(exc).__name__
        check("a poisoned health field cannot raise out of the probe handler", raised, None)
        body = bot.web.json_response.call_args[0][0]
        check("a broken metric is reported as a defect instead of pulling the pod out of rotation",
              ("error" in body, body.get("gateway_ready")), (True, False))
        check("and it is visible in last_error for whoever is reading healthz",
          "healthz" in (bot.state.last_error or ""), True)
    finally:
        bot.client = saved_client

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILURE(S): {', '.join(FAILED)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
