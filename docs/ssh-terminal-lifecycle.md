# SSH terminal lifecycle

Use the host-side launcher (run **on the NAS**, not inside the container):

```sh
sudo /bin/sh /share/Docker/codex/workspace/docker-codex/docker/codex-ssh resume
# Or enter a managed shell and run `codex` / `codex resume` there:
sudo /bin/sh /share/Docker/codex/workspace/docker-codex/docker/codex-ssh
```

The launcher supplies a unique lease, refreshed every 10 seconds. The container
wrapper owns a private PTY for each default shared-server resume client. A missing
lease, 45-second lease expiry, terminal EOF, or hangup detaches the display.
The guard keeps draining the PTY, preserving a running client until the shared
app-server reports its associated threads idle on two checks 10 seconds apart.
Busy, approval-waiting, unknown state, and failed probes retain the client.
The guard then kills and reaps only its own TUI child, never the shared server.
It does not save terminal output or send approval/interrupt RPCs.

A local Unix-socket WebSocket proxy records the actual thread IDs returned by
successful start/resume/fork responses, including picker selections. It forwards
messages without generating approvals or task actions, and stores only thread IDs
in a private temporary directory (not prompts, credentials, or transcripts).
If ownership is unknown or tracking fails, checks conservatively fall back to
**all** loaded threads. Known ownership permits cleanup even if an unrelated
thread is busy. Each managed window adds a small Node proxy process, which exits
with its guard; existing windows retain the previous behavior until reopened.
Large messages are forwarded without parsing to avoid duplicating long histories
in the proxy heap; they invalidate precise tracking and use the conservative
fallback until a later small successful association response.
It is not an idle timeout for connected windows. Host SSH loss must first
be detected by sshd: abrupt network blackholes can take longer than a window close.
No system-wide SSH timeout settings are changed.

Ordinary `docker exec -it codex ...`, existing windows, tmux, explicit `--remote`
overrides, `codex exec`, and direct `codex-real` do not get this lifecycle guard.
Do not claim they are covered. For managed entry, plain `codex` maps to shared
`resume`; other CLI subcommands retain their original behavior. Do not use a
managed shell for intentionally detached tmux sessions.

If the server is unavailable, the guard intentionally remains alive instead of
risking task cancellation. A server/container restart is outside this protection.
For tasks waiting on approval, reconnect via Relay or a fresh managed terminal.
The mechanism does not grant approvals or guarantee cross-client writer takeover.
Raw protocol `command/exec` processes are connection-owned and may terminate on
disconnect; this feature protects ordinary thread turns, not arbitrary RPC-owned
processes. The isolated same-version integration check covers `thread/shellCommand`
turn completion, not paid model inference or an actual external SSH network failure.

## Container process reaping

Both the repository Compose template and the deployed NAS Compose set `init: true`.
Recreate the container to activate Docker's init process. It reaps orphaned children
instead of leaving them under `tail` as PID 1. It does not fix a living parent that
fails to reap its own children. No running container is restarted by these helpers.
