"""Hermetic tests for the persistent manual-update manager."""

from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docker" / "codex-tools"
LOADER = importlib.machinery.SourceFileLoader("codex_tools_under_test", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
codex_tools = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = codex_tools
LOADER.exec_module(codex_tools)


def _managed_current(root: Path, tool: str, version: str, cli_relative: str) -> tuple[Path, Path]:
    tool_root, versions = codex_tools._prepare_tool(root, tool)
    version_dir = versions / version
    version_dir.mkdir(mode=0o700)
    cli = version_dir / cli_relative
    cli.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    cli.write_text("fixture", encoding="utf-8")
    (version_dir / ".codex-tools-version").write_text(version + "\n", encoding="utf-8")
    (version_dir / ".codex-tools-version").chmod(0o600)
    codex_tools._switch_current(tool_root, version_dir)
    return tool_root, version_dir


def test_magpie_sha_failure_keeps_previous_current_and_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "managed-tools"
    tool_root, old_dir = _managed_current(root, "magpie", "0.1.1", "magpie")
    old_binary = old_dir / "magpie"
    old_binary.write_bytes(b"previous magpie")

    bad_payload = b"corrupted new magpie"
    expected = hashlib.sha256(b"correct new magpie").hexdigest()
    release = {
        "version": "0.1.2",
        "assets": {
            "magpie-cli-linux-amd64": {
                "url": "https://github.com/yetone/magpie-releases/releases/download/v0.1.2/magpie-cli-linux-amd64",
                "sha256": expected,
                "size": len(bad_payload),
            }
        },
    }
    monkeypatch.setattr(codex_tools, "_fetch_json", lambda _url: release)

    def corrupt_download(_url: str, destination: Path, _max_bytes: int = codex_tools.MAX_BINARY_BYTES) -> int:
        destination.write_bytes(bad_payload)
        return len(bad_payload)

    monkeypatch.setattr(codex_tools, "_download_to_file", corrupt_download)
    versions = tool_root / "versions"
    with pytest.raises(codex_tools.ManagerError, match="SHA-256 verification failed"):
        codex_tools._install_magpie(tool_root, versions, None)

    assert os.readlink(tool_root / "current") == "versions/0.1.1"
    assert old_binary.read_bytes() == b"previous magpie"
    assert not (versions / "0.1.2").exists()
    assert list(versions.glob(".staging-magpie-*")) == []


def test_current_switch_uses_atomic_replace_and_retains_old_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "managed-tools"
    tool_root, old_dir = _managed_current(root, "codex", "old", "node_modules/@openai/codex/bin/codex.js")
    new_dir = tool_root / "versions" / "new"
    new_dir.mkdir(mode=0o700)
    (new_dir / "node_modules/@openai/codex/bin").mkdir(mode=0o700, parents=True)
    (new_dir / "node_modules/@openai/codex/bin/codex.js").write_text("new", encoding="utf-8")

    real_replace = os.replace
    seen: list[tuple[str, str]] = []

    def observe_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        assert source_path.is_symlink()
        seen.append((os.readlink(source_path), Path(destination).name))
        real_replace(source, destination)

    monkeypatch.setattr(codex_tools.os, "replace", observe_replace)
    codex_tools._switch_current(tool_root, new_dir)

    assert seen == [("versions/new", "current")]
    assert os.readlink(tool_root / "current") == "versions/new"
    assert old_dir.is_dir()
    assert not list(tool_root.glob(".current-*"))


def test_update_lock_excludes_a_second_flock(tmp_path: Path):
    root = tmp_path / "managed-tools"
    with codex_tools.update_lock(root):
        fd = os.open(root / ".update.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_npm_update_uses_local_prefix_and_validates_package_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "managed-tools"
    tool_root, versions = codex_tools._prepare_tool(root, "codex")
    calls: list[list[str]] = []

    def fake_command(
        args: list[str], *, env: dict[str, str] | None = None, timeout: int = 30
    ) -> str:
        calls.append(list(args))
        if args[0] == "npm":
            assert args[1] == "install"
            assert timeout == 600
            prefix = Path(args[args.index("--prefix") + 1])
            assert prefix.parent == versions
            package = "@openai/codex"
            package_dir = prefix / "node_modules" / "@openai" / "codex"
            (package_dir / "bin").mkdir(mode=0o700, parents=True)
            (package_dir / "bin" / "codex.js").write_text("fixture", encoding="utf-8")
            (package_dir / "package.json").write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
            assert args[-1] == f"{package}@1.2.3"
            return ""
        assert args[0] == "node"
        assert timeout == 30
        assert Path(args[-2]).name == "codex.js"
        assert args[-1] == "--version"
        return "codex-cli 1.2.3\n"

    monkeypatch.setattr(codex_tools, "_run_command", fake_command)
    version = codex_tools._install_npm_tool("codex", tool_root, versions, "1.2.3")

    assert version == "1.2.3"
    install_args = calls[0]
    assert "--prefix" in install_args
    assert "-g" not in install_args and "--global" not in install_args
    assert install_args[-1] == "@openai/codex@1.2.3"
    assert calls[1][0] == "node"
    assert calls[1][-1] == "--version"
    assert (tool_root / "current").is_symlink()


def test_magpie_version_probe_uses_isolated_home_and_disables_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "managed-tools"
    tool_root, versions = codex_tools._prepare_tool(root, "magpie")
    payload = b"verified magpie payload"
    checksum = hashlib.sha256(payload).hexdigest()
    release = {
        "version": "0.1.9",
        "assets": {
            "magpie-cli-linux-amd64": {
                "url": "https://github.com/yetone/magpie-releases/releases/download/v0.1.9/magpie-cli-linux-amd64",
                "sha256": checksum,
                "size": len(payload),
            }
        },
    }
    monkeypatch.setattr(codex_tools, "_fetch_json", lambda _url: release)

    def good_download(_url: str, destination: Path, _max_bytes: int = codex_tools.MAX_BINARY_BYTES) -> int:
        destination.write_bytes(payload)
        return len(payload)

    monkeypatch.setattr(codex_tools, "_download_to_file", good_download)
    probes: list[tuple[list[str], dict[str, str] | None, int]] = []

    def fake_command(
        args: list[str], *, env: dict[str, str] | None = None, timeout: int = 30
    ) -> str:
        probes.append((args, env, timeout))
        return "magpie 0.1.9\n"

    monkeypatch.setattr(codex_tools, "_run_command", fake_command)
    assert codex_tools._install_magpie(tool_root, versions, None) == "0.1.9"

    args, env, timeout = probes[0]
    assert args[-1] == "--version"
    assert timeout == 30
    assert env is not None
    assert env["MAGPIE_NO_ACCOUNTS"] == "1"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["HOME"] != os.environ.get("HOME")
    assert env["XDG_CONFIG_HOME"].startswith(env["HOME"])
    assert os.readlink(tool_root / "current") == "versions/0.1.9"


def test_status_cli_reports_managed_versions_and_paths_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    root = tmp_path / "managed-tools"
    tool_root, version_dir = _managed_current(
        root,
        "relay",
        "2.4.6",
        "node_modules/codex-relay/dist/cli.js",
    )
    assert tool_root.name == "relay"
    assert version_dir.is_dir()
    monkeypatch.setenv("CODEX_TOOLS_ROOT", str(root))

    assert codex_tools.main(["status"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.strip() == f"relay: version=2.4.6 path={root}/relay/current/node_modules/codex-relay/dist/cli.js"
    assert "token" not in output.out.lower()


def test_status_executable_is_hermetic_and_has_no_external_command(tmp_path: Path):
    root = tmp_path / "missing"
    env = os.environ.copy()
    env["CODEX_TOOLS_ROOT"] = str(root)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "status"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert not root.exists()


def test_magpie_and_agy_pins_are_rejected_without_network_or_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "managed-tools"
    magpie_root, magpie_versions = codex_tools._prepare_tool(root, "magpie")
    agy_root, agy_versions = codex_tools._prepare_tool(root, "agy")
    monkeypatch.setattr(codex_tools, "_fetch_json", lambda _url: pytest.fail("must not fetch for an unsupported pin"))
    monkeypatch.setattr(codex_tools, "_download_to_file", lambda *_args, **_kwargs: pytest.fail("must not download for an unsupported pin"))

    with pytest.raises(codex_tools.ManagerError, match="Magpie version pinning is not supported"):
        codex_tools._install_magpie(magpie_root, magpie_versions, "0.1.2")
    with pytest.raises(codex_tools.ManagerError, match="Ag y version pinning is not supported"):
        codex_tools._install_agy(agy_root, agy_versions, "1.2.3")
