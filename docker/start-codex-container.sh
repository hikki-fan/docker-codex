#!/usr/bin/env bash
set -e

export PATH="/usr/local/bin:/home/codex/.local/bin:${PATH}"

# Keep Relay and terminal TUIs on the same app-server. Terminal sessions must
# attach with `codex resume --remote unix:// [SESSION_ID]` instead of starting
# a second standalone app-server for the same thread.
codex-relay --bg --shared-app-server

# The supervisor is the only component allowed to switch codex-switch
# profiles.  A pre-existing codex-switch daemon could race it during a quota
# handoff, so stop it before starting the supervisor.
if [ -x /home/codex/.local/bin/codex-switch ]; then
  /home/codex/.local/bin/codex-switch daemon stop >/dev/null 2>&1 || true
fi

# Restore the ACP CLI after a container rebuild. Prefer the canonical
# persistent install, and fall back to the checked-out bridge repository.
if [ -x /workspace/scripts/acp-cli ]; then
  ln -sf /workspace/scripts/acp-cli /usr/local/bin/acp-cli
elif [ -x /workspace/antigravity-rest-bridge/acp-cli ]; then
  ln -sf /workspace/antigravity-rest-bridge/acp-cli /usr/local/bin/acp-cli
fi

# Start the health watchdog when the bridge has been installed in the
# persistent workspace. Its singleton lock makes repeated starts harmless.
if [ -f /workspace/scripts/acp_watchdog.sh ]; then
  setsid bash /workspace/scripts/acp_watchdog.sh </dev/null >/dev/null 2>&1 &
fi

# The supervisor source is intentionally kept on the persistent workspace (or
# the NAS bind mount) because the repository is private and must not be baked
# into a public Docker image.  The image only ships its safe default config and
# handoff launchers.
SUPERVISOR_SOURCE="${CODEX_SUPERVISOR_SOURCE:-/workspace/codex-account-supervisor}"
if [ ! -f "${SUPERVISOR_SOURCE}/pyproject.toml" ] && \
   [ -f /nas-docker/codex-account-supervisor/pyproject.toml ]; then
  SUPERVISOR_SOURCE=/nas-docker/codex-account-supervisor
fi

SUPERVISOR_DIR="${CODEX_SUPERVISOR_DIR:-/home/codex/.codex-supervisor}"
SUPERVISOR_CONFIG="${CODEX_SUPERVISOR_CONFIG:-${SUPERVISOR_DIR}/config.toml}"
SUPERVISOR_LOG="${SUPERVISOR_DIR}/supervisor.log"
SUPERVISOR_PID="${SUPERVISOR_DIR}/supervisor.pid"
mkdir -p "${SUPERVISOR_DIR}"
chmod 700 "${SUPERVISOR_DIR}"

# Never overwrite an operator's persistent config.  The shipped file is a
# conservative default (95% threshold and explicit idle signal required).
if [ ! -f "${SUPERVISOR_CONFIG}" ] && [ -f /usr/local/share/codex-supervisor/config.toml ]; then
  install -m 600 /usr/local/share/codex-supervisor/config.toml "${SUPERVISOR_CONFIG}"
fi

if [ -f "${SUPERVISOR_SOURCE}/pyproject.toml" ]; then
  if [ -f "${SUPERVISOR_PID}" ] && kill -0 "$(cat "${SUPERVISOR_PID}")" 2>/dev/null; then
    : # An already-running instance owns the lock; do not create a duplicate.
    true
  else
    rm -f "${SUPERVISOR_PID}"
    PYTHONPATH="${SUPERVISOR_SOURCE}" \
      setsid python3 -m codex_account_supervisor run --config "${SUPERVISOR_CONFIG}" \
      </dev/null >>"${SUPERVISOR_LOG}" 2>&1 &
    echo $! >"${SUPERVISOR_PID}"
    chmod 600 "${SUPERVISOR_PID}" "${SUPERVISOR_LOG}" 2>/dev/null || true
  fi
else
  echo "codex-account-supervisor source not found; quota handoff supervisor is disabled" >&2
fi

tmux has-session -t codex 2>/dev/null ||
  tmux new-session -d -s codex 'cd /workspace && exec bash'

exec tail -f /dev/null
