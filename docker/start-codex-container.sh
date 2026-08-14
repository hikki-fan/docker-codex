#!/usr/bin/env bash
set -e

codex-relay --bg

# Start the health watchdog when the bridge has been installed in the
# persistent workspace. Its singleton lock makes repeated starts harmless.
if [ -f /workspace/scripts/acp_watchdog.sh ]; then
  setsid bash /workspace/scripts/acp_watchdog.sh </dev/null >/dev/null 2>&1 &
fi

tmux has-session -t codex 2>/dev/null ||
  tmux new-session -d -s codex 'cd /workspace && exec bash'

exec tail -f /dev/null
