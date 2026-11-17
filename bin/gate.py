#!/usr/bin/env python3
"""The rules mcbot's tools enforce, in one file, outside the model.

mcbot's Pi agent runs with a real kubeconfig, a real git worktree and a real RCON password. What keeps that
safe is not the persona file: it is the wrappers installed earlier on PATH than the binaries they wrap, and
the ServiceAccount's RBAC. `bin/git-shim` is the repository boundary. This module is the *intent* boundary -
what kind of change this turn is allowed to make - and `bin/kubectl-shim` and `bin/mc-rcon` are the two
executables that apply it.

Why intent needs its own gate. An ask from inside Minecraft arrives as a line of chat from a name the game
asserts. There is no button on that screen and no membership roster behind it, so mcbot treats every in-game
ask as read-only. A prompt that says "do not change anything" holds only while the model agrees; the wrappers
are what makes it true when the model does not.

The mode itself is written by `bot.py:write_turn_mode` immediately before each ask, to `MCBOT_TURN_MODE`
(default `/data/turn.json`). PiRpc holds a lock, so exactly one turn is in flight and a wrapper reading the
file sees the turn it was called from.

Every decision is appended to `MCBOT_AUDIT` (default `/data/audit/gate.jsonl`) as one JSON line per call.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

# `config_table` lives one level up: /opt/mcbot/config_table.py next to /opt/mcbot/bin in the image, the
# repository root next to bin/ on a workstation. The gate reads its own settings from the table rather than
# being handed them by the program it is policing - the whole point of the gate is that it does not take the
# program's word for anything.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config_table as cfg  # noqa: E402

# A read-only turn may still run these. `wait` and `diff` are reads whatever a reader expects of them, and
# `logs` is how the bot tails the server console at all.
KUBECTL_READ = {
    "get", "describe", "logs", "top", "explain", "version", "diff", "wait", "api-resources",
    "api-versions", "options", "completion", "help", "auth",
}
# A read-only turn may not run these, and the reason is written next to each one because the list is the
# security boundary and a reviewer should be able to argue with it line by line.
KUBECTL_WRITE = {
    "apply", "create", "replace", "patch", "edit", "delete", "scale", "rollout", "set", "label",
    "annotate", "taint", "cordon", "uncordon", "drain", "run", "expose", "rolling-update",
    # `exec` is a root shell in the game container; `debug`/`attach`/`port-forward`/`cp` all reach inside a
    # pod too. In a read-only turn there is nothing they are needed for.
    "exec", "attach", "debug", "port-forward", "cp", "evict", "kill",
}
# Verbs that look like writes and are not, when the second word says so. `rollout status` is how you watch a
# restart somebody else already asked for; refusing it makes a read-only turn unable to answer "is it back up".
KUBECTL_READ_SUB = {
    ("rollout", "status"), ("rollout", "history"), ("rollout", "diff"),
    ("config", "view"), ("config", "current-context"), ("config", "get-contexts"),
}

# Minecraft console commands, classified the same way. `list` is a read and `whitelist add` is not, which is
# why the second word matters more than the first.
MC_READ_ALWAYS = {"list", "listplayers", "seed", "version", "help", "tb", "profile", "stats", "queryexec",
                  "locate"}
# Two-word forms that read. Every other form of these verbs changes something: `whitelist add`, `team add`,
# `time set`, `weather thunder`, `datapack enable`.
MC_READ_FORMS = {("whitelist", "list"), ("datapack", "list"), ("team", "list"), ("time", "query"),
                 ("weather", "query")}
# How many words the *question* form takes. `difficulty` asks and `difficulty peaceful` changes the world;
# `gamerule doDaylightCycle` asks and `gamerule doDaylightCycle false` changes it. Arity, not the first word,
# is what separates them.
MC_QUERY_ARITY = {"difficulty": 1, "gamerule": 2}
MC_BROADCAST_FIRST = {"say", "me"}
# A whisper is the thing the public rule exists to stop, so these are refused in every turn and not only a
# read-only one: `tellraw @a` is an announcement and `tellraw <name>` is a whisper with better formatting.
MC_PRIVATE_FIRST = {"msg", "w", "tell", "msgraw"}
MC_PUBLIC_TARGETS = {"@a"}

# What mcbot may actually *do* to the game, as an allowlist rather than a blacklist. This is the rule said
# by the person who plays on the server: out-of-game operations only.
#
# `whitelist add` is an out-of-game operation - it decides who may join, not what happens to a player who is
# already standing there. Everything on the other side of that line touches a world or a body, and mcbot does
# not do those at all, with or without a button: no give, no tp, no summon, no gamemode, no block edits.
MC_OPS_FORMS = {("whitelist", "add"), ("whitelist", "remove"), ("whitelist", "reload")}

# Named omissions, each one arguable on its own line. A reviewer should be able to disagree with a specific
# entry rather than with a wall of code.
MC_REFUSED_ALWAYS = {
    "kick": "moderation is a human call on a server with children on it",
    "ban": "same as kick, and harder to undo",
    "ban-ip": "same as ban, and it takes out everyone behind one router",
    "pardon": "only exists to undo a ban mcbot should not have made",
    "op": "handing out console power is not what mcbot is for",
    "deop": "removing an adult's permissions is not something a bot should be able to do",
    "stop": "a restart here is a rollout (kubectl), which leaves a record and keeps the pod spec in git",
    "reload": "Paper itself warns that /reload half-applies changes; the restart is the honest version",
    "whitelist-on-off": "switching the whitelist off lets anyone join the server",
    "gamemode": "changes how a player can break the world",
    "difficulty": "changes the world for everybody on it",
}


def mode_path() -> str:  # noqa: C901 - one reader, one definition
    # Must agree with the path entrypoint.sh writes its startup record to and bot.py writes each turn to. A gate
    # that reads a different file from the one the bot writes is a gate that always reads "no".
    return cfg.resolve("MCBOT_TURN_MODE")


def audit_path() -> str:
    return cfg.resolve("MCBOT_AUDIT")


def _flag(name: str) -> bool:
    return cfg.value_of(name)


def server_actions_on() -> bool:
    """Out-of-game operations: whitelist, config, restarts. **Off by default.**

    Flipping it off makes mcbot a pure oracle: it reads and explains everything about the server and changes
    nothing. That is the default now rather than the option, because this repository is public and a public
    repository should not ship a bot that is armed until somebody says otherwise. A deployment that wants the
    capability says so out loud: ours sets MCBOT_SERVER_ACTIONS=1 in a ConfigMap anyone can read.
    """
    return _flag("MCBOT_SERVER_ACTIONS")


def ingame_actions_on() -> bool:
    """In-game actions: give, tp, summon, gamemode, kick, ban. **Off by default.**

    Off because the person who plays on the server said so ("don't permit any in-game actions"), and because
    the two things it would do - hand out items and moderate other players - are the two a bot should not be
    doing to children's worlds. When it is switched on it still needs a pressed button on Discord; it never
    makes an in-game ask able to change anything.
    """
    return _flag("MCBOT_INGAME_ACTIONS")


def plugin_allowlist() -> set[str]:
    """Plugin names mcbot may install, lowercased, comma separated. Empty means none.

    "No stupid plugins" is only a rule if there is a list to argue about. An empty default means a plugin
    arrives by Ed naming it in the ConfigMap, not by the bot deciding it would be fun.
    """
    return {p.strip().lower() for p in cfg.value_of("MCBOT_PLUGIN_ALLOWLIST") if p.strip()}


def plugin_allowed(path: str) -> tuple[bool, str]:
    """Whether a staged file may carry a plugin jar, by name, against the allowlist.

    A jar is the one artifact mcbot could put on the server that nothing else on this list can police: it is
    arbitrary code running as the server process, in the one place the sandbox has no opinion about content.
    """
    base = os.path.basename(path).lower()
    if not base.endswith(".jar"):
        return True, "not a jar"
    name = base[:-4]
    allowed = plugin_allowlist()
    if not allowed:
        return False, "no plugin is allowed: MCBOT_PLUGIN_ALLOWLIST is empty"
    if name in allowed:
        return True, f"{name} is on the plugin allowlist"
    return False, f"{name} is not on the plugin allowlist ({', '.join(sorted(allowed))})"


def capabilities() -> dict:
    """The capability matrix, as the audit and /healthz both report it."""
    return {"server_actions": server_actions_on(), "ingame_actions": ingame_actions_on(),
            "plugins": sorted(plugin_allowlist())}


def turn_mode(path: Optional[str] = None) -> dict:
    """What kind of turn this is, as written by bot.py immediately before the ask.

    Missing or unreadable means **no writes**. That is the direction this one fails in on purpose: a bot that
    cannot tell its own wrappers what turn it is in has lost the only thing that made a write safe, so the
    writes stop and the failure shows up as refusals plus an empty `turn_mode` in /healthz rather than as a
    quiet return to "everything is allowed because nobody said otherwise".
    """
    try:
        with open(path or mode_path(), encoding="utf-8") as fh:
            rec = json.load(fh)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def readonly_turn(path: Optional[str] = None) -> bool:
    return bool(turn_mode(path).get("readonly"))


def audit(event: str, *, tool: str, allowed: bool, detail: str, argv: list[str],
          path: Optional[str] = None) -> None:
    """One line per tool call: what was asked, what the gate decided, and which turn it was asked in.

    Written append-only and never fatal: an audit failure must not become a way to suppress a refusal, so a
    failure to write is reported on stderr and the decision stands.
    """
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, "tool": tool,
           "allowed": allowed, "detail": detail, "argv": argv[:12], "mode": turn_mode(),
           "caps": capabilities()}
    target = path or audit_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"mcbot gate: could not write {target}: {exc}", file=sys.stderr)


def deny_message(tool: str, verb: str, mode: dict) -> str:
    """Refusal text the model can act on, not just read.

    A refusal that only says "denied" leaves an agent trying four variations of the same denied thing in one
    turn. Each of these says what *is* allowed next, so the turn ends with a sentence to the player instead of
    a loop against the same wall.
    """
    surface = mode.get("surface") or "a turn whose mode file could not be read"
    return (f"mcbot: {tool} {verb} is refused. This ask came from {surface} and nothing has been confirmed "
            f"for it. Answer the question, and if someone wants the server changed, say what it would do and "
            f"that it needs the confirmation button in the Discord channel.")


# Flags that take a value on the next argument. Without this list, `kubectl -n minecraft get pods` reads
# "minecraft" as the verb and classifies a `get` as an unknown - therefore a write - which is the kind of bug
# that makes a read-only bot look broken in the middle of answering "is the server up".
KUBECTL_VALUE_FLAGS = {
    "-n", "--namespace", "-l", "--selector", "-c", "--container", "-o", "--output", "-f", "--filename",
    "--field-selector", "--kubeconfig", "--context", "--cluster", "--server", "--request-timeout",
    "--timeout", "--tail", "--chunkSize", "--limit-bytes", "--prefix", "--from", "--to", "-p", "--patch",
    "--type", "--replicas", "--image", "--filename", "--template", "--sort-by", "--container-selector",
    "--field-selector", "--selector", "--overwrite",  # `kubectl apply --overwrite` is a boolean; harmless here
}


def classify_kubectl(argv: list[str]) -> tuple[str, str]:
    """`(classification, verb)` for a kubectl invocation.

    An unrecognised verb classifies as a write, which is the safe direction for a verb this file has never
    heard of: an unknown that turns out to be `drain` should be refused, and an unknown that turns out to be
    `get` will show up as a refusal in the audit log where somebody can see it.
    """
    words: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            # `--flag=value` carries its own value; a bare `--flag` swallows the next argument.
            skip_next = "=" not in arg and arg in KUBECTL_VALUE_FLAGS
            continue
        words.append(arg)
    if not words:
        return "read", ""
    verb = words[0].lower()
    if not verb:
        return "read", ""
    if len(words) > 1:
        pair = (verb, words[1].lower())
        if pair in KUBECTL_READ_SUB:
            return "read", " ".join(pair)
    if verb in KUBECTL_READ:
        return "read", verb
    if verb in KUBECTL_WRITE:
        return "write", verb
    return "write", verb


def classify_rcon(command: str) -> tuple[str, str]:  # noqa: A003 - kinds are read|broadcast|private|ops|world
    """`(classification, reason)` for one Minecraft console command.

    Two-word commands are classified by their second word because `whitelist list` and `whitelist remove`
    are not the same kind of act. `tellraw` is the interesting one: it is an announcement or a whisper
    depending on its target, so the target is checked rather than the verb. An command this file has never
    heard of classifies as a write, which is the safe direction for an unknown.
    """
    words = (command or "").strip().split()
    if not words:
        return "refused", "empty command"
    first = words[0].lower().lstrip("/")
    second = words[1].lower() if len(words) > 1 else ""

    if first in MC_PRIVATE_FIRST:
        return "private", f"{first} whispers to one player"
    if first == "tellraw":
        if second in MC_PUBLIC_TARGETS or second.startswith("@a"):
            return "broadcast", "tellraw to everybody"
        return "private", f"tellraw targets {second or 'nobody'}, not @a"
    if first in MC_BROADCAST_FIRST:
        return "broadcast", f"{first} is said to everybody"
    if first in MC_READ_ALWAYS:
        return "read", first
    # Reads first: `whitelist list` is an operation-shaped word that answers a question, and an in-game ask
    # must be able to answer "who is on the whitelist" without a button.
    if (first, second) in MC_READ_FORMS:
        return "read", f"{first} {second}"
    if (first, second) in MC_OPS_FORMS:
        return "ops", f"{first} {second}"
    if first in MC_QUERY_ARITY:
        if len(words) <= MC_QUERY_ARITY[first]:
            return "read", f"{first} (query)"
        return "world", f"{first} with a value changes the world for everyone on it"
    if first == "whitelist" and second in {"on", "off"}:
        return "world", "turning the whitelist off lets anyone join the server"
    return "world", first


def decide_rcon(command: str, mode: Optional[dict] = None) -> tuple[bool, str, str]:
    """`(allowed, classification, why)` for one Minecraft console command in this turn.

    Three gates, in order of how absolute they are:

    1. whispers are refused in every turn - the public rule is not a per-ask setting;
    2. in-game (world, body, inventory, moderation) actions are refused in every turn, which is the server
       owner's rule and the reason the confirm button never appears for "give me 64 diamonds";
    3. out-of-game operations (the whitelist family) need an approved turn, meaning a confirmation button
       was actually pressed for this ask, so mc-rcon cannot be reached around by an unpressed ask.
    """
    mode = mode if mode is not None else turn_mode()
    kind, reason = classify_rcon(command)
    verb = (command or "").strip().split()[0] if (command or "").strip() else ""
    if kind == "private":
        return False, kind, ("mcbot refuses private chat: every answer mcbot gives is said in front of "
                             "everybody on the server, so nobody gets an edge nobody asked for")
    if kind == "world":
        if not ingame_actions_on():
            named = MC_REFUSED_ALWAYS.get(verb.lower(), "it changes the world a player is standing in")
            return False, kind, (f"mcbot does not do in-game actions ({verb}: {named}). They are switched off "
                                 f"in its config (MCBOT_INGAME_ACTIONS=0) and no confirmation button changes "
                                 f"that - Ed flips the switch. Ask an adult with op to do it in game")
        if not (mode.get("approved") and not mode.get("readonly")):
            return False, kind, ("in-game actions are switched on in mcbot's config, but this one has no "
                                 "pressed confirmation button yet, and it will never come from an in-game ask")
    if kind == "ops":
        if not server_actions_on():
            return False, kind, ("out-of-game actions are switched off in mcbot's config "
                                 "(MCBOT_SERVER_ACTIONS=0), so it answers questions and changes nothing")
        if not (mode.get("approved") and not mode.get("readonly")):
            return False, kind, ("mcbot will not run that until somebody presses the confirmation button for "
                                 "it in the Discord channel")
    return True, kind, reason


def decide_kubectl(argv: list[str], mode: Optional[dict] = None) -> tuple[bool, str, str]:
    """`(allowed, verb, why)` for one kubectl invocation in this turn.

    Reads are free in every turn - status, logs, and `rollout status` are what the oracle is for. Every write
    verb needs a pressed confirmation button, including `exec`, which is a shell in the game container and has
    no read-only use. A Discord ask that has not been confirmed yet may describe a change; it may not perform
    one, and this is where that rule stops being a polite instruction.
    """
    mode = mode if mode is not None else turn_mode()
    kind, verb = classify_kubectl(argv)
    if kind != "write":
        return True, verb, kind
    if not server_actions_on():
        return False, verb, ("kubectl " + verb + " is refused: out-of-game actions are switched off in "
                             "mcbot's config (MCBOT_SERVER_ACTIONS=0), so it answers questions and changes "
                             "nothing")
    if not (mode.get("approved") and not mode.get("readonly")):
        return False, verb, deny_message("kubectl", verb, mode)
    return True, verb, kind
