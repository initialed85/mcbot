#!/usr/bin/env python3
"""Construct the real bot, against the image's real dependency set.

    docker run --rm --entrypoint python3 <image> /opt/mcbot/test-image-smoke.py

Why this is not covered by test-bot-policy.py: that suite stubs discord so it can run on a bare
python:3.12-slim with no secrets, and a stub can only prove the decision functions decide correctly. It
cannot tell you a name does not exist in the library. bot.py built `discord.CommandTree`, which discord.py
2.7 does not export, so the policy suite passed, the import smoke test passed, the image published, and
the pod then died at `McBot()` in CrashLoopBackOff with `AttributeError: module 'discord' has no attribute
'CommandTree'`. Importing a module does not evaluate the names inside a constructor; constructing it does.

So this file does the one thing the stubs cannot: build the object with the genuine library. It needs no
secrets and opens no socket - McBot.__init__ reads nothing from Config (setup_hook is where Config is used,
and setup_hook only runs inside login), and the Config defaults are inert on purpose.

Expected failures are the interesting ones: a renamed attribute, a discord.py upgrade that moves
app_commands, or a Config default that stops being inert all surface here at build time instead of in a
CrashLoopBackOff at 03:00.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin"))

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name}: expected {want!r}, got {got!r}")
        FAILED.append(name)


def call_sites(discord, text: str, filename: str):
    """Every call site in `text` that calls a discord.py method, resolved to the library object it calls.

    Split out of the checker so it can be pointed at a snippet as well as at bot.py: a checker that silently
    examines nothing is worse than no checker, because it reads like coverage.

    The names that matter here are `bot`/`client`/`self` for the client, `<client>.tree` for the command tree,
    and `discord.app_commands.*`. The tree is the case that was missing, and it cost a pod: mcbot registered
    `/mc` with `dm_permission=False`, `CommandTree.command()` rejected it at startup, and every check in
    test-bot-policy.py still passed because none of them looks at a library signature.
    """
    tree = ast.parse(text, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        parts: list[str] = []
        cur = node.func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            continue
        parts.append(cur.id)
        parts.reverse()

        target = None
        if len(parts) >= 3 and parts[0] in ("bot", "client", "self") and parts[1] == "tree":
            target = discord.app_commands.CommandTree
        elif len(parts) >= 3 and parts[0] == "discord" and parts[1] == "app_commands":
            target = discord.app_commands
        elif len(parts) == 2 and parts[0] in ("bot", "client", "self"):
            target = discord.Client
        if target is None:
            continue
        yield node.lineno, target, parts[-1], [k.arg for k in node.keywords if k.arg]


def check_library_calls(discord) -> int:
    """Keywords bot.py passes to discord.py methods must be keywords the real library accepts.

    Three of this bot's startup crashes were library API drift: `discord.CommandTree` (does not exist in 2.x),
    and `bot.start(token, log_handler=None)` (a kwarg 2.x moved from start() to __init__). Parsing the call
    sites and checking them against the live signatures turns that whole class into a build-time failure. Only
    keyword arguments are checked - the drift that actually bit was keywords - and calls that take **kwargs are
    skipped, because anything is valid there.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    checked = 0
    for lineno, target, attr, keywords in call_sites(discord, text, path):
        method = getattr(target, attr, None)
        if method is None:
            # `self._react` and `self._write` are this bot's own methods and none of the library's business, so
            # an attribute the library does not have is normally not a call on the library at all. On the command
            # tree and in `discord.app_commands` there is no such thing as a private method, so a name the
            # library does not have is exactly the drift worth failing on.
            if target is discord.app_commands.CommandTree or target is discord.app_commands:
                check(f"bot.py:{lineno} {target.__name__}.{attr} exists in discord.py {discord.__version__}",
                      False, True)
                checked += 1
            continue
        try:
            params = inspect.signature(method).parameters
        except (TypeError, ValueError):
            continue
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            continue
        for kw in keywords:
            check(f"bot.py:{lineno} {target.__name__}.{attr}(**{kw}) is a real parameter",
                  kw in params, True)
            checked += 1
    return checked


def check_checker_covers_the_tree(discord) -> None:
    """Assert the checker sees command-tree calls, by feeding it the mistake that actually shipped.

    `dm_permission` is not a parameter of `CommandTree.command()` in discord.py 2.7. The snippet below is that
    bug, kept here so the checker cannot quietly stop covering the call site it was written for.
    """
    snippet = ("async def main(client):\n"
               "    @client.tree.command(name='mc', description='d', dm_permission=False)\n"
               "    async def mc(interaction): pass\n")
    found = list(call_sites(discord, snippet, "<snippet>"))
    check("the call-site checker resolves `client.tree.command(...)`", len(found), 1)
    if found:
        _, target, attr, keywords = found[0]
        params = inspect.signature(getattr(target, attr)).parameters
        check("and it reports `dm_permission` on the tree as not a real parameter",
              "dm_permission" in params, False)
    real = list(call_sites(discord, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "bot.py"), encoding="utf-8").read(), "bot.py"))
    tree_sites = [s for s in real if s[1] is discord.app_commands.CommandTree]
    check("bot.py registers slash commands through the tree, and those sites are checked",
          len(tree_sites) >= 2, True)


def check_path_wiring() -> int:
    """The sandbox is a PATH lookup, so assert what PATH resolves to in the built image.

    Run as `--path`, after the Dockerfile has made the links. Every safety property of bin/git-shim,
    bin/kubectl-shim and bin/mc-rcon rests on one fact: the name the model types resolves to the wrapper, and
    the wrapper can still reach the binary it wraps. A COPY that misses a file, a chmod that misses an +x, a
    link written to the wrong directory or a PATH change in a base image all remove that property while leaving
    the bot perfectly able to start - so the failure would surface as a turn where the model edited something
    it should not have, rather than as a broken pod.
    """
    import shutil

    wrapped = {"git": "/opt/mcbot/bin/git-shim", "kubectl": "/opt/mcbot/bin/kubectl-shim",
               "mc-rcon": "/opt/mcbot/bin/mc-rcon"}
    real_env = {"git": "MCBOT_GIT_REAL", "kubectl": "MCBOT_KUBECTL_REAL"}
    bad = 0
    for name, target in wrapped.items():
        found = shutil.which(name) or ""
        check(f"`{name}` on PATH is the wrapper", os.path.realpath(found), os.path.realpath(target))
        bad += (os.path.realpath(found) != os.path.realpath(target))
        check(f"{name} wrapper is executable", bool(found) and os.access(found, os.X_OK), True)
        bad += not (bool(found) and os.access(found, os.X_OK))
        env_name = real_env.get(name)
        if env_name:
            real = os.environ.get(env_name, "")
            check(f"{env_name} points at a real binary the wrapper can exec",
                  (bool(real), os.path.isfile(real), os.access(real, os.X_OK)), (True, True, True))
            bad += not (bool(real) and os.path.isfile(real) and os.access(real, os.X_OK))
            check(f"{env_name} is not the wrapper itself (recursion guard)",
                  os.path.realpath(real) != os.path.realpath(target), True)
            bad += os.path.realpath(real) == os.path.realpath(target)

    # The gate is importable from the running image and refuses the two calls that matter, with no turn record
    # present - which is the state a hand-run command inside the pod would be in.
    import gate
    allowed, reason, kind = gate.decide_kubectl(["-n", "minecraft", "apply", "-f", "statefulset.yaml"],
                                                {"readonly": False, "approved": False, "surface": "test"})
    check("a kubectl write is refused in a turn with no pressed button", allowed, False)
    allowed, reason, kind = gate.decide_rcon("msg Steve here is the seed",
                                             {"readonly": False, "approved": True, "surface": "test"})
    check("a whisper is refused even in a confirmed turn", allowed, False)
    # Read bot.py rather than import it: this half of the smoke test is about the filesystem the process will
    # see, and importing bot pulls in discord for a check that needs none of it.
    bot_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"), encoding="utf-8").read()
    check("bot.py writes the turn record the wrappers read, on every ask",
          ("write_turn_mode(" in bot_src, "Config.turn_mode_path" in bot_src), (True, True))
    return bad


# A provider default must be a name, not a place. Written as markers rather than as a list of known private
# hosts, because a public repository that names somebody's hosts to prove it does not use them is its own leak.
ENDPOINT_MARKERS = ("://", ".svc", ".cluster.local", "localhost", "10.10.", "172.20.")


def check_path_wiring_note() -> None:
    print("asserting the *image's* PATH wiring: run this mode inside the built container "
          "(docker run --entrypoint python3 <image> /opt/mcbot/test-image-smoke.py --path). "
          "On a workstation the wrappers are not installed, and it fails for that reason alone.")


def main() -> int:
    # Scrub first, or the suite is asserting the laptop. A workstation that runs an agent already has
    # `PI_PROVIDER` and `PI_MODEL` set to that agent's inference deployment, and a check written as "with
    # nothing set" then reads somebody else's endpoint and passes for the wrong reason - which is worse than
    # failing, because it looks like a default that works.
    import config_table as cfg
    for name in list(os.environ):
        if name.startswith("MCBOT_") or name.startswith("PI_"):
            del os.environ[name]

    if "--path" in sys.argv:
        check_path_wiring_note()
        bad = check_path_wiring()
        print()
        print("ALL PASS (PATH wiring)" if bad == 0 else f"{bad} FAILURE(S) in PATH wiring")
        return 0 if bad == 0 else 1

    import discord

    import bot
    import pi_rpc

    check("discord.py is the pinned major", discord.__version__.split(".")[0], "2")

    # The constructor is the whole point of this file.
    client = bot.McBot()
    check("McBot() constructs", type(client).__name__, "McBot")
    check("the tree is a real app_commands tree",
          isinstance(client.tree, discord.app_commands.CommandTree), True)
    check("tree has .command for registration", callable(getattr(client.tree, "command", None)), True)
    check("tree has .sync for setup_hook", callable(getattr(client.tree, "sync", None)), True)
    check("tree is bound to this client", client.tree.client is client, True)

    # Intents the bot claims to need, on the real Intents object rather than the stub's namespace.
    for intent in ("guilds", "messages", "message_content"):
        check(f"intent {intent} requested", bool(getattr(client.intents, intent)), True)
    # Bit-level check on message_content, because whether Discord treats it as privileged is a developer
    # portal switch and not something the library exposes (`Intents.privileged` does not exist in 2.7).
    # If the portal switch is off the connection still succeeds and every message arrives with empty
    # content, so the only in-code defence is asserting the request is unambiguous here.
    mc_bit = discord.Intents(message_content=True).value
    check("message_content bit set on the client", client.intents.value & mc_bit, mc_bit)

    # Inert defaults: importing must not require credentials, and an unloaded Config must not authorise.
    check("no credentials needed to import", bot.Config.loaded, False)
    check("unloaded Config has no token", bot.Config.token, "")
    check("unloaded Config authorises nobody", bot.Config.approvers, set())
    check("unloaded Config has no approver roles", bot.Config.approver_roles, set())
    # An unloaded Config must serve no servers either: guild scoping is what makes a wrong-invite deploy
    # fail as "ignored" rather than as "answers everywhere".
    check("unloaded Config serves no servers", bot.Config.guild_ids, set())
    # The adult filter was removed by decision, so the default is asserted against the real Config object:
    # a typo that flipped it back to allowlist would otherwise only show up as mcbot refusing everybody. The
    # shipped default is the fail-closed one; a deployment that wants `anyone` has to say so in its manifest.
    check("approver mode defaults to allowlist", bot.Config.approver_mode, "allowlist")
    # Context and vision are real library surface too: the context read is Messageable.history and the
    # transcription path needs Attachment's content_type/size, which is what the guards read.
    check("discord.abc.Messageable.history exists (the context read calls it)",
          callable(getattr(discord.abc.Messageable, "history", None)), True)
    check("discord.Attachment exposes content_type and size (the image guards read them)",
          (hasattr(discord.Attachment, "content_type"), hasattr(discord.Attachment, "size")), (True, True))
    check("context depth defaults to 20 messages", bot.Config.context_messages, 20)

    # The confirmation buttons rest on this premise, and only the real library can assert it: discord.py 2.7 has
    # no `View.message`. The first ConfirmView reached for it in `_disable()`, which in the live pod surfaced as
    # `Task exception was never retrieved ... AttributeError: 'ConfirmView' object has no attribute 'message'` on
    # every expiry - and on a press, where it consumed the pending and closed the confirmation row before the
    # approved change ran. A stubbed discord cannot tell you a name does not exist, so it is checked here.
    check("discord.ui.View has no message attribute in this library", hasattr(discord.ui.View, "message"), False)
    conf_view = bot.ConfirmView(client, bot.Pending(kind="wipe", detail="detail", question="question",
                                                    author_id=0, channel_id=1))
    # store is opened in setup_hook, which does not run here, and expiry closes its row - so the smoke test
    # supplies a throwaway one rather than letting the view reach for a store that was never opened.
    client.store = bot.Store(os.path.join(tempfile.gettempdir(), "mcbot-smoke.db"))
    check("the view carries its own target instead", getattr(conf_view, "target", "missing"), None)
    check("both buttons are attached", [type(c).__name__ for c in conf_view.children], ["Button", "Button"])
    check("the view expires on the same clock as the pending it guards", conf_view.timeout, 300)

    def quiet(coro):
        try:
            asyncio.run(coro)
            return None
        except Exception as exc:  # noqa: BLE001
            return repr(exc)

    check("greying out an offer that never reached Discord is quiet, not a traceback",
          quiet(conf_view._disable()), None)
    check("and letting it expire is quiet too", quiet(conf_view.on_timeout()), None)
    check("the shipped vision default is no endpoint at all", bot.Config.vision_url, "")
    check("allowlist semantics survive (empty lists authorise nobody in that mode)",
          (bot.Config.approvers, bot.Config.approver_roles), (set(), set()))

    # The reaction lifecycle is real library surface: 👀/👍/👎/❌/❓ are add_reaction calls and the
    # clarification thread is create_thread. Both are asserted on discord.Message, not on the stub.
    check("discord.Message.add_reaction exists (the lifecycle emoji calls it)",
          callable(getattr(discord.Message, "add_reaction", None)), True)
    check("discord.Message.create_thread exists (a clarification is answered in a thread)",
          callable(getattr(discord.Message, "create_thread", None)), True)

    bot_src = open(bot.__file__).read()
    check("the invite link asks for Add Reactions, which the emoji lifecycle needs",
          "add_reactions=True" in bot_src, True)
    check("no adult-filter wording left in the refusals",
          any("grown" in s.lower() or "adult" in s.lower() for s in bot.REFUSALS), False)

    # The decision log is the observability the first live deploy had none of, and `bot._deliver` catches
    # `discord.HTTPException` by name at module top level - a name the policy stub now provides, so it is
    # asserted against the library here as well.
    check("discord.HTTPException exists (bot.py catches it at top level)",
          hasattr(discord, "HTTPException"), True)
    check("discord.Message.reply exists (every refusal goes through it)",
          callable(getattr(discord.Message, "reply", None)), True)
    check("decision() is callable", callable(getattr(bot, "decision", None)), True)
    check("refusals are worded distinctly (Discord drops repeated identical content)",
          len(set(bot.REFUSALS)), len(bot.REFUSALS))

    # The bridge the gateway supervises; the RPC transport is the other half of the image's contract.
    bridge = pi_rpc.PiRpc(cwd="/tmp", provider="example-provider", model="auto")
    check("PiRpc constructs without a child process", type(bridge).__name__, "PiRpc")
    # No inference provider is remembered in the library: with no arguments it takes the configured one, and
    # the configured one is empty until a deployment supplies an endpoint.
    bare = pi_rpc.PiRpc(cwd="/tmp")
    # Nothing inference-shaped is remembered inside the bridge: with no arguments it resolves the configured
    # provider through the table, and the table's default is a word, not a host.
    check("a bare PiRpc takes its provider from the table, not from a literal in pi_rpc",
          bare.provider, str(cfg.value_of("PI_PROVIDER", {})))
    check("and that default is a word rather than somebody's endpoint",
          [h for h in ENDPOINT_MARKERS if h in bare.provider], [])
    declared_models = cfg.value_of("PI_MODEL", {})
    first = declared_models[0] if isinstance(declared_models, tuple) else declared_models
    check("and it defaults to the first configured model id", bare.model, str(first))

    checked = check_library_calls(discord)
    # Call sites examined, not keyword count: clean code is allowed to pass none.
    check("signature check actually examined client call sites", checked > 0, True)
    check_checker_covers_the_tree(discord)

    # --- the in-game request path -------------------------------------------
    # 1. RCON framing, from the image's own module. The interesting failure is not "the socket broke"; it is
    #    an answer that contains a newline turning into a second command run against the children's server.
    import rcon as rcon_mod

    smuggled = 0
    for bad in ("list\nsay hacked", "list\x00drop", "list\rlist", "   "):
        try:
            rcon_mod.one_command(bad)
        except rcon_mod.RconError:
            smuggled += 1
    check("an answer can never smuggle a second RCON command", smuggled, 4)
    check("a legitimate command still passes the guard",
          rcon_mod.one_command("msg Steve 3 players online"), "msg Steve 3 players online")
    # The guard is only worth having if the path the bot actually uses goes through it. A check that tests a
    # helper nobody calls is how a guard survives a refactor and stops mattering.
    check("command() itself is the guarded path", "one_command(text)" in inspect.getsource(rcon_mod.Rcon.command),
          True)

    # 2. The log format is not invented here. These two byte strings were taken out of `kubectl logs mc-0`
    #    on the running server, colour codes and the console's `\x1b[K` redraw included: Paper's appender
    #    writes them even into a non-TTY tail, so a parser written against a tidy imaginary format fails here.
    real_chat = b'>....\r\x1b[K[07:18:30 INFO]: <Steve> \\'
    real_join = b'>....\r\x1b[K[06:57:00 INFO]: System chat: \x1b[93mNotch joined the game\x1b[0m'
    synthetic_ask = b'[07:18:41 INFO]: <Steve> !mcbot who is online'

    def parse(raw: bytes):
        line = bot.ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).rstrip()
        m = bot.CHAT_LINE_RE.search(line)
        return (m.group("name"), m.group("text")) if m else None

    check("a real chat line from the running server parses", parse(real_chat), ("Steve", "\\"))
    check("a join/leave line is not mistaken for chat", parse(real_join), None)
    parsed = parse(synthetic_ask)
    check("a triggered question is recognised",
          bot.ask_request(parsed[1], bot.Config.ingame_triggers) if parsed else None,
          ("!mcbot", "who is online"))
    check("a chat line that merely mentions the bot is not an ask",
          bot.ask_request("that mcbot thing is silly", bot.Config.ingame_triggers), None)

    # 3. Whatever reaches the game is a message to a player. A chat window has no markdown, so the markdown
    #    mcbot uses on Discord must not arrive as punctuation - and a fence that chunk_text() rewrote for
    #    Discord must not show up in game as three stray quotes.
    chunks = bot.McBot._game_chunks("**Online:** 3\n\n```\nlist\n```\nsee https://example.invalid/minecraft")
    check("an in-game answer is plain lines with no markdown left behind",
          [c for c in chunks if "**" in c or "```" in c or chr(39) * 3 in c], [])
    check("every line fits in one chat line", [c for c in chunks if len(c) > 110], [])

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILURE(S): {', '.join(FAILED)}")
        return 1
    print(f"ALL PASS (discord.py {discord.__version__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
