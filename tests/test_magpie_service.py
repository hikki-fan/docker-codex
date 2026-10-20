"""Hermetic security and lifecycle tests for the isolated Magpie service."""

from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


DOCKER = Path(__file__).resolve().parents[1] / "docker"
SERVICE_PATH = DOCKER / "codex-magpie"
WRAPPER = DOCKER / "magpie-wrapper"

_loader = SourceFileLoader("codex_magpie_service", str(SERVICE_PATH))
service = _loader.load_module()


def proc_start_time(pid):
    """Return Linux /proc starttime (field 22), accounting for spaces in comm."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat.rsplit(")", 1)[1].split()[19]


class MagpieServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "magpie-state"
        self.tools = self.root / "tools"
        self.env = os.environ.copy()
        self.env.update({
            "CODEX_MAGPIE_DIR": str(self.state),
            "CODEX_TOOLS_ROOT": str(self.tools),
            "HOME": str(self.root / "original-home"),
        })
        Path(self.env["HOME"]).mkdir()

    def service_env(self):
        return patch.dict(os.environ, self.env, clear=True)

    def install_fake_magpie(self, body):
        binary = self.tools / "magpie" / "current" / "magpie"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/usr/bin/env python3\n" + body)
        binary.chmod(0o755)
        return binary

    def run_service(self, command, *, timeout=10, check=True):
        return subprocess.run(
            [sys.executable, str(SERVICE_PATH), command],
            env=self.env, capture_output=True, text=True, timeout=timeout,
            check=check,
        )

    def test_prepare_isolates_xdg_and_disables_account_discovery_updates(self):
        with self.service_env():
            base, child_env, key = service.prepare()

        self.assertEqual(base, self.state)
        self.assertEqual(child_env["XDG_CONFIG_HOME"], str(self.state / "config"))
        self.assertEqual(child_env["XDG_CACHE_HOME"], str(self.state / "cache"))
        self.assertEqual(child_env["XDG_DATA_HOME"], str(self.state / "data"))
        self.assertEqual(child_env["XDG_STATE_HOME"], str(self.state / "state"))
        self.assertEqual(child_env["MAGPIE_NO_ACCOUNTS"], "1")
        self.assertEqual(child_env["MAGPIE_ADDR"], "127.0.0.1:3425")
        self.assertEqual(child_env["HOME"], self.env["HOME"])

        settings = json.loads((self.state / "config/magpie/settings.json").read_text())
        self.assertTrue(settings["noAccountDiscovery"])
        self.assertTrue(settings["noAutoUpdate"])
        self.assertTrue(settings["noAutoDownload"])
        self.assertFalse(settings["autoRestart"])
        for name in ("config", "cache", "data", "state", "config/magpie"):
            self.assertTrue((self.state / name).is_dir())
        self.assertGreaterEqual(len(key), 16)

    def test_web_key_is_private_and_existing_key_and_settings_are_preserved(self):
        config = self.state / "config/magpie"
        config.mkdir(parents=True)
        settings_path = config / "settings.json"
        original_settings = b'{"operatorSetting":"keep exactly"}\n'
        settings_path.write_bytes(original_settings)
        key_path = self.state / "web.key"
        key_path.write_text("operator-supplied-test-key\n")
        key_path.chmod(0o644)

        with self.service_env():
            base, child_env, key = service.prepare()

        self.assertEqual(base, self.state)
        self.assertEqual(key, "operator-supplied-test-key")
        self.assertEqual(child_env["MAGPIE_WEB_KEY"], key)
        self.assertEqual(key_path.read_text(), "operator-supplied-test-key\n")
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(settings_path.read_bytes(), original_settings)

    def test_status_and_protected_logs_never_reveal_web_key(self):
        self.install_fake_magpie(
            "import sys\n"
            "sys.exit(0 if sys.argv[1:] == ['healthcheck'] else 0)\n"
        )
        with self.service_env():
            _, _, key = service.prepare()
            service.log_line(self.state, "test diagnostic " + key, key)

        status = self.run_service("status")
        log = (self.state / "service.log").read_text()
        self.assertNotIn(key, status.stdout)
        self.assertNotIn(key, status.stderr)
        self.assertNotIn(key, log)
        self.assertIn("[REDACTED]", log)
        self.assertEqual((self.state / "service.log").stat().st_mode & 0o777, 0o600)

    def test_refuses_broad_state_directories(self):
        for directory in ("/", "/home", "/home/codex", "/workspace", "/tmp"):
            with self.subTest(directory=directory), patch.dict(
                os.environ, {"CODEX_MAGPIE_DIR": directory}
            ):
                with self.assertRaisesRegex(RuntimeError, "broad"):
                    service.root()

    def test_pid_and_start_time_must_match_and_stop_ignores_unrelated_process(self):
        pid = os.getpid()
        start = proc_start_time(pid)
        self.assertTrue(service.process_alive(pid, start))
        self.assertFalse(service.process_alive(pid, str(int(start) + 1)))
        self.assertFalse(service.process_alive(2**30, "1"))

        self.state.mkdir()
        (self.state / "watchdog.json").write_text(
            json.dumps({"pid": pid, "start": start})
        )
        # This test runner is alive but is not the codex-magpie watchdog. A
        # matching pid/start pair alone must never authorize killing it.
        stopped = self.run_service("stop")
        self.assertIn("Magpie stopped", stopped.stdout)
        os.kill(pid, 0)
        with self.service_env():
            self.assertIsNone(service.owner())

        # A stale start time must not identify the current process as owner.
        (self.state / "watchdog.json").write_text(
            json.dumps({"pid": pid, "start": str(int(start) + 1)})
        )
        with self.service_env():
            self.assertIsNone(service.owner())

    def test_start_status_stop_and_watchdog_restart_with_fake_binary(self):
        runs = self.root / "web-runs.log"
        self.env["CODEX_TEST_WEB_RUNS"] = str(runs)
        self.install_fake_magpie(
            "import os, pathlib, sys, time\n"
            "if sys.argv[1:] == ['healthcheck']:\n"
            "    raise SystemExit(0)\n"
            "p = pathlib.Path(os.environ['CODEX_TEST_WEB_RUNS'])\n"
            "with p.open('a') as out:\n"
            "    out.write('start\\n')\n"
            "    out.flush()\n"
            "    count = sum(1 for _ in p.open())\n"
            "if count == 1:\n"
            "    raise SystemExit(17)\n"
            "while True:\n"
            "    time.sleep(0.1)\n"
        )

        try:
            started = self.run_service("start", timeout=10)
            self.assertIn("Magpie started", started.stdout)

            status = self.run_service("status", timeout=10)
            status_data = json.loads(status.stdout)
            self.assertTrue(status_data["running"])
            self.assertTrue(status_data["healthy"])
            key = (self.state / "web.key").read_text().strip()
            self.assertNotIn(key, status.stdout)

            # The first fake web process exits. The watchdog retries using its
            # bounded, production interval; this is why the test has a deadline.
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if runs.exists() and len(runs.read_text().splitlines()) >= 2:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(len(runs.read_text().splitlines()), 2)

            stopped = self.run_service("stop", timeout=10)
            self.assertIn("Magpie stopped", stopped.stdout)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and (self.state / "watchdog.json").exists():
                time.sleep(0.05)
            self.assertFalse((self.state / "watchdog.json").exists())
        finally:
            # Keep a failing assertion from leaving the isolated watchdog behind.
            if (self.state / "watchdog.json").exists():
                self.run_service("stop", timeout=10, check=False)


class MagpieWrapperTests(unittest.TestCase):
    def test_wrapper_scopes_xdg_and_magpie_settings_without_changing_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "service-state"
            tools = root / "tools"
            home = root / "original-home"
            home.mkdir()
            marker = home / "existing-file"
            marker.write_text("leave me alone")
            before = sorted(path.name for path in home.iterdir())

            binary = tools / "magpie" / "current" / "magpie"
            binary.parent.mkdir(parents=True)
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "keys = ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', "
                "'XDG_STATE_HOME', 'MAGPIE_NO_ACCOUNTS', 'MAGPIE_ADDR', 'DO_NOT_TRACK', "
                "'MAGPIE_WEB_KEY', 'KEEP_ME')\n"
                "print(json.dumps({key: os.environ.get(key) for key in keys}))\n"
                "print(json.dumps(sys.argv[1:]))\n"
            )
            binary.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "CODEX_MAGPIE_DIR": str(base),
                "CODEX_TOOLS_ROOT": str(tools),
                "HOME": str(home),
                "XDG_CONFIG_HOME": "/old/config",
                "XDG_CACHE_HOME": "/old/cache",
                "XDG_DATA_HOME": "/old/data",
                "XDG_STATE_HOME": "/old/state",
                "MAGPIE_NO_ACCOUNTS": "0",
                "MAGPIE_ADDR": "0.0.0.0:9999",
                "DO_NOT_TRACK": "0",
                "MAGPIE_WEB_KEY": "must-not-be-injected-by-wrapper",
                "KEEP_ME": "unchanged",
            })

            result = subprocess.run(
                ["bash", str(WRAPPER), "--version"], env=env,
                capture_output=True, text=True, check=True, timeout=10,
            )
            lines = result.stdout.splitlines()
            observed = json.loads(lines[0])
            self.assertEqual(json.loads(lines[1]), ["--version"])
            self.assertEqual(observed["HOME"], str(home))
            self.assertEqual(observed["XDG_CONFIG_HOME"], str(base / "config"))
            self.assertEqual(observed["XDG_CACHE_HOME"], str(base / "cache"))
            self.assertEqual(observed["XDG_DATA_HOME"], str(base / "data"))
            self.assertEqual(observed["XDG_STATE_HOME"], str(base / "state"))
            self.assertEqual(observed["MAGPIE_NO_ACCOUNTS"], "0")
            self.assertEqual(observed["MAGPIE_ADDR"], "0.0.0.0:9999")
            self.assertEqual(observed["DO_NOT_TRACK"], "1")
            self.assertEqual(observed["MAGPIE_WEB_KEY"], "must-not-be-injected-by-wrapper")
            self.assertEqual(observed["KEEP_ME"], "unchanged")
            self.assertEqual(sorted(path.name for path in home.iterdir()), before)
            self.assertEqual(marker.read_text(), "leave me alone")
            self.assertFalse(base.exists(), "the wrapper should only configure env, not mutate state")


if __name__ == "__main__":
    unittest.main()
