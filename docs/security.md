# How mcbot is contained, and what that does not cover

Written for the person who has to decide whether to point this at a server they care about. It describes the
layers in order of how absolute they are, then the parts that are honestly not solved.

## The thing being defended against

The interesting adversary is not a network attacker. It is this: an LLM with a shell, a git checkout and
`kubectl`, sitting in front of a Discord channel that anyone in the family can type into. Prompt injection is
not hypothetical here — the input is chat messages from children, screenshots uploaded by anybody in the
channel, and log lines from a game server where players write whatever they like.

The conclusion the design is built on: **a model cannot be trusted to enforce a boundary it can read.** So the
boundary is not in the prompt. It is in the image's `PATH`, in file permissions, and in the RBAC the cluster
grants. The persona says roughly the same thing the wrappers enforce, because a bot that explains itself well
is nicer to live with — not because it is the control.

## Layer 1 — the RBAC the cluster grants

The narrowest layer, because it is enforced by the API server and this repository cannot widen it. The bot gets
a ServiceAccount in the server's own namespace, and verbs that let it observe and restart but not redesign:

[`deploy/example/rbac.yaml`](../deploy/example/rbac.yaml) is the narrow version, and it is the one worth starting
from:

```yaml
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log", "services", "persistentvolumeclaims", "configmaps"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["delete"]                    # a restart, which a GitOps controller will reconcile anyway
  - apiGroups: ["apps"]
    resources: ["statefulsets"]
    verbs: ["get", "list"]
```

No `pods/exec`, no `patch` on a StatefulSet, no write verbs on anything. If you decide mcbot should apply
changes through `kubectl` instead of through a commit, the file explains which four lines to add and why adding
them is a change in the design rather than a convenience.

There is no kubeconfig in the image and no second context available, so "which cluster" is decided by what the
pod is mounted with, not by anything the agent chooses. Adapt the verbs to your cluster. The default shape is
read-only plus one restart, because in the intended design the write path is a git commit that a controller
reconciles, and a bot that cannot write to the API server at all is a smaller problem than one that can.

## Layer 2 — the wrappers on PATH

`bin/git-shim`, `bin/kubectl-shim` and `bin/mc-rcon` are installed where the real binaries would be, earlier on
`PATH`. The agent's `bash` tool, any script it writes, and any subprocess it spawns all resolve to the same
wrappers. Each one:

1. consults `bin/gate.py`, which classifies the command and answers `allowed / kind / why`;
2. reads the turn record, which says what kind of turn this is;
3. writes an audit line — refusals included — and exits `77` (or non-zero) **without executing anything**.

The point of putting this in the wrapper rather than in the agent's tool layer is that the tool layer is one
call deep. A script the model writes can call `subprocess.run(["git", ...])`, and a shell can call the binary
by name; both land in the same wrapper, which is the only place the decision is made.

The wrappers refuse to fall back. If `MCBOT_REPO` points at a directory that is not a git worktree, `git-shim`
names it and refuses rather than running real git against whatever directory the caller happened to be standing
in. If `MCBOT_GIT_REAL` resolves back to the wrapper itself, it refuses rather than recursing. If
`MCBOT_RCON_HOST` is unset, `mc-rcon` names the variable rather than connecting to a hostname remembered from
somebody else's cluster.

## Layer 3 — the turn record

`$MCBOT_DATA/turn.json` is written before every ask:

```json
{"ts": "...", "approved": false, "readonly": true, "surface": "game", "ask": "..."}
```

- `surface: game` forces `readonly: true` and `approved: false`. An in-game ask can never authorise anything.
- `approved: true` is written only when a person pressed the confirmation button on a `[[CONFIRM kind: detail]]`
  block that the model emitted for *this* ask. It authorises one turn.
- `entrypoint.sh` resets the file at pod start, so an approval cannot survive a rollout into a deployment that
  never asked for it.

It is a file rather than an environment variable because the pi child is one long-lived process: the wrapper
reads it per call, and the gateway writes it per ask.

## Layer 4 — what the capability switches arm

`MCBOT_SERVER_ACTIONS` and `MCBOT_INGAME_ACTIONS` decide what the bot is *capable of at all*, independent of
who is asking. Both default off in code and both default off in `deploy/example/`. A deployment that wants a
capability has to say so in a manifest, which means the capability is reviewable in a diff.

`MCBOT_PLUGIN_ALLOWLIST` defaults empty. A plugin jar is an unreadable binary that joins the server with op
permissions; the position in this repository is that it should arrive in a commit a person wrote, and the
wrapper refuses any jar not named in the allowlist even in an approved turn.

## Layer 5 — the filesystem

The pod runs `readOnlyRootFilesystem: true` with one writable volume, `MCBOT_DATA`: the queue, the audit log,
the turn record, the cloned repository, pi's generated provider file. The image, the wrappers, the persona and
`docs/` are read-only. The agent authors changes; it cannot edit the code that decides what it is allowed to
change, the Dockerfile, the workflow file, or the switch values it is subject to. `gate.py` reports this in
`/healthz` as `writable: false` for those paths.

This is also why `MCBOT_DATA` exists as one variable rather than six paths: a footprint that can be moved in
one line is a footprint you can verify in one line.

## What the audit log is for

`$MCBOT_DATA/audit/gate.jsonl` records every wrapper decision, allowed and refused, with the argv, the kind,
the reason and the turn it was decided in. It exists because the failure mode of a correct refusal is
indistinguishable from the failure mode of a bug: a bot that says "no" for a good reason and a bot that is
broken both look like a bot that does not do what you asked. Without a log you find out which one you have by
reading code at midnight.

## What this does not cover

- **Reading.** The agent can read the manifests it is allowed to write, the pod's logs, and the server console
  output. Secrets it can *see* include anything in its own environment. If a value must not be known to the
  agent, it must not be in the pod. Discord ids, hostnames and paths are not secrets in this design; tokens and
  passwords are, and they arrive as environment variables from a Secret rather than from a ConfigMap.
- **The log tail.** The in-game ask path reads the server pod's log through `kubectl logs -f`, which is a read
  and needs only `pods/log`. What it does *not* need is `pods/exec`, and the example RBAC leaves it out. If you
  add it for something else, remember that it is the one verb that lets a command run inside another pod.
- **Anything outside the two edges.** A bot with a shell can reach the network the pod can reach. NetworkPolicy
  is the layer for that, and this repository does not ship one — it does not know your cluster's addresses.
  Restrict egress from the namespace if that matters to you.
- **A human who is socially engineered.** The button says what will change, in the model's own words, which is
  useful and also the model's words. The control is that the person pressing it is on `MCBOT_APPROVER_IDS` and
  can read the diff before pressing. Do not run `MCBOT_APPROVER_MODE=anyone` on a server whose changes matter
  to anybody but the family in the channel; it is a family setting, not a safe one.
- **Prompt quality, model quality, and luck.** Every layer above is designed to survive the model deciding to
  ignore the persona. None of them survive a mistake in *this* repository — which is why the tests run in CI
  before an image is published, and why the image is checked after it is built: `git` must resolve to the
  wrapper, the wrapper must actually refuse, and the boundary must hold in the artifact that gets deployed
  rather than in the source that got reviewed.

## Changing this design

If you extend it, the asymmetry worth preserving is this: the agent may author changes to its persona, its
notes and the code it runs, but the *criteria* — the gate, the wrappers, the switches, the workflow file, the
tests that decide what passes — sit outside its writable surface. A system that can rewrite its own rubric has
stopped being a system you can audit; it is a system that grades its own homework.
