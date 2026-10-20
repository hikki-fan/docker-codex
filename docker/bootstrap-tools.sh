#!/usr/bin/env bash
set -euo pipefail
# The source is on the existing workspace bind mount: helper fixes survive
# recreation even while the base image stays pinned for manual maintenance.
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
backup_dir=/home/codex/.local/share/codex-tools/bootstrap-backups
mkdir -p "$backup_dir" /opt/codex/bin
chmod 700 "$backup_dir"
copy_helper() {
  local source="$1" target="$2"
  if [ -f "$target" ] && cmp -s "$source_dir/$source" "$target"; then
    return
  fi
  if [ -L "$target" ]; then
    mv -- "$target" "$backup_dir/$(basename "$target").$(date +%s%N).link"
  elif [ -f "$target" ]; then
    cp -p -- "$target" "$backup_dir/$(basename "$target").$(date +%s%N)"
  fi
  install -m 755 "$source_dir/$source" "$target"
}
copy_helper codex-real-wrapper /usr/local/bin/codex-real
copy_helper codex-wrapper /opt/codex/bin/codex
copy_helper codex-relay-wrapper /opt/codex/bin/codex-relay
copy_helper agy-wrapper /opt/codex/bin/agy
copy_helper magpie-wrapper /opt/codex/bin/magpie
copy_helper codex-tools /usr/local/bin/codex-tools
copy_helper codex-magpie /usr/local/bin/codex-magpie
