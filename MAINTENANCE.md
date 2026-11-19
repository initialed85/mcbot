# mcbot maintenance notes

Operational notes for whoever runs this bot. Everything here is written for a stranger's cluster: replace your
own service names and addresses, and never copy a value out of somebody else's deployment.

## Answering in game: what has to be true

An in-game ask is two independent wires, and each one fails on its own.

1. **Capture** - `kubectl logs -f` on the server pod, filtered by `CHAT_LINE_RE`. Needs `pods/log` in the
   server's namespace, `MCBOT_INGAME_POD` naming a pod that exists, and `MCBOT_INGAME_CONTAINER` naming a
   **container id** - which is not inferable from the other names in a deployment. On the server this was written
   against, the app directory, ConfigMap, PVC and volume are all `minecraft` while the pod's single container is
   `server`, so `kubectl logs -c minecraft` is the natural wrong guess and is refused outright (`container
   minecraft is not valid for pod server-0 out of: server`). Ask the pod what containers it has.
   The failure is quiet - `ingame.tailing: false` in `/healthz`, every ask silently unanswered, and the Discord
   half still looking perfectly healthy. Check `tailing` before anything else.
2. **Answer** - RCON `say`. Needs `MCBOT_RCON_HOST` / `MCBOT_RCON_PORT` / `MCBOT_RCON_PASSWORD` to reach the
   server, and the password to equal the server's own RCON secret. Two namespaces cannot share one Secret, so
   on a split deployment the value is duplicated and the two copies have to be rotated together.

`/healthz` reports both halves: `tailing`, `rcon_configured`, `rcon_connected`, and `error`. Note
`rcon_connected` is lazy and honest: the RCON client connects when an answer is actually sent, so a bot that has
never been asked in game reports `false` while everything is configured correctly. Ask it something in game and
it becomes `true` (and stays true until the connection drops, at which point the client reconnects).

## Proving the in-game path without a player in the game

The obvious test - broadcast a line and see whether the bot answers it - does not work, and not for a flaky
reason. A console or RCON `say` is printed as `<[Server]> ...`, and the chat pattern refuses `<[Server]>`
on purpose, because accepting it would mean the tail answering its own broadcasts. There is no way to write a
line that looks like a player typed it without a player.

So prove it one level in. Run a throwaway pod in the bot's own namespace, with the bot's image, ConfigMap,
Secret and service account, that:

- calls `load_config()` (importing `bot.py` is not enough - the deployment's values are read inside that call);
- authenticates with the real `Rcon` client against the real RCON service;
- starts the real `_watch_game` task, in front of a `kubectl` stub that prints **one** Paper-shaped chat line
  (`[13:50:29 INFO]: <Probe0> !mcbot what is the ip?`) and then execs the image's real `kubectl` wrapper, so
  the tail continues as a real tail and every other kubectl call - including whatever Pi decides to run - still
  goes through the gate;
- records what the bot broadcasts, then exits.

Everything after the injected line is the real pipeline: the gate records the ask as read-only, Pi answers (in
the run that checked this, it looked the address up with `kubectl get service` and replied `Java: <lb-ip>:25565`),
and the answer lands in the server's console as `[Rcon] mcbot: <answer>` - which is visible in the server pod's
own log, so the proof does not depend on the probe's output.

Two things to keep the probe from contaminating the running bot: give it its own `MCBOT_DATA` (its SQLite
store, session directory, turn record and audit log must not be the ones on the live pod's volume), and delete
the pod afterwards. Do not run a second copy of `bot.py` itself: two Discord gateway clients on one token is not
a thing the gateway tolerates.

## Rotating the RCON password

1. Generate one password. `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
2. Update the server's Secret and the bot's Secret with the same value. Both are applied out of band (gitignored
   `___secret/` files in this deployment), because Argo prunes anything in git that carries a Secret value.
3. Restart the server pod, then the bot pod. Minecraft reads `RCON_PASSWORD` at start, and Paper logs the value
   in clear text on the way up - which is why the password is a Secret and why this file does not quote it.
4. Confirm from the bot's pod that the new password authenticates (`Rcon.list()`), and that the old one does not:
   a wrong password makes RCON close the socket without a message, so `EOFError` is the *expected* result of a
   wrong password, not a network fault.

## Keeping RCON off the public wire

The game port is published because players need it. RCON is not.

- The server's published Service carries the Java and Bedrock ports only.
- A second, ClusterIP Service carries RCON, so it has no external address and is reachable only inside the
  cluster (and only from pods whose service account can reach it).
- `RCON_PORT` on the server is set to the internal Service's port rather than the conventional 25575, so a stale
  firewall rule or an old LoadBalancer cannot leave the old port open by accident.
- Verify from outside your cluster, not from inside: `socket.connect_ex` against your public addresses on both
  the old and new RCON ports. Both should refuse, while the game port still answers. That measurement is the
  only thing distinguishing "internal" from "in a Service I never look at".

## The CI lane, and what it needs from you

`.woodpecker/build.yaml` is the whole path from a push to a rollout: `test-policy` runs the stdlib suites on the
clone, `build-and-push` builds and publishes `:$CI_COMMIT_SHA` and `:latest`, and `deploy-manifests` runs the
`wp-kustomize-deploy` plugin, which stamps the commit sha into the manifests repository and pushes. That commit
*is* the deploy - Argo CD reconciles it, and `git revert` of the one `newTag` line is the rollback.

Two things about that file are easy to get wrong:

- **The registry credentials and the write key are not per-repository.** They are Secrets in the CI's own
  namespace (`dockerhub` with keys `username`/`token`, and `home-ops-deploy-key` with `id_ed25519`/`known_hosts`),
  which every lane in this deployment reaches through the workflow's `backend_options.kubernetes.secrets`. So
  moving a pipeline to a new repository needs no secret work; the deploy key being repository-scoped is the blast
  radius, not a per-pipeline setup step.
- **Activating a repository does not build the commit it was activated on.** The lane is gated to
  `event: push, branch: master`, so a pipeline that did not exist when the push happened will not retroactively
  run. The first build needs another commit.
- **A lane that publishes nothing is not necessarily a lane that never ran.** The first build of this repository
  looked exactly like an inactive repository: no tag, no bump, and a badge route that answers with the SPA. It had
  in fact run and failed in `test-policy` in 13 seconds. Without UI access the ground truth is one read-only query
  against the CI's own Postgres (`kubectl -n <ci namespace> exec deploy/<postgres> -- psql -U $POSTGRES_USER -d
  $POSTGRES_DB`): `pipelines` joined to `steps` says which step died, and `log_entries.data` (bytea, ordered by
  `line`) is the step's full output. The server's own log only carries `level=error` events, so a failed lane is
  not in it.

- **The gate step's image has almost nothing in it.** `test-policy` runs in `python:3.12-slim` on purpose - no
  dependencies, no secrets, no image - so a suite that shells out to `git` or `kubectl` fails there and passes on
  a workstation, which is an assertion about the machine rather than about the code. Any wrapper test must name
  its own real binary through `MCBOT_GIT_REAL` / `MCBOT_KUBECTL_REAL` and point it at a stub. `test-config.py` is
  in the image step for the same reason: it imports `bot.py`, which imports `discord.py`.

The one thing worth checking by hand after a build: the tag the pin lands on is `$CI_COMMIT_SHA`, so the pushed
image tag and the manifest `newTag` are the same 40 characters, and the pod's `.status.containerStatuses[].imageID`
digest should match what the build step logged. A `:latest` alone proves nothing about which commit is running.

## Where the security decisions are written

`docs/security.md` - what the wrappers, the RBAC, the turn record and the capability switches are for. The
in-game ask path is read-only by three independent facts: `_run` re-derives `readonly` from the channel being an
in-game one, the kubectl wrapper refuses the write verbs for that turn, and an in-game chat window proves only
that the speaker can join the server, which is not an approval, so `approved` stays false there whatever
`MCBOT_APPROVER_MODE` says.
