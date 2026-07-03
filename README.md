# docker-codex

Build a personal Docker image for the OpenAI Codex CLI and Codex Relay, then publish it to Docker Hub.

The image installs pinned `@openai/codex` and `codex-relay` versions from npm. GitHub Actions checks the latest npm versions every day and only rebuilds the Docker image when Codex or Codex Relay has changed.

It also includes common terminal tools and `bubblewrap` for sandbox support. The compose file grants the container the extra sandbox permissions bubblewrap needs inside Docker.

## Files

```text
.
|-- Dockerfile
|-- docker-compose.yml
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
3. When Dockerfile or the workflow file changes on main
```

Each run checks:

```text
npm view @openai/codex version
npm view codex-relay version
docker run your-dockerhub-username/codex-dev:latest codex --version
docker run your-dockerhub-username/codex-dev:latest node -p "require('/usr/local/lib/node_modules/codex-relay/package.json').version"
```

It builds and pushes only when one of these is true:

```text
1. No current Docker Hub image exists
2. The npm Codex version is newer than the image version
3. The npm Codex Relay version is newer than the image version
4. Dockerfile or the workflow file changed
5. A manual run enables force rebuild
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
docker build \
  --build-arg CODEX_VERSION="${CODEX_VERSION}" \
  --build-arg CODEX_RELAY_VERSION="${CODEX_RELAY_VERSION}" \
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

The container starts the preinstalled Codex Relay in the background first:

```bash
codex-relay --bg
```

Then it starts Codex automatically inside a detached `tmux` session named `codex`.

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
  -> docker pull Docker Hub latest image
  -> docker run latest image and read codex / codex-relay versions
  -> build and push only when an update is needed
```

This keeps the Docker Hub image updated without rebuilding every day when Codex has not changed.
