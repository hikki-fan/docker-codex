import os
from pathlib import Path
import subprocess
import time


LAUNCHER = Path(__file__).resolve().parents[1] / 'docker' / 'codex-dsh-web'


def test_https_web_startup_and_private_link(tmp_path):
    stub = tmp_path / 'fake-dsh'
    stub.write_text(
        "#!/bin/bash\n"
        "case \"$*\" in *'web --host 127.0.0.1 --port 3080 --no-open --public-url https://localhost:3080/ --trusted-host localhost:3080 --tls-cert '*) ;; *) exit 90 ;; esac\n"
        "echo 'DSH: https://localhost:3080/?token=fixture'\n"
        "exec sleep 30\n"
    )
    stub.chmod(0o700)
    state = tmp_path / 'state'
    env = dict(os.environ, HOME=str(tmp_path), DSH_WEB_STATE_DIR=str(state),
               DSH_WEB_PUBLIC_URL='https://localhost:3080/',
               DSH_WEB_BIND_ADDRESS='127.0.0.1', DSH_WEB_BIN=str(stub))
    worker = subprocess.Popen(['bash', LAUNCHER, 'serve'], env=env)
    try:
        for _ in range(60):
            probe = subprocess.run(['bash', LAUNCHER, 'access'], env=env,
                                   capture_output=True, text=True, check=False)
            if probe.returncode == 0 or worker.poll() is not None:
                break
            time.sleep(0.05)
        assert probe.stdout.strip() == 'https://localhost:3080/?token=fixture'
        assert (state / 'start.log').stat().st_mode & 0o777 == 0o600
        assert (state / 'tls.key').stat().st_mode & 0o777 == 0o600
        assert subprocess.run(['bash', LAUNCHER, 'serve'], env=env, timeout=2).returncode == 0
    finally:
        worker.terminate()
        worker.wait(timeout=5)
    stale = subprocess.run(['bash', LAUNCHER, 'access'], env=env,
                           capture_output=True, text=True, check=False)
    assert stale.returncode != 0
    assert 'fixture' not in stale.stdout
