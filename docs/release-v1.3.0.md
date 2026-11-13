# v1.3.0 — SSH terminal lifecycle and container process reaping

SSH disconnects could leave Codex terminal clients running for days. This release
adds a POSIX-sh-compatible NAS launcher, a per-connection heartbeat, and a private
PTY guard. Disconnected clients stay alive while their associated threads are
busy and are reaped after two idle checks. Unknown ownership or failed status
queries retain the conservative all-thread check. Relay and the shared app-server
are never stopped by the terminal guard.

- The Unix-socket proxy records only actual thread IDs returned by successful
  start/resume/fork responses; no credentials, prompts, or terminal transcripts.
- Oversized messages pass through without parsing and fall back to conservative
  ownership checks. Explicit remote overrides and old windows are unchanged.
- Compose now uses `init: true` to reap orphaned children. Container recreation is
  required to activate init; this release does not recreate the running container.
- Existing unrelated Magpie/tool updates in the working tree are not included.

On the NAS:

```sh
sudo /bin/sh /share/Docker/codex/workspace/docker-codex/docker/codex-ssh resume
sudo /bin/sh /share/Docker/codex/workspace/docker-codex/docker/codex-ssh resume -C /workspace/AutoFilm
```

Validation covers POSIX host hangup/lease cleanup, PTY preservation during busy
turns and probe failures, wrapper argument handling, scoped idle checks,
transparent proxy/reconnect behavior, and isolated real app-server shell-turn
completion after a client disconnect. No paid inference or actual external SSH
network-loss test was performed. RPC connection-owned `command/exec` processes
are not covered by the ordinary thread-turn continuation guarantee.
