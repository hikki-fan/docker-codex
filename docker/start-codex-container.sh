#!/usr/bin/env bash
set -e

# Keep Relay and terminal TUIs on the same app-server. Terminal sessions must
# attach with `codex resume --remote unix:// [SESSION_ID]` instead of starting
# a second standalone app-server for the same thread.
codex-relay --bg --shared-app-server

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

tmux has-session -t codex 2>/dev/null ||
  tmux new-session -d -s codex 'cd /workspace && exec bash'

exec tail -f /dev/null
