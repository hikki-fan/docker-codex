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

The image puts the Codex wrapper at `/opt/codex/bin/codex`, ahead of npm's
`/usr/local/bin` entry point on PATH. Thus `codex resume` automatically connects
to the shared Unix socket even after an in-container npm upgrade. Login shells
also restore this path through `/etc/profile.d/codex-path.sh`. Other Codex
commands and explicitly supplied remote endpoints are passed through unchanged.
The explicit equivalent is:

```bash
codex resume --remote unix://
```

Then it starts a shell inside a detached `tmux` session named `codex`.

The startup script also restores the unified Agent Executor Gateway client and
starts its singleton watchdog from `/workspace/agent-executor-gateway`. The
retired legacy ACP bridge is intentionally not started, so AGY and Grok remain
available through the generic `/v1/executors/*` production API after rebuilds.

## Account Supervisor

The container can run the private `codex-account-supervisor` from the persistent
workspace. Keep the checkout outside the image, for example:

```bash
git clone https://github.com/hikki-fan/codex-account-supervisor.git \
  /share/Docker/codex/workspace/codex-account-supervisor
```

The startup script then automatically:

1. Stops `codex-switch`'s background daemon so it cannot race the supervisor.
2. Starts one supervisor instance with a 98% *used* quota threshold. A target
   is eligible only while its own 5-hour usage is strictly below 98%. Hitting
   the threshold hard-stops the shared app-server and switches immediately,
   even if a turn is still running or the current account still has credits.
   If every account is at or above 98%, the supervisor pauses and stops Relay
   so credits are not burned as 5h overflow.
3. Uses the official Relay PID file and `codex-relay stop`/`--bg --shared-app-server`.
4. Polls forced account-global usage after each completed five-second wait.
   Any TUI, Relay/mobile, background, or concurrent session that contributes
   to the current account reaching 98% therefore triggers the same cutover.
5. Keeps state, locks, handoff packets, and logs under
   `/home/codex/.codex-supervisor` (the persistent `/home/codex` mount).

If three consecutive forced usage refreshes fail, the supervisor stops Relay
instead of continuing to consume quota while blind. It keeps probing without
parallel refreshes; after visibility returns it evaluates 98% before either
switching profiles or restoring Relay.

The legacy app-server turn monitor remains in the image but is disabled by
default. Hard cutover uses account-global usage and does not need to resume or
inspect threads. This avoids creating another writer while an operator is
using `codex resume`. It can be re-enabled only for the optional drain mode by
setting `CODEX_SUPERVISOR_TURN_MONITOR_ENABLED=true`.

The supervisor hard-preempts at the 5h threshold: it does not wait for the
current turn to become idle and does not need to identify which thread caused
the aggregate usage. A manual/client integration can optionally record a turn
boundary for the legacy drain mode with:

```bash
PYTHONPATH=/workspace/codex-account-supervisor \
  python3 -m codex_account_supervisor turn-signal active \
  --turn-id TURN_ID --thread-id THREAD_ID

# after the turn is complete
PYTHONPATH=/workspace/codex-account-supervisor \
  python3 -m codex_account_supervisor turn-signal idle --turn-id TURN_ID
```

After a successful cutover and Relay health check, the default configuration
does not create a detached TUI, does not guess a continuation target, and does
not mark any saved thread dead. The operator reconnects and manually chooses a
session. `enable_handoff_hook=true` is a legacy opt-in and is not recommended
for this mode.

### What the operator sees during a switch

In a foreground `codex` CLI attached to the shared Relay, the current turn is
aborted when the 5h threshold is reached. During the Relay stop/start window
the CLI may report a closed connection, reconnect message, or return to its
prompt. Wait until the supervisor is back in `WATCH` and Relay is healthy:

```bash
docker exec codex python3 -m codex_account_supervisor status \
  --config /home/codex/.codex-supervisor/config.toml
```

Then resume manually and select the desired saved session (or supply its ID):

```bash
docker exec -it codex codex resume --remote unix:// <THREAD_ID>
```

Background turns are also interrupted by the hard switch. They are not
automatically resumed; choose the relevant saved session manually after Relay
is healthy.

The mobile client itself may briefly reconnect while Relay restarts. Its usage
is included in the same account-global quota sample.

The startup script also runs `codex-warmup-scheduler`. It executes one targeted
`codex-switch warmup --json <alias>` attempt for each account every five hours.
The schedule is a deterministic five-day cycle: account A starts at day 1
00:00, then repeats every five hours; account B uses the same cadence starting
150 minutes later. Therefore day 5 ends at A 19:00 / B 21:30 and day 6 returns
to day 1. The scheduler persists state under
`/home/codex/.codex-supervisor/warmup-scheduler.json` and writes only redacted
status lines to `/home/codex/.codex-switch/logs/warmup.log`.

When a warmup attempt returns `{ok: true, skipped: true}` because a quota window
is still active (for instance when a 5h slot arrives slightly earlier than the
previous window's completion time), the slot is not marked consumed. Instead, the
scheduler tracks a persistent, bounded retry in its state file:
- Retries are throttled with a configurable delay (`CODEX_WARMUP_RETRY_DELAY_SECONDS`,
  default 60s) to prevent polling storms every 20s.
- Failures and active skips are bounded by `CODEX_WARMUP_MAX_RETRIES` (default 5
  attempts); once reached, the slot is consumed to prevent infinite retry loops.
- Upon successful warmup (`warmup completed`), the scheduler automatically executes
  a forced usage refresh (`codex-switch --json list --force`) to update the local
  cache, parses the primary 5-hour quota metrics for the warmed profile, and logs
  only sanitized `used` and `reset` information without exposing credentials or tokens.
- State files are upgraded seamlessly with backward compatibility for legacy v1 states.

By default it discovers the two profiles and assigns the first two aliases in
sorted order to A/B. To pin the mapping and cycle anchor, set
`CODEX_WARMUP_A_ALIAS`, `CODEX_WARMUP_B_ALIAS`, and
`CODEX_WARMUP_EPOCH=YYYY-MM-DDTHH:MM:SS` (local `CODEX_WARMUP_TZ`, default
`Asia/Shanghai`). If no epoch is supplied, the first local calendar day after
startup is persisted as day 1. The `codex-switch` daemon remains disabled; the account
supervisor is still the only component allowed to switch accounts.

Startup PID checks ignore zombie processes. After an unclean container shutdown,
a reparented zombie monitor or supervisor no longer blocks startup from
removing its stale PID file and launching exactly one live process.

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
