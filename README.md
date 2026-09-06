# docker-codex

Build a personal Docker image for the OpenAI Codex CLI and Codex Relay, then publish it to Docker Hub.

The image installs `@openai/codex`, `codex-relay`, Google Antigravity CLI (`agy`), GitHub CLI (`gh`), and common terminal tools. GitHub Actions checks the latest upstream versions every day and rebuilds the Docker image when Codex, Codex Relay, or Antigravity CLI changes.

It also includes common terminal tools and `bubblewrap` for sandbox support. The compose file grants the container the extra sandbox permissions bubblewrap needs inside Docker.

## Files

```text
.
|-- Dockerfile
|-- docker-compose.yml
|-- docker/
|   |-- start-codex-container.sh
|   |-- codex-supervisor.toml
|   |-- codex-supervisor-new-thread
|   |-- codex-supervisor-run-thread
|   `-- codex-supervisor-turn-monitor.mjs
`-- .github/
    `-- workflows/
        `-- update-codex-image.yml
```

## Docker Hub Settings

In the GitHub repository, open:

```text
Settings
  -> Secrets and variables
  -> Actions
```

Add these repository secrets:

```text
DOCKERHUB_USERNAME = your Docker Hub username
DOCKERHUB_TOKEN = your Docker Hub access token
```

Use a Docker Hub access token, not your Docker Hub password.

## Automatic Updates

The workflow in `.github/workflows/update-codex-image.yml` runs in three cases:

```text
1. Every day at 03:00 UTC
2. Manually from the GitHub Actions page, optionally with force rebuild enabled
3. When Dockerfile, files under `docker/`, or the workflow file change on main
```

Each run checks:

```text
npm view @openai/codex version
npm view codex-relay version
curl -fsSL https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json | jq -r '.version'
docker run your-dockerhub-username/codex-dev:latest codex --version
docker run your-dockerhub-username/codex-dev:latest node -p "require('/usr/local/lib/node_modules/codex-relay/package.json').version"
docker run your-dockerhub-username/codex-dev:latest agy --version
```

It builds and pushes only when one of these is true:

```text
1. No current Docker Hub image exists
2. The npm Codex version is newer than the image version
3. The npm Codex Relay version is newer than the image version
4. The Antigravity CLI version is newer than the image version
5. Dockerfile, a file under `docker/`, or the workflow file changed
6. A manual run enables force rebuild
```

## Image Tags

The workflow publishes two tags:

```text
your-dockerhub-username/codex-dev:latest
your-dockerhub-username/codex-dev:codex-0.142.3
your-dockerhub-username/codex-dev:codex-0.142.3-relay-1.2.4
```

Use `latest` on your NAS for normal updates. Use a version tag if you need to roll back.

## Exposed Port

The image declares port `8787`, and the compose file maps it to the host:

```yaml
ports:
  - "8787:8787"
```

This only opens the port mapping. A service still needs to listen on `8787` inside the container, such as a web terminal or code server.

## Bubblewrap

The image includes `bubblewrap`:

```bash
bwrap --version
```

Because bubblewrap creates Linux namespaces, Docker must grant extra permissions. The compose file includes:

```yaml
cap_add:
  - SYS_ADMIN
security_opt:
  - seccomp=unconfined
  - apparmor=unconfined
```

Without these options, `bwrap` may fail with:

```text
Creating new namespace failed: Operation not permitted
```

## Manual Build

```bash
CODEX_VERSION=$(npm view @openai/codex version)
CODEX_RELAY_VERSION=$(npm view codex-relay version)
AGY_VERSION=$(curl -fsSL https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json | jq -r .version)
docker build \
  --build-arg CODEX_VERSION="${CODEX_VERSION}" \
  --build-arg CODEX_RELAY_VERSION="${CODEX_RELAY_VERSION}" \
  --build-arg AGY_VERSION="${AGY_VERSION}" \
  -t codex-dev:local .
```

## NAS Deployment

Create these directories on the NAS:

```bash
mkdir -p /share/Docker/codex/home
mkdir -p /share/Docker/codex/workspace
```

Copy or deploy `docker-compose.yml`. The default Docker Hub namespace is `limairui`, but you can override it if needed:

```bash
export CODEX_HTTP_PROXY=http://192.168.50.161:7897
export CODEX_HTTPS_PROXY=http://192.168.50.161:7897
cd /share/Docker/codex
docker compose pull
docker compose up -d
```

Enter the container:

```bash
docker exec -it codex bash
codex --version
codex login
codex
```

The container starts the preinstalled Codex Relay in shared app-server mode:

```bash
codex-relay --bg --shared-app-server
```

The image wraps the Codex executable so `codex resume` automatically connects to
the shared Unix socket. Other Codex commands are passed through unchanged. The
explicit equivalent is:

```bash
codex resume --remote unix://
```

Then it starts a shell inside a detached `tmux` session named `codex`.

## Account Supervisor

The container can run the private `codex-account-supervisor` from the persistent
workspace. Keep the checkout outside the image, for example:

```bash
git clone https://github.com/hikki-fan/codex-account-supervisor.git \
  /share/Docker/codex/workspace/codex-account-supervisor
```

The startup script then automatically:

1. Stops `codex-switch`'s background daemon so it cannot race the supervisor.
2. Starts one supervisor instance with a 95% *used* quota threshold.
3. Uses the official Relay PID file and `codex-relay stop`/`--bg --shared-app-server`.
4. Keeps state, locks, handoff packets, and logs under
   `/home/codex/.codex-supervisor` (the persistent `/home/codex` mount).

The image now includes a read-only app-server turn monitor. It connects to the
shared Unix app-server, resumes/listens to known threads, and records
`turn/started` plus terminal events for both terminal Codex and Relay/mobile
traffic. If the socket or a thread resync fails, it records `UNKNOWN`; the
supervisor then refuses to switch until a fresh snapshot is available.

The supervisor still requires an explicit idle state before switching accounts,
but the monitor supplies that state automatically. A manual/client integration
can also record a turn boundary with:

```bash
PYTHONPATH=/workspace/codex-account-supervisor \
  python3 -m codex_account_supervisor turn-signal active \
  --turn-id TURN_ID --thread-id THREAD_ID

# after the turn is complete
PYTHONPATH=/workspace/codex-account-supervisor \
  python3 -m codex_account_supervisor turn-signal idle --turn-id TURN_ID
```

After a successful cutover and Relay health check, the configured hook creates a
detached `tmux` session (`codex-handoff-*`) and first attempts to resume the
original thread on the new account. This is enabled only in the container
config after validating the current Codex CLI/Relay behavior. If resume is
rejected immediately, the hook falls back to a new thread with the continuation
packet. The supervisor no longer marks the original thread dead in this mode.

The mobile client itself may briefly reconnect while Relay restarts. Its thread
history remains the same local app-server rollout; the monitor does not need
mobile pairing tokens and never submits a prompt through the mobile API.

The startup script also runs a small `codex-warmup-scheduler`. It executes
`codex-switch warmup --json` once during the 07:00 hour in `Asia/Shanghai` and
writes its result to `/home/codex/.codex-switch/logs/warmup.log`. It does not
start `codex-switch daemon` or switch profiles; the account supervisor remains
the only component that can switch accounts. Set `CODEX_WARMUP_TZ` or
`CODEX_WARMUP_HOUR` only when the deployment uses a different local schedule.

Inspect the live integration without exposing credentials:

```bash
docker exec codex sh -lc \
  'ps -ef | grep -E "[c]odex_account_supervisor|[c]odex-relay"; \
   tail -n 50 /home/codex/.codex-supervisor/supervisor.log'
```

If the private supervisor checkout is absent, the container continues to run
Relay and the normal terminal session, but logs that quota handoff is disabled.

Use one active turn per thread. Relay/mobile and the terminal can observe the
same live session through the shared app-server, but a second prompt submitted
while a turn is running must wait for the current turn to finish.

Attach from a phone SSH session:

```bash
ssh user@your-server
docker exec -it codex bash
tmux attach -t codex
```

If the session is gone, start it again:

```bash
tmux new -s codex
codex
```

## Update Flow

```text
GitHub Actions schedule
  -> npm view @openai/codex version
  -> npm view codex-relay version
  -> Antigravity release manifest version
  -> docker pull Docker Hub latest image
  -> docker run latest image and read codex / codex-relay versions
  -> build and push only when an update is needed
```

This keeps the Docker Hub image updated without rebuilding every day when Codex has not changed.
