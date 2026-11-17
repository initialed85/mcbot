#!/usr/bin/env bash
# Prove the git wrapper actually holds, rather than assuming it does.
#
#   uv run bash containers/minecraft-bot/test-git-shim.sh
#
# Builds a throwaway clone with the allowed prefix and some files outside it, then checks that
# every "should refuse" case exits non-zero and every "should allow" case exits zero.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIM="$HERE/bin/git-shim"
PREFIX="apps/minecraft/"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

# The real git, deliberately *not* `command -v git`. Inside the mcbot image /usr/bin/git is shadowed by
# the wrapper, and this suite used to take REAL_GIT from PATH - so on the host it passed, and in the
# built image it set MCBOT_GIT_REAL to the wrapper itself and built its fixtures
# with a sandbox that correctly refused, failing 6 checks. Take the first PATH hit that is not the
# wrapper, and refuse to run at all rather than recurse if none is found.
REAL_GIT=""
SHIM_REAL="$(readlink -f "$SHIM")"
for dir in ${PATH//:/ }; do
  cand="$dir/git"
  [ -x "$cand" ] || continue
  [ "$(readlink -f "$cand")" = "$SHIM_REAL" ] && continue
  head -c 2 "$cand" 2>/dev/null | grep -q '#!' && continue   # a script is not the binary
  REAL_GIT="$cand"
  break
done
if [ -z "$REAL_GIT" ]; then
  for cand in /usr/bin/git /bin/git /usr/local/git/bin/git; do
    [ -x "$cand" ] && { REAL_GIT="$cand"; break; }
  done
fi
if [ -z "$REAL_GIT" ] || ! "$REAL_GIT" --version >/dev/null 2>&1; then
  echo "FAIL: could not find a real git to drive the fixtures (would have recursed into the wrapper)"
  exit 1
fi
echo "harness: wrapper=$SHIM real-git=$REAL_GIT ($("$REAL_GIT" --version | head -1))"

export MCBOT_GIT_REAL="$REAL_GIT"
export MCBOT_REPO="$WORK/repo"
export MCBOT_ALLOWED_PREFIX="$PREFIX"
export MCBOT_GIT_LOG="$WORK/audit.jsonl"
export MCBOT_ALLOWED_BRANCH=master
# bin/gate.py writes its own audit next to MCBOT_DATA, and `bin/git-shim` reads the turn record from
# MCBOT_TURN_MODE. Both have to point inside the throwaway directory or this suite depends on the machine
# running it having a writable /data.
export MCBOT_DATA="$WORK/data"
mkdir -p "$MCBOT_DATA"
TURN="$WORK/turn.json"
export MCBOT_TURN_MODE="$TURN"

# The turn record bot.py writes before every ask. `approved` means a confirmation button was pressed for this
# specific ask, which is what makes a publish allowed. Most checks below want the confirmed state, so the
# harness starts there and then deliberately breaks it, one case at a time.
turn() { # approved readonly surface
  printf '{"ts":"test","approved":%s,"readonly":%s,"surface":"%s"}' "$1" "$2" "$3" > "$TURN"
}
turn true false discord
export MCBOT_SERVER_ACTIONS=1

# Origin has to be bare: git refuses a push that updates a branch someone has checked out, and
# that refusal would otherwise read as a shim bug rather than a harness one.
mkdir -p "$WORK/seed/$PREFIX" "$WORK/seed/clusters/prod/applications/other"
"$REAL_GIT" -C "$WORK/seed" init -q -b master
"$REAL_GIT" -C "$WORK/seed" config user.email t@example.com
"$REAL_GIT" -C "$WORK/seed" config user.name test
echo one > "$WORK/seed/$PREFIX/configmap.yaml"
echo one > "$WORK/seed/clusters/prod/applications/other/x.yaml"
echo one > "$WORK/seed/README.md"
"$REAL_GIT" -C "$WORK/seed" add -A
"$REAL_GIT" -C "$WORK/seed" commit -qm seed
"$REAL_GIT" clone -q --bare "$WORK/seed" "$WORK/origin"

"$REAL_GIT" clone -q "$WORK/origin" "$WORK/repo"
"$REAL_GIT" -C "$WORK/repo" config user.email ed@example.com
"$REAL_GIT" -C "$WORK/repo" config user.name ed

check() { # name expect_code actual_code
  if [ "$2" -eq "$3" ]; then echo "ok   $1"
  else echo "FAIL $1: expected exit $2, got $3"; fail=1; fi
}

# 1. commit inside the prefix must be allowed
echo two > "$WORK/repo/$PREFIX/configmap.yaml"
(cd "$WORK/repo" && "$SHIM" add "$PREFIX/configmap.yaml") >/dev/null 2>&1
"$SHIM" -C "$WORK/repo" commit -qm "inside prefix" >/dev/null 2>&1
check "commit inside prefix allowed" 0 $?

# 2. commit outside the prefix must be refused
cd "$WORK/repo"
"$REAL_GIT" reset --hard -q HEAD~1
echo bad > clusters/prod/applications/other/x.yaml
"$REAL_GIT" add clusters/prod/applications/other/x.yaml >/dev/null 2>&1
out=$("$SHIM" commit -qm "outside prefix" 2>&1); rc=$?
check "commit outside prefix refused" 1 $rc
echo "$out" | grep -q "REFUSED" || { echo "FAIL: no REFUSED message: $out"; fail=1; }

# 3. mixed staging (one good, one bad) must be refused as a whole
echo two > "$PREFIX/configmap.yaml"
echo bad2 > clusters/prod/applications/other/x.yaml
"$REAL_GIT" add -A >/dev/null 2>&1
"$SHIM" commit -qm mixed >/dev/null 2>&1; rc=$?
check "mixed staging refused" 1 $rc

# 4. `git add` naming a path outside the prefix is refused before staging can matter
"$SHIM" add clusters/prod/applications/other/x.yaml >/dev/null 2>&1; rc=$?
check "add outside prefix refused" 1 $rc

# 5. an empty commit is refused
"$REAL_GIT" reset --hard -q HEAD; "$REAL_GIT" clean -qfd
"$SHIM" commit -qm nothing >/dev/null 2>&1; rc=$?
check "empty commit refused" 1 $rc

# 6. push with a clean, in-prefix history is allowed
echo three > "$PREFIX/configmap.yaml"
"$REAL_GIT" add "$PREFIX/configmap.yaml"
"$SHIM" commit -qm "in prefix again" >/dev/null 2>&1
"$SHIM" push origin master >/dev/null 2>&1; rc=$?
check "push in-prefix allowed" 0 $rc
"$REAL_GIT" -C "$WORK/origin" log -1 --pretty=%s | grep -q "in prefix again" \
  || { echo "FAIL: push did not land in origin"; fail=1; }

# 7. a force push is refused
"$SHIM" push --force origin master >/dev/null 2>&1; rc=$?
check "force push refused" 1 $rc

# 8. push from a different branch is refused
"$REAL_GIT" -C "$WORK/repo" branch -q side
"$REAL_GIT" -C "$WORK/repo" checkout -q side
echo four > "$PREFIX/configmap.yaml"
"$REAL_GIT" add "$PREFIX/configmap.yaml"
"$SHIM" commit -qm "on side" >/dev/null 2>&1
"$SHIM" push origin side >/dev/null 2>&1; rc=$?
check "push non-master refused" 1 $rc

# 9. push of a commit that reached outside the prefix *after* staging checks is refused:
#    smuggle it with `git commit-tree` via real git, then try the shim.
"$REAL_GIT" -C "$WORK/repo" checkout -q master
"$REAL_GIT" reset --hard -q HEAD
echo five > README.md
"$REAL_GIT" add README.md
"$REAL_GIT" commit -qm "smuggled via real git" >/dev/null 2>&1
"$SHIM" push origin master >/dev/null 2>&1; rc=$?
check "push containing out-of-prefix commit refused" 1 $rc

# 10. history-rewriting / out-of-tree subcommands are refused
for sub in filter-branch remote worktree submodule stash; do
  "$SHIM" $sub >/dev/null 2>&1; rc=$?
  check "git $sub refused" 1 $rc
done

# 11. read-only plumbing still passes through so the agent can inspect state
"$SHIM" status --porcelain >/dev/null 2>&1; rc=$?
check "status allowed" 0 $rc
"$SHIM" log -1 --oneline >/dev/null 2>&1; rc=$?
check "log allowed" 0 $rc
"$SHIM" diff --cached --name-only >/dev/null 2>&1; rc=$?
check "diff allowed" 0 $rc

# 12. failure modes must be messages, not tracebacks. Both of these came from a real CI break: the
# image build runs `git --version` before the repository has been cloned, and the wrapper's cwd was the
# repo unconditionally, so Docker build died on FileNotFoundError with a stack and the lane went red
# for a reason that looked like a sandbox bug.
missing_repo="$WORK/definitely-not-a-repo"
out=$(MCBOT_REPO="$missing_repo" "$SHIM" --version 2>&1); rc=$?
check "git --version with no repo exits 0" 0 $rc
echo "$out" | grep -q "git version" || { echo "FAIL: no version output when repo absent: $out"; fail=1; }
echo "$out" | grep -q "Traceback" && { echo "FAIL: traceback with repo absent"; fail=1; }
out=$(MCBOT_REPO="$missing_repo" "$SHIM" status --porcelain 2>&1); rc=$?
check "consequential git refused with no repo" 1 $rc
echo "$out" | grep -q "not mounted" || { echo "FAIL: no clear message for missing repo: $out"; fail=1; }
echo "$out" | grep -q "Traceback" && { echo "FAIL: traceback instead of git's own error"; fail=1; }
out=$(MCBOT_GIT_REAL="$WORK/no-such-git" "$SHIM" --version 2>&1); rc=$?
check "missing real git exits 1" 1 $rc
echo "$out" | grep -q "not at" || { echo "FAIL: missing real git gave no clear message: $out"; fail=1; }
echo "$out" | grep -q "Traceback" && { echo "FAIL: traceback instead of a message for missing real git"; fail=1; }

# 13. the wrapper must refuse to call itself. Inside the image /usr/local/bin/git IS the wrapper, so a
# MCBOT_GIT_REAL derived from PATH is this same file, and without the guard that is unbounded recursion.
out=$(MCBOT_GIT_REAL="$SHIM" "$SHIM" --version 2>&1); rc=$?
check "wrapper-as-real-git refused" 1 $rc
echo "$out" | grep -q "points back at this wrapper" || { echo "FAIL: no recursion message: $out"; fail=1; }

# 14. with MCBOT_REPO unset, the default has to be the directory entrypoint.sh actually clones into.
# entrypoint.sh exports MCBOT_REPO for the bot, so this default only governs a shell that did not inherit
# it - `kubectl exec`, a CronJob, a debug session - and while it pointed at a directory the image never
# in such a shell was refused as "the repository is not mounted", which reads exactly like the sandbox
# denying the bot.
grep -q 'REPO_DIR="${MCBOT_REPO:-\$DATA/repo}"' "$HERE/entrypoint.sh" \
  || { echo "FAIL: entrypoint.sh no longer clones into \$DATA/repo; the shim default has to move with it"; fail=1; }
grep -q 'MCBOT_REPO="\$REPO_DIR"' "$HERE/entrypoint.sh" \
  || { echo "FAIL: entrypoint.sh no longer exports MCBOT_REPO, so the shim default is the only thing holding it"; fail=1; }
mkdir -p "$WORK/data"
"$REAL_GIT" clone -q "$WORK/origin" "$WORK/data/repo"
"$REAL_GIT" -C "$WORK/data/repo" config user.email bot@example.com
"$REAL_GIT" -C "$WORK/data/repo" config user.name bot
# Section 6 pushes "three" into origin's master, so the fixture content here has to be something the
# clone does not already have - otherwise there is nothing to stage and the check fails for the wrong reason.
echo default-follows-mcbot-data > "$WORK/data/repo/$PREFIX/configmap.yaml"
rc=0
out=$(cd "$WORK/data/repo" && env -u MCBOT_REPO MCBOT_DATA="$WORK/data" "$SHIM" add "$PREFIX/configmap.yaml" \
  && env -u MCBOT_REPO MCBOT_DATA="$WORK/data" "$SHIM" commit -qm "default repo follows MCBOT_DATA" 2>&1) || rc=$?
check "default repo follows MCBOT_DATA when MCBOT_REPO is unset" 0 $rc
out=$(env -u MCBOT_REPO -u MCBOT_DATA "$SHIM" status --porcelain 2>&1); rc=$?
check "no MCBOT_REPO and no MCBOT_DATA still refuses rather than guessing" 1 $rc
echo "$out" | grep -q "not mounted at /data/repo" \
  || { echo "FAIL: the fallback default did not name the directory it looked in: $out"; fail=1; }

echo "--- publish permission: a commit in this prefix is a deploy ---"
# Committing under apps/minecraft/ is not bookkeeping: Argo CD watches that path and
# applies it. So a commit needs the same two things a `kubectl` write needs - the capability switched on, and
# a pressed confirmation button for this ask - and an in-game ask has neither.
publish_case() { # name approved readonly surface server_actions expect_code needle
  local name=$1 approved=$2 readonly=$3 surface=$4 sa=$5 want=$6 needle=$7 out rc=0
  "$REAL_GIT" -C "$WORK/repo" reset -q --hard >/dev/null 2>&1
  echo "change for $name" > "$WORK/repo/$PREFIX/publish.yaml"
  "$SHIM" -C "$WORK/repo" add "$PREFIX/publish.yaml" >/dev/null 2>&1
  turn "$approved" "$readonly" "$surface"
  out=$(env MCBOT_SERVER_ACTIONS="$sa" "$SHIM" -C "$WORK/repo" commit -qm "$name" 2>&1) || rc=$?
  check "$name" "$want" "$rc"
  [ -n "$needle" ] && { echo "$out" | grep -qi "$needle" \
    || { echo "FAIL $name: refusal did not say why ($out)"; fail=1; }; }
  [ "$rc" -eq 0 ] && "$REAL_GIT" -C "$WORK/repo" reset -q --hard HEAD~1 >/dev/null 2>&1
}
publish_case "commit refused when no button was pressed" false false discord 1 1 "pressed button"
publish_case "commit refused for an in-game ask" false true game 1 1 "inside the game"
publish_case "commit refused when out-of-game actions are switched off" true false discord 0 1 "switched off"
publish_case "commit allowed with a pressed button" true false discord 1 0 ""

# 16. an unset prefix is not "everything is allowed". `"any/path".startswith("")` is True for every path, so a
# wrapper that had its configuration dropped would have opened the whole repository. It refuses instead.
"$REAL_GIT" -C "$WORK/repo" reset -q --hard >/dev/null 2>&1
echo "change with no prefix configured" > "$WORK/repo/$PREFIX/publish.yaml"
"$SHIM" -C "$WORK/repo" add "$PREFIX/publish.yaml" >/dev/null 2>&1
turn true false discord
rc=0
out=$(env -u MCBOT_ALLOWED_PREFIX MCBOT_SERVER_ACTIONS=1 "$SHIM" -C "$WORK/repo" commit -qm no-prefix 2>&1) || rc=$?
check "no MCBOT_ALLOWED_PREFIX means nothing is committable" 1 $rc
echo "$out" | grep -qi "outside mcbot's area\|MCBOT_ALLOWED_PREFIX" \
  || { echo "FAIL: the unset prefix did not produce a refusal that names the setting: $out"; fail=1; }
"$REAL_GIT" -C "$WORK/repo" reset -q --hard >/dev/null 2>&1

jar_case() { # name allowlist expect_code needle
  local name=$1 allow=$2 want=$3 needle=$4 out rc=0
  "$REAL_GIT" -C "$WORK/repo" reset -q --hard >/dev/null 2>&1
  mkdir -p "$WORK/repo/${PREFIX}plugins"
  printf 'not a jar, a fixture' > "$WORK/repo/${PREFIX}plugins/TreeCapitator999.jar"
  "$SHIM" -C "$WORK/repo" add "${PREFIX}plugins/TreeCapitator999.jar" >/dev/null 2>&1
  turn true false discord
  out=$(env MCBOT_PLUGIN_ALLOWLIST="$allow" "$SHIM" -C "$WORK/repo" commit -qm "$name" 2>&1) || rc=$?
  check "$name" "$want" "$rc"
  echo "$out" | grep -qi "$needle" || { echo "FAIL $name: did not name the rule ($out)"; fail=1; }
  [ "$rc" -eq 0 ] && "$REAL_GIT" -C "$WORK/repo" reset -q --hard HEAD~1 >/dev/null 2>&1
  "$REAL_GIT" -C "$WORK/repo" reset -q --hard >/dev/null 2>&1
}
jar_case "a plugin jar is refused when the allowlist is empty" "" 1 "MCBOT_PLUGIN_ALLOWLIST is empty"
jar_case "a plugin jar named on the allowlist is allowed" "treecapitator999" 0 ""
jar_case "a plugin jar that is not named is refused" "luckperms" 1 "not on the plugin allowlist"

# The refusal has to be attributable afterwards, in the same log as every other refusal.
grep -q "nothing has been confirmed" "$WORK/audit.jsonl" \
  || { echo "FAIL: the publish refusals were not audited"; fail=1; }

echo "--- audit log ---"
[ -f "$WORK/audit.jsonl" ] && python3 -c "
import json,sys
for ln in open('$WORK/audit.jsonl'):
    r=json.loads(ln); print(' ', r['event'], r.get('reason') or r.get('files') or '')
"
[ "$fail" -eq 0 ] && echo "ALL PASS" || { echo "FAILURES"; exit 1; }
