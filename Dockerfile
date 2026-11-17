# mcbot - a Discord bot that looks after a family Minecraft server.
#
# Three runtimes in one image, which is deliberate: the gateway (Python) supervises Pi (Node) as an RPC child,
# and both need `git` and `kubectl`. The scoping that makes this safe to point at a cluster lives in
# bin/git-shim (a PATH-level git wrapper) and in the ServiceAccount's RBAC that the deployment supplies - see
# docs/security.md - and not in prompt wording.
#
# Build context is this repository. Pinned versions throughout: this image is rebuilt on any change here, and
# a build that cannot pin is a build that changes underneath a running bot.

FROM docker.io/library/node:22-bookworm-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0 \
    PI_OFFLINE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssh-client procps python3 python3-venv tzdata \
    && rm -rf /var/lib/apt/lists/*

# kubectl matching the k3s server version this bot is deployed against. Pinned rather than fetched from
# `stable.txt`, because "latest" here would drift under a running bot with nobody watching. Change it with
# the cluster, not with the calendar.
ARG KUBECTL_VERSION=v1.36.2
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
      -o /usr/bin/kubectl && chmod 0755 /usr/bin/kubectl \
    && /usr/bin/kubectl version --client >/dev/null

# Pi itself. 1.5.2 pinned; `pi update` is not a thing in this container, upgrades are a rebuild.
ARG PI_VERSION=1.5.2
RUN npm install -g --omit=dev "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && pi --version >/dev/null

# Python side in a venv: bookworm's PEP 668 marks the system interpreter externally-managed.
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

WORKDIR /opt/mcbot
# Everything here is read-only to the running bot: the pod mounts only $MCBOT_DATA writable, so the program,
# its configuration table, its persona and the tests that decide what passes are all outside what the agent can
# edit. `pi-models.json` is deliberately NOT in this list - it is generated at startup by render_pi_models.py,
# from configuration, because a checked-in copy of it is one provider's deployment baked into everybody's image.
COPY bot.py pi_rpc.py rcon.py config_table.py render_pi_models.py /opt/mcbot/
COPY entrypoint.sh test-git-shim.sh test-sandbox-adversarial.sh /opt/mcbot/
COPY test-rcon.py test-image-smoke.py test-gate.py test-config.py test-bot-policy.py /opt/mcbot/
COPY hack/ /opt/mcbot/hack/
COPY docs/ /opt/mcbot/docs/
COPY workspace/ /opt/mcbot/workspace/
COPY bin/ /opt/mcbot/bin/
RUN chmod 0755 /opt/mcbot/entrypoint.sh /opt/mcbot/bin/git-shim /opt/mcbot/bin/kubectl-shim \
    /opt/mcbot/bin/mc-rcon /opt/mcbot/bin/gate.py /opt/mcbot/test-git-shim.sh \
    /opt/mcbot/test-sandbox-adversarial.sh /opt/mcbot/test-image-smoke.py /opt/mcbot/test-gate.py

# Build this object with the real discord.py, not a stub. `import bot` is not enough: the names inside a
# constructor are only evaluated when it runs, and `discord.CommandTree` (which bot.py used, and which
# discord.py 2.7 does not export) survived both the policy suite and an import smoke test before crashing
# the pod at startup. Needs no secrets and no network.
RUN python3 /opt/mcbot/test-image-smoke.py

# The whole sandbox hinges on this ordering: `git` must resolve to the wrapper, and the wrapper must
# be able to reach the real binary at the absolute path it was given.
RUN ln -sf /opt/mcbot/bin/git-shim /usr/local/bin/git && /usr/local/bin/git --version >/dev/null
ENV MCBOT_GIT_REAL=/usr/bin/git

# The same ordering for the two ways out of the game, because they are the two ways a chat message becomes a
# change to a cluster or to a running server. `kubectl` resolves to the wrapper, which refuses a write unless
# the turn record says a button was pressed; `mc-rcon` is the only sanctioned way to reach the server's RCON
# and it refuses on the same record. Both resolve through /usr/local/bin, which precedes /usr/bin in PATH,
# and both are checked *here* rather than at first use.
ENV MCBOT_KUBECTL_REAL=/usr/bin/kubectl
RUN ln -sf /opt/mcbot/bin/kubectl-shim /usr/local/bin/kubectl \
    && ln -sf /opt/mcbot/bin/mc-rcon /usr/local/bin/mc-rcon \
    && kubectl --help >/dev/null && mc-rcon --help >/dev/null

# The configuration contract, asserted in the artifact: every variable declared once, no default that belongs to
# somebody else's deployment, the rendered documentation agreeing with the table, and each knob demonstrably
# moving what leaves the process. In the image there is no .git, so this scans the files the image actually
# ships, which is the stricter version of the question.
RUN python3 /opt/mcbot/test-config.py

# The gate is asserted at build time, with the wrappers that will actually be on PATH in the running image.
# bin/gate.py is pure logic and needs no secrets and no network, so a broken permission rule fails the build
# instead of failing a turn in front of a nine-year-old.
RUN python3 /opt/mcbot/test-gate.py

# Second run of the smoke test, with the wiring half only, because it can only be asserted after the links
# above exist. A sandbox made of PATH ordering is worth asserting on the PATH the real process will see.
RUN python3 /opt/mcbot/test-image-smoke.py --path

# GitHub's host keys, so the first clone over SSH is verified rather than skipped.
RUN mkdir -p /root/.ssh && curl -fsSL https://api.github.com/meta \
    | python3 -c "import json,sys; m=json.load(sys.stdin); ks=m['ssh_keys']; print('\n'.join(('%s ' + k) % h for h in ('github.com','ssh.github.com') for k in ks))" \
    > /etc/ssh/ssh_known_hosts && chmod 0644 /etc/ssh/ssh_known_hosts \
    && sort -u /etc/ssh/ssh_known_hosts | wc -l

# Non-root. The pod also runs with a read-only root filesystem and only /data writable, so this is
# belt-and-braces rather than the boundary.
RUN useradd --create-home --uid 10001 --shell /bin/bash mcbot \
    && mkdir -p /data && chown -R mcbot:mcbot /data
USER mcbot

# `StrictHostKeyChecking=yes`, which is the only strict value OpenSSH understands - `reject_hosts`
# (which this used) is not a real option and ssh fails the connection with `command-line line 0:
# unsupported option "reject_hosts"` before it ever reaches GitHub. `yes` is what we want anyway
# because the keys above are baked in: an unknown host key is an error, not a prompt, and there is no
# `accept-new` TOFU path to fall into.
ENV MCBOT_DATA=/data \
    MCBOT_HEALTH_PORT=8080 \
    GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ssh/ssh_known_hosts"

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${MCBOT_HEALTH_PORT}/healthz" || exit 1
ENTRYPOINT ["/opt/mcbot/entrypoint.sh"]
