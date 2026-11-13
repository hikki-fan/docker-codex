#!/usr/bin/env python3
"""Own one TUI PTY; on host disconnect drain it until the shared server is idle.

Only used by the opt-in host launcher. Never scans or kills other clients.
Unknown/busy server state retains the child. No transcript is written to disk.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import termios
import tempfile
import time
import tty


def server_idle(state_path=None):
    probe = Path(__file__).with_name("codex-terminal-idle.cjs")
    try:
        return subprocess.run(
            ["node", str(probe), *([str(state_path)] if state_path else [])], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_tracker(command, directory):
    """Only rewrite the default endpoint; failed tracking keeps conservative mode."""
    command = list(command)
    try:
        index = command.index('--remote') + 1
        if command[index] != 'unix://':
            return command, None, None
    except (ValueError, IndexError):
        return command, None, None
    socket_path = directory / 'client.sock'
    state_path = directory / 'state.json'
    upstream = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
    upstream = upstream/'app-server-control/app-server-control.sock'
    proc = subprocess.Popen([
        'node', str(Path(__file__).with_name('codex-terminal-proxy.cjs')),
        str(socket_path), str(state_path), str(upstream),
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 5
    while proc.poll() is None and time.monotonic() < deadline:
        try:
            if json.loads(state_path.read_text()).get('ready') and socket_path.exists():
                command[index] = f'unix://{socket_path}'
                return command, proc, state_path
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    stop_tracker(proc)
    return command, None, None


def stop_tracker(proc):
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc.wait()


def run(command, lease, probe=server_idle, interval=10):
    detached = False
    previous = termios.tcgetattr(0) if os.isatty(0) else None
    def detach(*_):
        nonlocal detached
        detached = True
    # Install before fork: the child resets these before exec.
    signal.signal(signal.SIGHUP, detach)
    signal.signal(signal.SIGTERM, detach)
    pid, master = pty.fork()
    if pid == 0:
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_DFL)
        os.execvpe(command[0], command, os.environ)
    def resize(*_):
        try:
            size = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
        except OSError:
            pass
    signal.signal(signal.SIGWINCH, resize)
    resize()
    next_probe = 0
    idle_checks = 0
    reaped = False
    exit_code = 0
    master_eof = False
    output = bytearray()
    output_flags = fcntl.fcntl(1, fcntl.F_GETFL)
    try:
        fcntl.fcntl(1, fcntl.F_SETFL, output_flags | os.O_NONBLOCK)
        if previous is not None:
            tty.setraw(0)
        while True:
            if not reaped:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    reaped = True
                    exit_code = os.waitstatus_to_exitcode(status)
            if reaped and master_eof and not output:
                return exit_code
            try:
                if time.time() - lease.stat().st_mtime > 45:
                    detached = True
            except FileNotFoundError:
                detached = True
            if detached:
                output.clear()
            if detached and not reaped and time.monotonic() >= next_probe:
                idle_checks = idle_checks + 1 if probe() else 0
                next_probe = time.monotonic() + interval
                if idle_checks >= 2:
                    # Kill only our direct child. SIGINT could send turn/interrupt.
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    reaped = True
                    return 0
            readers = [] if detached else [0]
            if not master_eof and (detached or len(output) < 1048576):
                readers.append(master)
            ready, writable, _ = select.select(readers, [1] if output else [], [], 1)
            if writable:
                try:
                    count = os.write(1, output)
                    del output[:count]
                except BlockingIOError:
                    pass
                except OSError:
                    detach()
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    if fd == master and exc.errno == errno.EIO:
                        master_eof = True
                        continue
                    detach()
                    continue
                if not data:
                    if fd == master:
                        master_eof = True
                    else:
                        detach()
                elif fd == 0:
                    os.write(master, data)
                elif not detached:
                    output.extend(data)
    finally:
        try:
            fcntl.fcntl(1, fcntl.F_SETFL, output_flags)
        except OSError:
            pass
        if previous is not None:
            try:
                termios.tcsetattr(0, termios.TCSANOW, previous)
            except OSError:
                pass
        # Closing the PTY master would HUP its child, even if *we* ignore HUP.
        # On an unexpected failure retain/drain the PTY until the child exits.
        # This sacrifices reclamation rather than an active task.
        if not reaped:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                try:
                    ready, _, _ = select.select([master], [], [], 1)
                    if ready:
                        os.read(master, 65536)
                except OSError:
                    time.sleep(0.2)
        os.close(master)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix='codex-tui-') as temporary:
        tracker = None
        try:
            command, tracker, state = start_tracker(sys.argv[1:], Path(temporary))
            sys.exit(run(command, Path(os.environ["CODEX_TERMINAL_LEASE"]),
                         probe=lambda: server_idle(state)))
        finally:
            stop_tracker(tracker)
