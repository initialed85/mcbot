#!/usr/bin/env python3
"""The wrappers' rules, asserted without a cluster, a game server, or a model.

`bin/gate.py` is where mcbot's capabilities are decided: which kubectl verbs a turn may run, which Minecraft
console commands are whispers, which are in-game actions, which are out-of-game operations, and what a plugin
jar needs. Those decisions are the security boundary of the whole app - the ServiceAccount RBAC bounds the
blast radius and this file bounds the intent - so they are asserted here rather than discovered by watching a
child ask for 64 diamonds and getting some.

Runs on the checkout with no dependencies, and the last section runs the real `bin/kubectl-shim` as a process
against a stub binary, because the PATH wiring (a wrapper that refuses, and a wrapper that can still reach the
real binary) is the part that only breaks in the built image.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE / "bin"
sys.path.insert(0, str(BIN))

import gate  # noqa: E402

FAILURES: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="mcbot-gate-test-"))


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        FAILURES.append(what)


def mode(*, readonly: bool = False, approved: bool = False, surface: str = "discord") -> dict:
    return {"ts": "test", "readonly": readonly, "approved": approved, "surface": surface, "text": ""}


def _push(env: dict) -> dict:
    """Install a wrapper-shaped environment; a value of None means *absent*, which is the interesting case.

    `MCBOT_SERVER_ACTIONS` unset is the deployment that never mentioned it, and that is the one the fail-closed
    default is for. A suite that can only set a variable to "0" tests the escape hatch, not the default.
    """
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old


def _pop(old: dict) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def case(label: str, *, env: dict, mode_rec: dict, argv: list[str], expect_allowed: bool) -> None:
    """One `kubectl ...` decision, with the environment the wrapper would actually have."""
    old = _push(env)
    try:
        allowed, verb, why = gate.decide_kubectl(argv, mode=mode_rec)
        check(allowed == expect_allowed,
              f"{label}: `kubectl {' '.join(argv)}` -> {'allowed' if allowed else 'refused'} "
              f"(verb={verb or '-'})" + ("" if allowed == expect_allowed else f" but expected {'allowed' if expect_allowed else 'refused'}: {why}"))
    finally:
        _pop(old)


def rcon_case(label: str, command: str, *, mode_rec: dict, expect_allowed: bool,
              env: dict | None = None) -> None:
    env = env or {}
    old = _push(env)
    try:
        allowed, kind, why = gate.decide_rcon(command, mode=mode_rec)
        check(allowed == expect_allowed,
              f"{label}: `{command}` -> {'allowed' if allowed else 'refused'} (class={kind})"
              + ("" if allowed == expect_allowed else f" but expected {'allowed' if expect_allowed else 'refused'}: {why}"))
    finally:
        _pop(old)


# The capability armed, for the sections below that ask what an *approved* turn may do. Off is the default and
# is asserted as such ("the switch absent, button pressed" below and in test-config.py); arming it here is what
# makes "the button is what authorises a change, not the switch" a testable claim rather than an argument.
os.environ["MCBOT_SERVER_ACTIONS"] = "1"

print("kubectl: reads are free in every turn")
DISCORD_UNPRESSED = mode()
DISCORD_APPROVED = mode(approved=True)
GAME = mode(readonly=True, surface="game")
for argv in (["get", "pods", "-n", "minecraft"], ["-n", "minecraft", "get", "statefulset", "server"],
             ["describe", "pod", "mc-0"], ["logs", "mc-0", "--tail=50"], ["top", "pods"],
             ["diff", "-f", "x.yaml"], ["wait", "--for=condition=ready", "pod/mc-0"],
             ["rollout", "status", "statefulset/server"], ["version", "--client"]):
    for label, m in (("unpressed discord", DISCORD_UNPRESSED), ("approved discord", DISCORD_APPROVED),
                     ("in-game", GAME)):
        check(gate.decide_kubectl(argv, mode=m)[0], f"{label}: `kubectl {' '.join(argv)}` is a read")

print("\nkubectl: every write verb needs a pressed button")
for verb in (["delete", "pod", "mc-0"], ["patch", "statefulset/server", "-p", "{}"],
             ["rollout", "restart", "statefulset/server"], ["apply", "-f", "x.yaml"],
             ["exec", "-it", "mc-0", "--", "bash"], ["scale", "statefulset/server", "--replicas=0"],
             ["label", "pod", "mc-0", "x=y"], ["delete", "pod", "-n", "minecraft", "mcbot-0"]):
    check(not gate.decide_kubectl(verb, mode=DISCORD_UNPRESSED)[0],
          f"unpressed discord: `kubectl {verb[0]}` refused")
    check(not gate.decide_kubectl(verb, mode=GAME)[0], f"in-game: `kubectl {verb[0]}` refused")
    check(gate.decide_kubectl(verb, mode=DISCORD_APPROVED)[0], f"approved discord: `kubectl {verb[0]}` allowed")

print("\nkubectl: a verb nobody has heard of classifies as a write")
check(gate.classify_kubectl(["some-new-verb", "thing"]) == ("write", "some-new-verb"),
      "unknown verb classifies as write")
check(gate.classify_kubectl([])[0] == "read", "no arguments at all is not a write")

print("\nkubectl: MCBOT_SERVER_ACTIONS=0 removes the capability, button included")
case("switch off", env={"MCBOT_SERVER_ACTIONS": "0"}, mode_rec=DISCORD_APPROVED,
     argv=["rollout", "restart", "statefulset/server"], expect_allowed=False)
case("switch off, reads survive", env={"MCBOT_SERVER_ACTIONS": "0"}, mode_rec=DISCORD_APPROVED,
     argv=["get", "pods"], expect_allowed=True)
case("switch on explicitly", env={"MCBOT_SERVER_ACTIONS": "1"}, mode_rec=DISCORD_APPROVED,
     argv=["rollout", "restart", "statefulset/server"], expect_allowed=True)
case("switch absent", env={"MCBOT_SERVER_ACTIONS": None}, mode_rec=DISCORD_APPROVED,
     argv=["rollout", "restart", "statefulset/server"], expect_allowed=False)
# The wording matters: a deployment whose capability is off must be told it is off. A refusal that blames a
# missing button sends somebody looking for a button that will never appear, because the switch is what keeps
# the feature out of the program.
_old = _push({"MCBOT_SERVER_ACTIONS": None})
try:
    why = gate.decide_kubectl(["rollout", "restart", "statefulset/server"], mode=DISCORD_APPROVED)[2]
finally:
    _pop(_old)
check("switched off" in why.lower() and "button" not in why.lower(),
      f"the refusal names the capability that is off, not a button that never appears: {why}")

print("\nno turn record at all means no writes (a bot that cannot tell its wrappers has lost them)")
check(not gate.decide_kubectl(["delete", "pod", "x"], mode={})[0], "empty mode: write refused")
check(gate.decide_kubectl(["get", "pods"], mode={})[0], "empty mode: read allowed")
missing = TMP / "definitely-not-here.json"
check(gate.turn_mode(str(missing)) == {}, "a missing mode file reads as empty rather than raising")
os.environ["MCBOT_TURN_MODE"] = str(missing)
check(not gate.readonly_turn() and not gate.decide_kubectl(["delete", "pod", "x"])[0],
      "a missing mode file still refuses writes (fail closed, not fail open)")
os.environ.pop("MCBOT_TURN_MODE", None)

print("\nmc-rcon: whispers are refused in every turn, approved ones included")
for command in ("msg Steve here is your base", "w Notch x", "tell Steve echo hi",
                "tellraw Steve {\"text\":\"your coords\"}"):
    for label, m in (("unpressed", DISCORD_UNPRESSED), ("approved", DISCORD_APPROVED), ("in-game", GAME)):
        allowed, kind, why = gate.decide_rcon(command, mode=m)
        check(not allowed and kind == "private", f"{label}: `{command}` refused as private")

print("\nmc-rcon: the public version of the same words is allowed")
for command in ('say base is at 100 64 -200', 'tellraw @a {"text":"server restarts in 2 minutes"}',
                'me looks around'):
    allowed, kind, why = gate.decide_rcon(command, mode=GAME)
    check(allowed, f"in-game: `{command}` is a broadcast to everybody")

print("\nmc-rcon: reads are free, including the two-word ones")
for command in ("list", "seed", "version", "whitelist list", "time query daytime", "weather query",
                "gamerule doDaylightCycle", "difficulty", "datapack list", "team list"):
    allowed, kind, why = gate.decide_rcon(command, mode=GAME)
    check(allowed and kind == "read", f"in-game: `{command}` is a read")

print("\nmc-rcon: in-game actions are refused by default, with or without a button")
for command in ("give Steve diamond 64", "tp Steve 100 64 -200", "summon wither",
                "gamemode creative Notch", "kick Steve", "ban Notch",
                "setblock 0 64 0 bedrock", "time set day", "weather thunder", "difficulty hard",
                "gamerule doDaylightCycle false", "whitelist off", "stop", "reload", "op Steve"):
    for label, m in (("unpressed", DISCORD_UNPRESSED), ("approved", DISCORD_APPROVED)):
        allowed, kind, why = gate.decide_rcon(command, mode=m)
        check(not allowed and kind == "world", f"{label}, default switches: `{command}` refused as in-game")

print("\nmc-rcon: MCBOT_INGAME_ACTIONS=1 makes them possible, but never from an in-game ask")
on = {"MCBOT_INGAME_ACTIONS": "1"}
for command in ("give Steve diamond 64", "kick Steve"):
    os.environ.update(on)
    try:
        allowed, kind, why = gate.decide_rcon(command, mode=DISCORD_APPROVED)
        check(allowed and kind == "world", f"ingame_actions on + pressed button: `{command}` allowed")
        check(not gate.decide_rcon(command, mode=mode(approved=True, readonly=True, surface="game"))[0],
              f"ingame_actions on + in-game ask: `{command}` still refused")
        check(not gate.decide_rcon(command, mode=DISCORD_UNPRESSED)[0],
              f"ingame_actions on + unpressed ask: `{command}` still refused")
    finally:
        os.environ.pop("MCBOT_INGAME_ACTIONS", None)

print("\nmc-rcon: the whitelist family is the out-of-game operation, and it needs the button")
allowed, kind, why = gate.decide_rcon("whitelist add mason_bee", mode=DISCORD_APPROVED)
check(allowed and kind == "ops", "approved discord: `whitelist add` is an out-of-game operation")
for label, m in (("unpressed", DISCORD_UNPRESSED), ("in-game", GAME)):
    allowed, kind, why = gate.decide_rcon("whitelist add mason_bee", mode=m)
    check(not allowed and kind == "ops", f"{label}: `whitelist add` refused until a button is pressed")
os.environ["MCBOT_SERVER_ACTIONS"] = "0"
try:
    check(not gate.decide_rcon("whitelist add mason_bee", mode=DISCORD_APPROVED)[0],
          "switched off: `whitelist add` refused even with a pressed button")
finally:
    os.environ.pop("MCBOT_SERVER_ACTIONS", None)

print("\nplugin jars need a name")
ok, why = gate.plugin_allowed("apps/minecraft/plugins/TreeCapitator999.jar")
check(not ok, f"no allowlist: a plugin jar is refused ({why})")
os.environ["MCBOT_PLUGIN_ALLOWLIST"] = "treecapitator999, LuckPerms"
try:
    ok, why = gate.plugin_allowed("apps/minecraft/plugins/TreeCapitator999.jar")
    check(ok, f"named on the allowlist: allowed ({why})")
    ok, why = gate.plugin_allowed("apps/minecraft/plugins/WorldEditCheat.jar")
    check(not ok, f"not on the allowlist: refused ({why})")
    check(gate.plugin_allowed("apps/minecraft/configmap.yaml")[0],
          "a yaml file is not a jar and is not judged by this rule")
finally:
    os.environ.pop("MCBOT_PLUGIN_ALLOWLIST", None)

print("\nthe audit line records both the decision and the switches that produced it")
audit_path = TMP / "gate.jsonl"
os.environ["MCBOT_AUDIT"] = str(audit_path)
try:
    gate.audit("kubectl", tool="kubectl", allowed=False, detail="delete: no button",
               argv=["kubectl", "delete", "pod", "x"], path=str(audit_path))
    rec = json.loads(audit_path.read_text().splitlines()[-1])
    check(rec["allowed"] is False and rec["tool"] == "kubectl" and "caps" in rec and "mode" in rec,
          "audit line carries the decision, the turn mode and the capability matrix")
finally:
    os.environ.pop("MCBOT_AUDIT", None)

print("\nthe wrapper as a process: refuses, does not run the real binary, and passes reads through")
stub = TMP / "kubectl-stub"
ran = TMP / "kubectl-ran"
stub.write_text("#!/bin/sh\necho ran\n")
stub.chmod(0o755)
env = dict(os.environ)
env.update({"MCBOT_KUBECTL_REAL": str(stub), "MCBOT_DATA": str(TMP), "MCBOT_TURN_MODE": str(TMP / "t.json"),
            "MCBOT_SERVER_ACTIONS": "1",
            # where mc-rcon finds rcon.py: /opt/mcbot in the image, this directory in a checkout.
            "MCBOT_LIB": str(HERE)})
TMP.joinpath("t.json").write_text(json.dumps(mode(readonly=True, surface="game")))
proc = subprocess.run([str(BIN / "kubectl-shim"), "delete", "pod", "mc-0"], env=env,
                      capture_output=True, text=True)
check(proc.returncode == 77, f"kubectl-shim refuses with a distinct exit code (got {proc.returncode})")
check(not ran.exists() and "ran" not in proc.stdout, "the real binary was not executed")
check("read-only" in proc.stderr or "confirmation button" in proc.stderr,
      f"the refusal says what to do instead: {proc.stderr.strip()[:120]}")

TMP.joinpath("t.json").write_text(json.dumps(mode(approved=True)))
proc = subprocess.run([str(BIN / "kubectl-shim"), "rollout", "restart", "statefulset/server"], env=env,
                      capture_output=True, text=True)
check(proc.returncode == 0 and "ran" in proc.stdout, "kubectl-shim execs the real binary for an approved write")

proc = subprocess.run([str(BIN / "kubectl-shim"), "logs", "mc-0", "--tail=5"], env=env,
                      capture_output=True, text=True)
check(proc.returncode == 0 and "ran" in proc.stdout, "kubectl-shim passes reads through in any turn")
audit_lines = (TMP / "audit" / "gate.jsonl").read_text().splitlines()
check(len(audit_lines) == 3, f"every call, refused or allowed, was audited ({len(audit_lines)} lines)")

print("\nmc-rcon: usage, and a refused write never reaches the socket")
proc = subprocess.run([str(BIN / "mc-rcon"), "--help"], env=env, capture_output=True, text=True)
check(proc.returncode == 0 and "mc-rcon list" in proc.stdout,
      f"--help is usage, not a command sent to the children's server (rc={proc.returncode})")
# An unapproved operation, with no RCON host or password configured at all. If the gate let it through the
# failure would be a connection error, which reads like a network problem; refusing first is what makes the
# refusal the last word.
TMP.joinpath("t.json").write_text(json.dumps(mode(readonly=True, surface="game")))
env_nonet = dict(env)
env_nonet.pop("MCBOT_RCON_HOST", None)
env_nonet.pop("MCBOT_RCON_PASSWORD", None)
# Host present, password absent: the credential is the only thing missing, so the failure has to name the
# credential. Two variables missing at once would let the message name the host and still "pass".
env_nopw = dict(env)
env_nopw.pop("MCBOT_RCON_PASSWORD", None)
env_nopw["MCBOT_RCON_HOST"] = "127.0.0.1"
proc = subprocess.run([str(BIN / "mc-rcon"), "whitelist", "add", "Notch"], env=env_nonet,
                      capture_output=True, text=True)
check(proc.returncode == 77 and "confirmation button" in proc.stderr,
      f"an in-game turn cannot run an operation, and says so before any socket ({proc.returncode})")
check("nothing was sent to the server" in proc.stderr, "the refusal states that nothing was sent")
TMP.joinpath("t.json").write_text(json.dumps(mode(approved=True)))

# A read that is allowed, in an environment with no password: the point is that it fails as a sentence a human
# can act on. `mc-rcon "list"` once died on `NameError: name 'RconError' is not defined`, which is the failure
# mode a lazy import gives you when only half the names are unpacked - and in a child's game it reads as the bot
# being broken, because `list` is the command mcbot runs most.
proc = subprocess.run([str(BIN / "mc-rcon"), "list"], env=env_nopw, capture_output=True, text=True)
check(proc.returncode != 0 and "Traceback" not in proc.stderr and "MCBOT_RCON_PASSWORD" in proc.stderr,
      f"an allowed read with no password says what is missing instead of crashing ({proc.stderr.strip()[:90]})")
proc = subprocess.run([str(BIN / "mc-rcon"), "status"], env=env_nonet, capture_output=True, text=True)
check(proc.returncode != 0 and "Traceback" not in (proc.stderr + proc.stdout),
      "mc-rcon status reports a failure rather than raising one")

print("\nmc-rcon --explain classifies without sending anything")
proc = subprocess.run([str(BIN / "mc-rcon"), "--explain", "msg", "Steve", "hi"], env=env,
                      capture_output=True, text=True)
check(proc.returncode == 77 and "private" in proc.stdout, "a whisper is refused and says why")
proc = subprocess.run([str(BIN / "mc-rcon"), "--explain", "list"], env=env, capture_output=True, text=True)
check(proc.returncode == 0 and "read" in proc.stdout, "list is a read")

# --- the arrangement the image actually uses: these wrappers are symlinks in /usr/local/bin -----------
# A wrapper that finds its own modules with `dirname(abspath(__file__))` works on a checkout and fails at
# `RUN ln -sf ... && mc-rcon --help` inside the build, because the link's directory is /usr/local/bin and
# gate.py is not there. All three import config_table, gate, or both, so all three are run this way.
with tempfile.TemporaryDirectory() as td:
    bindir = Path(td) / "bin"
    bindir.mkdir()
    # A turn file that is not there at all, so every wrapper under this arrangement is exercising the fail-closed
    # branch rather than inheriting the approved turn an earlier section wrote.
    link_env = dict(env, MCBOT_DATA=str(Path(td) / "turn.json"),
                    MCBOT_TURN_MODE=str(Path(td) / "absent-turn.json"),
                    MCBOT_AUDIT=str(Path(td) / "audit.jsonl"),
                    MCBOT_REPO_URL="git@example.test:org/mcbot.git", MCBOT_ALLOWED_PATHS="apps/minecraft/",
                    MCBOT_KUBECTL_REAL="/bin/true")
    for name in ("git-shim", "kubectl-shim", "mc-rcon"):
        link = bindir / name
        os.symlink(str(BIN / name), str(link))
        proc = subprocess.run([str(link), "--help"], env=link_env, capture_output=True, text=True)
        check(proc.returncode == 0 and "Traceback" not in proc.stderr,
              f"{name} resolves its own modules through the image's symlink")
    # And the refusal still happens through the link, which is the part that would be embarrassing to lose:
    # a wrapper that imports fine but forgets the gate is a wrapper with a nicer error message.
    # The capability is armed here, so the reason has to be the missing turn record rather than the switch:
    # a wrapper that reaches its modules but not its state file is the failure mode that would look fine.
    proc = subprocess.run([str(bindir / "kubectl-shim"), "delete", "pod", "x"],
                          env=dict(link_env, MCBOT_SERVER_ACTIONS="1"),
                          capture_output=True, text=True)
    check(proc.returncode == 77 and "button" in proc.stderr,
          "a refusal through the symlink still refuses, and still names the button it is waiting for")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all gate assertions hold")
