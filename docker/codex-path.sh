# npm updates /usr/local/bin/codex; keep the shared-session wrapper first.
case "$PATH" in
  /opt/codex/bin|/opt/codex/bin:*) ;;
  *) export PATH="/opt/codex/bin:$PATH" ;;
esac
