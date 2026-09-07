"""Deterministic tests for the five-day, two-account warmup scheduler."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "docker" / "codex-warmup-scheduler"


def make_fake_switch(tmp_path: Path) -> tuple[Path, Path]:
    binary = tmp_path / "codex-switch"
    calls = tmp_path / "calls.log"
    binary.write_text(
        """#!/bin/sh
if [ \"$1\" = \"--json\" ] && [ \"$2\" = \"list\" ]; then
  printf '%s\\n' '{\"profiles\":[{\"alias\":\"alpha\"},{\"alias\":\"beta\"}]}'
  exit 0
fi
if [ \"$1\" = \"warmup\" ] && [ \"$2\" = \"--json\" ]; then
  printf '%s\\n' \"$3\" >> \"$FAKE_CALLS\"
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary, calls


def run_at(tmp_path: Path, binary: Path, calls: Path, when: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "CODEX_WARMUP_BIN": str(binary),
            "CODEX_WARMUP_STATE_FILE": str(tmp_path / "state.json"),
            "CODEX_WARMUP_LOG_FILE": str(tmp_path / "warmup.log"),
            "CODEX_WARMUP_EPOCH": "2026-01-01T00:00:00",
            "CODEX_WARMUP_TZ": "Asia/Shanghai",
            "CODEX_WARMUP_NOW": when,
            "CODEX_WARMUP_ONCE": "1",
            "CODEX_WARMUP_A_ALIAS": "alpha",
            "CODEX_WARMUP_B_ALIAS": "beta",
            "FAKE_CALLS": str(calls),
        }
    )
    result = subprocess.run([str(SCHEDULER)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def calls_at(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return calls.read_text(encoding="utf-8").splitlines()


def test_each_account_is_due_every_five_hours_and_is_idempotent(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)

    run_at(tmp_path, binary, calls, "2026-01-01T00:00:00")
    run_at(tmp_path, binary, calls, "2026-01-01T00:00:00")
    assert calls_at(calls) == ["alpha"]

    # B is exactly 150 minutes after A; A's next slot is five hours after A0.
    run_at(tmp_path, binary, calls, "2026-01-01T02:30:00")
    run_at(tmp_path, binary, calls, "2026-01-01T05:00:00")
    run_at(tmp_path, binary, calls, "2026-01-01T05:01:00")
    assert calls_at(calls) == ["alpha", "beta", "alpha"]

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["last_slot"] == {"A": 1, "B": 0}


def test_cycle_day_boundaries_and_b_cross_midnight(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)

    # Model a scheduler that has already processed through day-5 14:00/16:30.
    # This isolates the final pair and the day-6 rollover from catch-up logic.
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "epoch": "2026-01-01T00:00:00", "last_slot": {"A": 22, "B": 22}}),
        encoding="utf-8",
    )

    # Day 5 A 19:00 / B 21:30 are the final pair (slot 23).
    run_at(tmp_path, binary, calls, "2026-01-05T19:00:00")
    run_at(tmp_path, binary, calls, "2026-01-05T21:30:00")

    # Day 6 returns to day 1: A is 00:00, B is 02:30.
    run_at(tmp_path, binary, calls, "2026-01-06T00:00:00")
    run_at(tmp_path, binary, calls, "2026-01-06T02:30:00")
    assert calls_at(calls) == ["alpha", "beta", "alpha", "beta"]


def test_restart_catches_up_only_latest_missed_slot(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)

    # Starting after several missed slots does not burst all historical calls.
    run_at(tmp_path, binary, calls, "2026-01-02T12:00:00")
    assert calls_at(calls) == ["alpha", "beta"]

    # Re-running in the same slot after a simulated restart is a no-op.
    run_at(tmp_path, binary, calls, "2026-01-02T12:00:20")
    assert calls_at(calls) == ["alpha", "beta"]
