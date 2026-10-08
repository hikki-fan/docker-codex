import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


WRAPPER = Path(__file__).resolve().parents[1] / "docker" / "codex-wrapper"


class CodexWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.real = self.root / "real"
        self.real.write_text(
            '#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n'
        )
        self.real.chmod(0o755)
        self.env = dict(os.environ, CODEX_WRAPPER_REAL_BIN=str(self.real))

    def invoke(self, *args):
        result = subprocess.run(
            ["bash", str(WRAPPER), *args], env=self.env,
            check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def test_resume_uses_shared_server_and_preserves_arguments(self):
        self.assertEqual(
            self.invoke("resume", "thread-id", "--all", "a prompt with spaces"),
            ["resume", "--remote", "unix://", "thread-id", "--all", "a prompt with spaces"],
        )

    def test_explicit_remote_is_preserved(self):
        for remote in (["--remote", "ws://localhost:8788"], ["--remote=unix:///tmp/other.sock"]):
            args = ["resume", *remote, "thread-id"]
            self.assertEqual(self.invoke(*args), args)

    def test_non_interactive_commands_pass_through(self):
        for args in (["app-server", "--listen", "unix://"], ["exec", "resume", "thread-id"], ["--version"]):
            self.assertEqual(self.invoke(*args), args)

    def test_npm_bin_replacement_does_not_bypass_wrapper(self):
        stable = self.root / "stable"
        npm_bin = self.root / "npm-bin"
        stable.mkdir()
        npm_bin.mkdir()
        entry = stable / "codex"
        entry.write_bytes(WRAPPER.read_bytes())
        entry.chmod(0o755)
        (npm_bin / "codex").symlink_to(self.real)
        env = dict(self.env, PATH=f"{stable}:{npm_bin}:" + self.env["PATH"])
        # Simulate npm removing and recreating its global executable link.
        (npm_bin / "codex").unlink()
        (npm_bin / "codex").symlink_to(self.real)
        result = subprocess.run(["codex", "resume", "thread-id"], env=env,
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), ["resume", "--remote", "unix://", "thread-id"])


if __name__ == "__main__":
    unittest.main()
