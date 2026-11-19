# Working in this repository (development)

Instructions for whoever is changing mcbot's code. Everything here is about the *program* and its tests.
Operational facts - how to run a deployment, how to prove the in-game path, how to rotate the RCON password -
are in `MAINTENANCE.md`. The behavioural rules the bot itself follows are in `workspace/AGENTS.md`. The
argument for why the sandbox is shaped the way it is, and what it does not cover, is in `docs/security.md`.
If you learn something, put it in the one file it belongs in rather than in all four.

## What this repository is, and what is deliberately not in it

The shipped program reads its infrastructure entirely from environment variables. No hostname, namespace,
cluster name, registry path, Discord id or node address appears in `bot.py`, `config_table.py`, `rcon.py`,
`pi_rpc.py`, the wrappers or any test fixture - and `test-config.py` asserts that a missing required variable
refuses startup rather than falling back to somebody's default.

The owner's infrastructure lives in exactly one place: `.woodpecker/build.yaml`, which names the docker daemon
to build against, the image to publish, and the manifests repository to deploy into. A fork replaces that file
and nothing else. When you are tempted to add a `or "the-usual-host"` fallback because it makes a local run
convenient, that convenience is the bug: a deployment that silently stops being able to do the thing it was
deployed for is indistinguishable from a working one.

`deploy/example/` is the starting point for a stranger's cluster. It is a ConfigMap, Secret, RBAC, PVC and
Deployment with placeholder values, and `hack/check-mcbot-config.py` is the tool that checks a *real*
deployment against the variables this build requires (missing required, armed-but-unsupplied, set-but-never-read,
and what the capability switches actually arm).

## Layout

    bot.py                 the Discord client, the in-game log tail, the confirmation view, /healthz
    config_table.py        every variable this build reads: required/optional, default, and why
    rcon.py                the RCON client (framing, multi-packet reassembly, reconnect)
    pi_rpc.py              the Pi bridge: one long-lived Pi process per conversation
    render_pi_models.py    generates Pi's provider file from MCBOT_PI_* - we do not ship somebody's models
    entrypoint.sh          clones MCBOT_REPO_URL, generates config, execs bot.py
    bin/gate.py            the decision: read vs write vs action vs operation, and what a plugin jar needs
    bin/git-shim           the `git` on PATH: writes allowed only under MCBOT_ALLOWED_PREFIX, only in an approved turn
    bin/kubectl-shim       the `kubectl` on PATH: reads always, write verbs only in an approved turn
    bin/mc-rcon            the only route to the server console; classifies before it sends anything
    workspace/AGENTS.md    the persona the in-pod agent reads (three tiers, copy-first, the [[CONFIRM]] marker)
    test-*.py, test-*.sh   the suites, described below
    hack/check-mcbot-config.py   deployment completeness checker (manifests and .env, no cluster access)

## The suites are the design

`bin/gate.py` and the wrappers *are* the security behaviour. The model's good intentions are not, so the
boundary is asserted by running it. Each suite exists because something was once believed rather than measured.

    python3 test-bot-policy.py      who may make mcbot write, what counts as "just asking", tier classification
    python3 test-rcon.py            RCON framing against a server modelled on measured behaviour
    python3 test-gate.py            gate.py decisions, and the wrappers run as real processes
    test-git-shim.sh                the git wrapper against a scratch repo: what it commits and what it refuses
    python3 test-config.py          startup contract: refuses on a missing variable, clones the URL it was given,
                                    generates Pi's provider file. Imports bot.py, so it needs discord.py
    python3 test-image-smoke.py     builds the real client against the real discord.py
    bash test-sandbox-adversarial.sh  the phrasings that try to talk past the gate
    hack/check-mcbot-config.py <configmap> <secret>   a deployment against this build's requirements

Run them before you push; the CI lane runs them and a failing lane publishes nothing, which is the point.

Three rules these suites teach the hard way:

- **A stub cannot report a missing attribute.** `discord.CommandTree` passed a stubbed import, passed a plain
  `import bot`, and then killed the pod at startup. Construction against the real dependency is the assertion -
  that is what `test-image-smoke.py` is for, and why it runs twice (Dockerfile build and CI).
- **Do not let a suite assert about its runner.** `test-policy` runs in `python:3.12-slim` with no dependencies
  and no secrets so it can refuse a publish before anything is built. That means the image has no `git` and no
  `kubectl` in it: a wrapper test that shells out must name its own real binary (`MCBOT_GIT_REAL`,
  `MCBOT_KUBECTL_REAL`) and point it at a stub. `git-shim --help` passing through to a git that does not exist
  ate two pipelines. Same reason `test-config.py` belongs in the image step and not the gate step.
- **A refusal that is not tested is a rumour.** Every "this must be refused" case is a case in a suite. Sections
  written against allowlist semantics have to actually be in allowlist mode: pinned to the deployed
  `anyone` mode, `can_approve()` said yes to everyone and every refusal check passed for the wrong reason.

## Traps already paid for

Each of these is a bug that was paid for once. The comment in the code carries the full story; this is the index.

- `MCBOT_INGAME_CONTAINER` is a **container id**, and that id is not inferable from the other names in a
  deployment. On the server this was written against, the app directory, ConfigMap, PVC and volume are all
  `minecraft` while the pod's one container is `server`, so `kubectl logs -c minecraft` is the natural wrong
  guess and is refused outright. The tail never starts, `ingame.tailing` reports false, and every in-game ask goes
  unanswered while Discord looks healthy. Ask the pod which containers it has.
- Paper prefixes an unsigned client's chat with `[Not Secure] `. It marks the transport, not the line, and the
  pattern treats it as optional - without that, a whole class of player asks was silently dropped. The refusals
  that matter stay refused: `<[Server]>` and `[Rcon]` do not parse, so the tail cannot answer its own broadcasts.
- This server closes the RCON connection the moment it receives an empty payload. The textbook "send an empty
  flush packet and read until its id returns" trick hangs up the socket. `rcon-cli` never sends one, and neither
  do we. A fake server that answers the flush packet politely is how that passed a suite and failed a server.
- discord.py 2.7 has no `View.message`. Reaching for it after popping the pending and closing the confirmation
  row produced a failed interaction, no change, and a second press that said "that offer expired" - i.e. the bot
  refusing to do something, which is not how it reads when it is a bug in the press path. Answer the interaction
  first; housekeeping after.
- The wrappers are installed as **symlinks** in `/usr/local/bin` and must find their own modules from the
  checkout path, not from `dirname(abspath(__file__))`. They also have to win `command -v`, and the CI lane
  asserts exactly that (asserting the *real* path instead would be the bug in the workflow file).
- `MCBOT_GIT_REAL` derived from `command -v git` is the wrapper itself. A wrapper that calls itself is a fork
  bomb with extra steps, so it compares resolved paths and refuses.
- With no repository mounted, `git --version` still has to answer (the image is built before the clone) and
  everything with consequences must refuse. Falling back to the caller's cwd would run real git against some other
  repository while looking sandboxed.

## The cross-repository contract

The image tag is the commit sha, and the manifest `newTag` in the deployment repository is bumped by this
repository's own `deploy-manifests` step after a successful build and push. That commit *is* the deploy: the
GitOps controller reconciles it and `git revert` of the one `newTag` line is the rollback.

So: never hand-edit a pin to a tag that does not exist on the registry. A build that fails the gate publishes
nothing, and a manifest pointing at an unpublished tag is a rollout that fails at pull time. Check the registry
tag matches the commit before believing a lane deployed anything, and check the pod's
`.status.containerStatuses[].imageID` digest against what the build step logged. `:latest` proves nothing about
which commit is running.

A lane that publishes nothing is not necessarily a lane that never ran: a gate failure in 13 seconds looks
identical to an unactivated repository from outside. `MAINTENANCE.md` has the read-only queries that tell them
apart.

## Commit as you go

This repository has lost uncommitted work to a host reboot: the working tree came back at HEAD, with no reflog
entry and no stash, and the only surviving copies of some edits were in a conversation. Write a file, commit it.
That is also the difference between a change being reviewable and a change being folklore.

## Hygiene for a public repository

Nothing in this repository may carry a live hostname, Discord id, personal path, credential or cluster name -
including in tests and fixtures, which is where this rule gets broken by accident. Fixture values are
`example.test` and synthetic ids. `deploy/example/` holds placeholders. `workspace/AGENTS.md` describes the
persona in terms of `{{REPO_NAME}}` and `{{ALLOWED_PREFIX}}` because the entrypoint substitutes them at startup
rather than baking in an owner's paths.
