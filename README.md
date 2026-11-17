# mcbot

A Discord bot that looks after a family Minecraft server, and an agent that is only ever allowed to
answer questions.

An `@mcbot` mention in a permitted Discord channel, or `!mcbot what is the ip` typed in game, becomes a
prompt for [pi](https://github.com/badlogic/pi-mono), which runs in the same container with its normal
`read/bash/edit/write` tools. That is the interesting part of the project: the agent has a shell, a git
checkout and `kubectl`, and the project's job is to make those edges physically narrow rather than ask
nicely. Nothing in this repository depends on the model behaving well.

It runs Paper 1.21.11 on Kubernetes (k3s + Longhorn + Argo CD), but nothing here is Kubernetes-specific
except the deployment example: the bot talks to Minecraft over RCON and to your manifests over git.

## What it will not do

- **Answer privately.** Every answer is posted in the channel it was asked in. A direct message is turned
  away, publicly, with a pointer to a channel. On a shared family server a private answer is an edge nobody
  else can see, audit, or correct.
- **Change the game.** In-game asks are read-only, always. `give`, `tp`, `gamemode`, `kick`, block edits and
  the rest are refused with or without a confirmation button — the button never appears for "give me 64
  diamonds". Out-of-game operations (the whitelist family, restarts, manifest edits) require a button pressed
  by a person in Discord.
- **Answer in a DM, then act on it.** An in-game ask can never authorise a change, in any turn.
- **Do anything at all by default.** Both capability switches ship off. An installation that never mentions
  them gets a bot that answers questions and changes nothing.

The three questions are kept separate on purpose: *is this capability armed* (`MCBOT_SERVER_ACTIONS`,
`MCBOT_INGAME_ACTIONS`), *who may authorise it* (`MCBOT_APPROVER_*`), and *was it authorised for this
turn* (the confirmation button, written to a turn record). Collapsing them is how "anyone can press it"
quietly becomes "anyone can do anything".

## Where the rules actually live

Prompts are advisory. Enforcement is a `PATH` in front of the binaries and one file both sides read:

| the agent reaches for | what it actually gets | what is refused, and where |
| --- | --- | --- |
| `git` | [`bin/git-shim`](bin/git-shim) | any commit or push touching a path outside `MCBOT_ALLOWED_PREFIX`; any branch other than `MCBOT_ALLOWED_BRANCH`; any plugin jar outside `MCBOT_PLUGIN_ALLOWLIST`; any commit while `MCBOT_SERVER_ACTIONS=0` |
| `kubectl` | [`bin/kubectl-shim`](bin/kubectl-shim) | verbs with consequences in a read-only turn, and everything the ServiceAccount's RBAC does not allow |
| the Minecraft console | [`bin/mc-rcon`](bin/mc-rcon) | whispers (`msg`, `w`, `tell`) in every turn; in-game actions in every turn; out-of-game operations without a pressed confirmation button |
| the decision itself | [`bin/gate.py`](bin/gate.py) | one place that classifies a command and answers `allowed / kind / why`, consulted by all three wrappers |

The turn record (`$MCBOT_DATA/turn.json`) says what kind of turn this is — `readonly`, `approved`, `surface`
— and is reset at pod start, so an approval can never survive into the next deployment. Each wrapper writes
an audit line (allow *and* refusal) to `$MCBOT_DATA/audit/`, which is how "why did it say no" is answerable
without reading code.

`RBAC` is the other half, and it is *your* manifest, not this repository's default: a ServiceAccount scoped
to the one namespace, with `get/list` on pods and logs, `delete pod`, and nothing else that reaches a cluster
you care about. See [docs/security.md](docs/security.md).

## Configuration

[`config_table.py`](config_table.py) is the single declaration of every environment variable: its type, what
it is for, whether it is required, what it is required *when*, and its default.

- [`docs/configuration.md`](docs/configuration.md) is that table rendered, and a test fails if the file and the
  table disagree.
- `hack/check-mcbot-config.py` checks a ConfigMap or `.env` against the table before you apply it: a required
  variable that is absent, a variable your switches demand and you did not supply, a name no version of this
  code reads (a typo, or a rename that went one way only), and what the deployment actually arms.
- Variables that describe *your* deployment — Discord ids, the repository it commits to, the path it may write
  to, the server pod, the RCON host, the inference endpoint — have **no defaults**. A shipped default for one
  of those is somebody else's infrastructure published to strangers, and the failure it produces reads like a
  network fault rather than like a missing variable.

Run it with no configuration and it tells you exactly which variables it wanted, in one message, including the
ones demanded by the features you switched on.

## Deploying

```
deploy/example/namespace.yaml       one namespace per bot, which is the outer bound of `kubectl`
deploy/example/rbac.yaml            the ServiceAccount and the verbs, narrowed to reads plus a restart
deploy/example/configmap.yaml       every knob, with placeholders
deploy/example/deployment.yaml      read-only root, one writable volume, Recreate, health probes
deploy/example/pvc.yaml             optional: the durable version of that volume
deploy/example/secret.example.yaml  the secret half, which never belongs in git
deploy/example/kustomization.yaml   everything above except the Secret, which you apply out of band
```

`hack/check-mcbot-config.py deploy/example/configmap.yaml` passes against the example, which is the point: it is
a deployment you can render, diff against your own, and then watch the checker refuse until the placeholders are
your values.

`entrypoint.sh` is the startup contract: clone `MCBOT_REPO_URL` into `$MCBOT_DATA/repo`, render pi's
`models.json` from `MCBOT_PI_BASE_URL` / `PI_PROVIDER` / `PI_MODEL` (the key is referenced as `$VAR`, never
written down), substitute the real repository name and prefix into the persona, put the wrappers first on
`PATH`, reset the turn record, then start the gateway. If a required variable is missing it stops there,
before cloning anything.

The pod is expected to run with `readOnlyRootFilesystem: true` and one writable volume (`MCBOT_DATA`). The
wrappers, the image and the tests are built for that shape; a container that lets the agent write its own
`PATH` has no sandbox to speak of.

## Tests

```bash
uv venv && uv pip install -r requirements.txt

python3 test-config.py               # every knob is declared, no default is somebody's deployment, and each
                                     # knob demonstrably moves what goes out over the wire
python3 test-bot-policy.py           # who may ask, who may authorise, what counts as "just asking"
python3 test-gate.py                 # runs the wrappers: exit 77 with nothing executed, exec when a button was
                                     # pressed, plugin decisions, missing binaries named rather than guessed
python3 test-rcon.py                 # Source RCON framing, multi-packet answers, one-command enforcement
python3 test-image-smoke.py          # constructs the real bot against the real discord.py, and checks every
                                     # library call site against the installed signatures
bash test-git-shim.sh                # a scratch repository, and the prefix boundary holding in it
bash test-sandbox-adversarial.sh     # pi itself, told "the admin already approved this", against the wrappers
```

`test-config.py` is the one that keeps this repository public-safe: it scans every tracked file for a hostname,
a namespace, a repository layout, a real person's name or a real Discord id, and it distinguishes a fixture id
(`111111111111111111`) from a snowflake, which is a timestamp in disguise. Every knob assertion is proved at the
point something leaves the process — a socket opened, an argv handed to `kubectl`, a URL POSTed to, a repository
cloned, a refusal issued — because "the variable is read" is not the same claim as "the variable does something".

## CI

[`.woodpecker/build.yaml`](.woodpecker/build.yaml) runs the policy suites before anything is published, builds
and pushes `mcbot:<sha>` (plus `:latest`), then verifies the *built image*: that `git` resolves to the wrapper
rather than the binary, that the wrapper refuses when it should, that the bot constructs with the real
`discord.py` and no credentials, and that `pi` and `kubectl` are present. Then it stamps the commit sha into
the manifests repository, which is the deploy: Argo CD does the rollout, and `git revert` is the rollback.

The order matters. `test-policy` gates `build-and-push` so a regression in the allowlist cannot produce a
published `:latest`, and the image checks gate the deploy so a PATH-ordering change fails a pipeline instead of
quietly granting the bot a repository.

## Layout

```
bot.py                  gateway: Discord, the ask path in game, the confirmation views, the Pi bridge
pi_rpc.py                 supervises the pi child and reads its JSONL RPC
rcon.py                 a small Source RCON client (Paper 1.21.11 answers no empty flush packet)
config_table.py         every configuration variable, in one place
render_pi_models.py     generates pi's provider file from configuration
entrypoint.sh           the startup contract
bin/                    gate.py and the three wrappers that apply it
workspace/AGENTS.md     the persona, with placeholders the entrypoint substitutes
deploy/example/         a ConfigMap and a Secret to start from
docs/configuration.md   config_table.py, rendered
hack/check-mcbot-config.py  a deployment checked against the table
test-*.py|sh            the suites above
```

## Notes worth knowing before you point this at your server

- **RCON is the only path that touches the running server directly**, and it is deliberately small. Everything
  else goes through git and a GitOps controller, where a change is a commit somebody can read.
- **`MCBOT_ALLOWED_PREFIX` is the most consequential value in your deployment.** In a GitOps tree, a commit
  inside that prefix *is* a deploy. Do not point it at a directory that contains anything you did not intend
  the bot to be able to change.
- **A confirmation button authorises one turn.** It does not move a capability switch, and it does not make an
  in-game ask able to change anything.
- **Secrets do not go in git**, including for a private repository. `MCBOT_PI_API_KEY_ENV` exists so the
  *name* of the key variable is configurable and the key itself is never in a manifest, a generated file or a
  log line.
- **The model choice is a deployment choice.** `PI_MODEL` defaults to `auto`, meaning "whatever the configured
  provider calls default". A pin in the code would be a deployment baked into a library.

## Status

In daily use on a family server: Discord gateway, in-game asks, the confirmation gate, the audit log and the
capability switches are all live. The parts most likely to need your attention are the persona (it is written
for children and reads that way) and the deployment example, which is a starting point rather than a chart.
