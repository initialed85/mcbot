#!/usr/bin/env bash
# mcbot startup: prepare the workspace Pi will run in, then hand over to the gateway.
#
# Everything here is idempotent because the PVC outlives the pod: a restart must not re-clone, and a recreated
# namespace must not need anything more than applying the Secret and ConfigMap again.
#
# What this file does *not* do is supply a value that describes a deployment. There is no repository URL, no
# path prefix it may commit to, no inference endpoint and no API key variable name written into it. Those come
# from the environment; `docs/configuration.md` says what each is for, and bot.py refuses to start without them.
set -euo pipefail

: "${DISCORD_BOT_TOKEN:?DISCORD_BOT_TOKEN is required}"
: "${MCBOT_REPO_URL:?MCBOT_REPO_URL is required - the repository mcbot clones and may commit to}"
: "${MCBOT_ALLOWED_PREFIX:?MCBOT_ALLOWED_PREFIX is required - the only path prefix mcbot may commit inside that repository}"

# Where the installed program lives. /opt/mcbot in the image; overridable so the startup contract can be
# exercised on a workstation against a checkout, which is the only way to know it holds.
APP_ROOT="${MCBOT_APP_ROOT:-/opt/mcbot}"
DATA="${MCBOT_DATA:-/data}"
# Which variable carries the inference provider's key. The key itself never appears in this script, in a log, or
# in the generated models.json - pi is given the name and reads the value from its own environment.
PI_KEY_ENV="${MCBOT_PI_API_KEY_ENV:-MCBOT_PI_API_KEY}"
: "${!PI_KEY_ENV:?${PI_KEY_ENV} is required (name it with MCBOT_PI_API_KEY_ENV)}"

REPO_DIR="${MCBOT_REPO:-$DATA/repo}"
WORKSPACE="${MCBOT_WORKSPACE:-$DATA/workspace}"
# The workspace link is named after the repository rather than after one repository: AGENTS.md tells Pi the clone
# is at ./<name>, and the name it is told has to be the name this script actually makes. Computed here because
# AGENTS.md is staged further up.
REPO_NAME="$(basename "${MCBOT_REPO_URL%/}")"
REPO_NAME="${REPO_NAME%.git}"
PILHOME="$DATA/pi"
export PI_CODING_AGENT_DIR="$PILHOME"
export PI_CODING_AGENT_SESSION_DIR="$DATA/sessions"
export MCBOT_DATA="$DATA"
export MCBOT_REPO="$REPO_DIR"
export MCBOT_WORKSPACE="$WORKSPACE"
export MCBOT_GIT_REAL="${MCBOT_GIT_REAL:-/usr/bin/git}"
export MCBOT_GIT_LOG="${MCBOT_GIT_LOG:-$DATA/audit/git.jsonl}"
export MCBOT_AUDIT_DIR="${MCBOT_AUDIT_DIR:-$DATA/audit}"
# These two must agree with what the gate reads and with what bot.py writes. Derived from MCBOT_DATA so a
# deployment with a different writable volume has one variable to set rather than three.
export MCBOT_TURN_MODE="${MCBOT_TURN_MODE:-$DATA/turn.json}"
export MCBOT_AUDIT="${MCBOT_AUDIT:-$DATA/audit/gate.jsonl}"
export HOME="$DATA"

mkdir -p "$WORKSPACE" "$PILHOME" "$DATA/sessions" "$DATA/audit" "$PILHOME/npm"

# Pi's provider definition, generated from MCBOT_PI_BASE_URL / PI_PROVIDER / PI_MODEL rather than baked into the
# image as one cluster's service names. Written to the writable volume because pi wants to read it, and the
# image is read-only.
python3 "$APP_ROOT/render_pi_models.py" > "$PILHOME/models.json"
chmod 600 "$PILHOME/models.json"

# The persona. Kept in the image, staged into the workspace, so Pi's project context is mcbot's own and never
# whoever's infrastructure this repository happens to have been developed against. The two placeholders are the
# only facts in it that depend on the deployment: where the clone appears in the workspace, and which paths a
# commit may touch. Substituted here rather than guessed by the model at runtime.
cp_src="$APP_ROOT/workspace/AGENTS.md"
sed -e "s|{{REPO_NAME}}|${REPO_NAME}|g" -e "s|{{ALLOWED_PREFIX}}|${MCBOT_ALLOWED_PREFIX}|g" \
  "$cp_src" > "$WORKSPACE/AGENTS.md"

# --- the deploy key -------------------------------------------------------------------------
# A Secret is mounted root-owned no matter what defaultMode says, so the key needs one hop before ssh will
# accept it. At 0400 the pod's own uid (10001) is not the owner and not in the owning group, so the file is
# simply unreadable. At 0440 plus fsGroup the pod can read it, but OpenSSH then refuses to use a private key
# that is group-readable ("Bad owner or permissions"). Hence: read the mount once, write our own 0600 copy in
# the pod's tmpfs, and never point ssh at the mount itself. /tmp is an emptyDir, so the copy cannot reach the
# PVC and dies with the pod - which is the property the original design wanted.
SSH_DIR="${MCBOT_SSH_DIR:-/tmp/.ssh}"
KEY_MOUNT="${MCBOT_GIT_KEY_MOUNT:-/mnt/git-key/id_ed25519}"
mkdir -p "$SSH_DIR" && chmod 700 "$SSH_DIR"
if [ ! -r "$KEY_MOUNT" ]; then
  echo "mcbot: cannot read the deploy key at $KEY_MOUNT" >&2
  echo "mcbot: the key Secret must be mounted with defaultMode 0440 - at 0400 the file is root-owned and this" >&2
  echo "mcbot: pod runs as uid 10001, so nothing here can open it." >&2
  exit 1
fi
cp "$KEY_MOUNT" "$SSH_DIR/id_ed25519" && chmod 600 "$SSH_DIR/id_ed25519"
export GIT_SSH_COMMAND="ssh -i $SSH_DIR/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ssh/ssh_known_hosts"

# --- the checkout ---------------------------------------------------------------------------
BRANCH="${MCBOT_REPO_BRANCH:-master}"

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "mcbot: cloning $MCBOT_REPO_URL ($BRANCH)"
  # The real binary, not the wrapper: with no worktree yet the wrapper has nothing to police, and it refuses
  # consequential subcommands in that state on purpose (see bin/git-shim) rather than falling back to whatever
  # repository the caller happens to be standing in.
  "$MCBOT_GIT_REAL" clone --branch "$BRANCH" "$MCBOT_REPO_URL" "$REPO_DIR"
fi

# The identity is set per-repo so a commit is always attributable to the bot, and the wrapper's own setdefault
# cannot be outrun by a global config someone else wrote.
git -C "$REPO_DIR" config user.name "${MCBOT_GIT_NAME:-mcbot}"
git -C "$REPO_DIR" config user.email "${MCBOT_GIT_EMAIL:-mcbot@localhost}"
git -C "$REPO_DIR" config gc.auto 0

ln -sfn "$REPO_DIR" "$WORKSPACE/$REPO_NAME"

# Refuse to start on a dirty tree that is not ours: the wrapper blocks out-of-prefix commits, but a stray edit
# would make every subsequent `git add -A` a negotiation with the sandbox.
dirty=$(git -C "$REPO_DIR" status --porcelain | awk '{print $2}' | grep -v "^$MCBOT_ALLOWED_PREFIX" || true)
if [ -n "$dirty" ]; then
  echo "mcbot: worktree has changes outside $MCBOT_ALLOWED_PREFIX; resetting to HEAD"
  echo "$dirty" | head -5
  git -C "$REPO_DIR" checkout -- . 2>/dev/null || true
fi

echo "mcbot: repo at $(git -C "$REPO_DIR" rev-parse --short HEAD) on $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
echo "mcbot: may commit under $MCBOT_ALLOWED_PREFIX, may push $MCBOT_REPO_BRANCH"
echo "mcbot: git wrapper is $(command -v git) (real: $MCBOT_GIT_REAL)"
echo "mcbot: kubectl wrapper is $(command -v kubectl) (real: ${MCBOT_KUBECTL_REAL:-/usr/bin/kubectl})"

# A turn record lives on the persistent volume, so a pod that died mid-turn can leave behind a record saying a
# button was pressed. Nothing reads it until bot.py writes a new one for the next ask, but "nothing reads it
# until" is a property of the code rather than of the filesystem, and an exec into the pod would read it.
# Start every pod in the state that has no authority.
TURN_FILE="$MCBOT_TURN_MODE"
printf '{"ts":"startup","approved":false,"readonly":true,"surface":"startup"}' > "$TURN_FILE"
chmod 0644 "$TURN_FILE"
echo "mcbot: turn record reset to read-only/unapproved at $TURN_FILE"
echo "mcbot: kubectl $(kubectl version --client --output=json 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["clientVersion"]["gitVersion"])' 2>/dev/null || echo unknown)"

exec python3 -u "$APP_ROOT/bot.py"
