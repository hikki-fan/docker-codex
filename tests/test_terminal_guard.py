import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

GUARD = Path(__file__).resolve().parents[1] / 'docker/codex-terminal-guard.py'


class TerminalGuardTests(unittest.TestCase):
    def test_probe_crash_keeps_pty_alive_until_child_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('guard', sys.argv[1])
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
r = pathlib.Path(sys.argv[2])
child = "import time,pathlib; time.sleep(0.5); pathlib.Path(%r).touch()" % str(r/'finished')
def broken(): raise RuntimeError('probe failed unexpectedly')
g.run([sys.executable, '-c', child], r/'absent', probe=broken)
'''
            result = subprocess.run([sys.executable, '-c', worker, str(GUARD), tmp],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root/'finished').exists(), 'PTY hangup killed active child')

    def test_host_hangup_removes_lease_while_docker_is_attached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root/'docker'
            fake.write_text('''#!/usr/bin/env python3
import os, pathlib, sys, time
r = pathlib.Path(os.environ['TEST_ROOT'])
a = sys.argv[1:]
if 'mktemp' in a:
    (r/'lease').touch(); print(r/'lease')
elif 'touch' in a:
    (r/'lease').touch()
elif 'rm' in a:
    (r/'lease').unlink(missing_ok=True)
elif '-it' in a:
    (r/'attached').touch(); time.sleep(30)
''')
            fake.chmod(0o755)
            proc = subprocess.Popen(['/bin/sh', str(GUARD.with_name('codex-ssh')), 'resume'],
                env=dict(os.environ, TEST_ROOT=tmp, PATH=f'{tmp}:'+os.environ['PATH']),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+5
                while not (root/'attached').exists() and time.monotonic()<deadline:
                    time.sleep(0.02)
                self.assertTrue((root/'attached').exists())
                proc.send_signal(signal.SIGHUP)
                proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 129)
                self.assertFalse((root/'lease').exists())
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.communicate()

    def test_disconnected_busy_client_retained_then_idle_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('guard', sys.argv[1])
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
r = pathlib.Path(sys.argv[2])
child = "import os,time,pathlib; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(r/'pid')
sys.exit(g.run([sys.executable, '-c', child], r/'absent-lease',
               probe=lambda: (r/'idle').exists(), interval=0.1))
'''
            proc = subprocess.Popen([sys.executable, '-c', worker, str(GUARD), tmp],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            child_pid = None
            try:
                deadline = time.monotonic() + 5
                while not (root/'pid').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                child_pid = int((root/'pid').read_text())
                time.sleep(1.2)
                self.assertIsNone(proc.poll(), 'busy disconnected client was stopped')
                os.kill(child_pid, 0)
                (root/'idle').touch()
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, err)
                self.assertFalse(Path(f'/proc/{child_pid}').exists(), 'child not reaped')
            finally:
                if proc.poll() is None:
                    if child_pid:
                        try: os.kill(child_pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                    proc.kill()
                    proc.communicate()

    def test_missing_probe_fails_closed(self):
        spec = importlib.util.spec_from_file_location('guard', GUARD)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from unittest.mock import patch
        with patch.object(module.subprocess, 'run', side_effect=OSError('unavailable')):
            self.assertFalse(module.server_idle())


if __name__ == '__main__':
    unittest.main()
