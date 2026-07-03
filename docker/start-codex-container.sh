#!/usr/bin/env bash
set -e

codex-relay --bg

tmux has-session -t codex 2>/dev/null ||
  tmux new-session -d -s codex 'cd /workspace && codex; exec bash'

tail -f /dev/null
