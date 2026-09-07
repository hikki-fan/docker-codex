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

# AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF
# Restore only the unified Agent Executor Gateway client and watchdog. The
# retired legacy bridge must not compete for port 8765 after a container
# rebuild, otherwise the AGY/Grok executor routes disappear behind its older
# health-only API.
GATEWAY_CLI="/workspace/agent-executor-gateway/acp-cli"
GATEWAY_WATCHDOG="/workspace/agent-executor-gateway/scripts/gateway_watchdog.sh"
if [ -x "${GATEWAY_CLI}" ]; then
  ln -sf "${GATEWAY_CLI}" /usr/local/bin/acp-cli
fi
if [ -x "${GATEWAY_WATCHDOG}" ]; then
  setsid bash "${GATEWAY_WATCHDOG}" </dev/null >/dev/null 2>&1 &
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
TURN_MONITOR_PID="${SUPERVISOR_DIR}/turn-monitor.pid"
WARMUP_SCHEDULER_PID="${SUPERVISOR_DIR}/warmup-scheduler.pid"
mkdir -p "${SUPERVISOR_DIR}"
chmod 700 "${SUPERVISOR_DIR}"

# Never overwrite an operator's persistent config.  The shipped file is a
# conservative default (98% used threshold and explicit idle signal required).
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

# Observe the shared app-server's turn lifecycle.  This covers terminal Codex
# and Relay/mobile turns without requiring mobile pairing credentials.  If the
# observer disconnects it writes UNKNOWN, so the supervisor refuses a cutover
# until a fresh snapshot is available.
if [ -f /usr/local/bin/codex-supervisor-turn-monitor.mjs ] && \
   [ -f "${SUPERVISOR_SOURCE}/pyproject.toml" ]; then
  if [ -f "${TURN_MONITOR_PID}" ] && kill -0 "$(cat "${TURN_MONITOR_PID}")" 2>/dev/null; then
    true
  else
    rm -f "${TURN_MONITOR_PID}"
    CODEX_SUPERVISOR_SOURCE="${SUPERVISOR_SOURCE}" \
    CODEX_SUPERVISOR_CONFIG="${SUPERVISOR_CONFIG}" \
      setsid node /usr/local/bin/codex-supervisor-turn-monitor.mjs \
      </dev/null >>"${SUPERVISOR_LOG}" 2>&1 &
    echo $! >"${TURN_MONITOR_PID}"
    chmod 600 "${TURN_MONITOR_PID}" 2>/dev/null || true
  fi
fi

# Warm quota windows once per day without enabling codex-switch's account
# switching daemon. The account supervisor remains the sole switch owner.
if [ -x /usr/local/bin/codex-warmup-scheduler ] && \
   [ -x /home/codex/.local/bin/codex-switch ]; then
  WARMUP_RUNNING=0
  if [ -f "${WARMUP_SCHEDULER_PID}" ]; then
    WARMUP_PID_VALUE=$(cat "${WARMUP_SCHEDULER_PID}" 2>/dev/null || true)
    case "${WARMUP_PID_VALUE}" in
      (''|*[!0-9]*) ;;
      (*)
        WARMUP_PID_STATE=$(awk '/^State:/{print $2; exit}' "/proc/${WARMUP_PID_VALUE}/status" 2>/dev/null || true)
        if [ "${WARMUP_PID_STATE}" != "Z" ] && kill -0 "${WARMUP_PID_VALUE}" 2>/dev/null; then
          WARMUP_RUNNING=1
        fi
        ;;
    esac
  fi
  if [ "${WARMUP_RUNNING}" -eq 0 ]; then
    rm -f "${WARMUP_SCHEDULER_PID}"
    CODEX_WARMUP_TZ="${TZ:-Asia/Shanghai}" \
      setsid /usr/local/bin/codex-warmup-scheduler \
      </dev/null >>"${SUPERVISOR_LOG}" 2>&1 &
    echo $! >"${WARMUP_SCHEDULER_PID}"
    chmod 600 "${WARMUP_SCHEDULER_PID}" 2>/dev/null || true
  fi
fi

tmux has-session -t codex 2>/dev/null ||
  tmux new-session -d -s codex 'cd /workspace && exec bash'

exec tail -f /dev/null
