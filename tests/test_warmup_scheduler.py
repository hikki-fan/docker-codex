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
  [ \"$3\" = \"--force\" ] || exit 3
  printf '%s\\n' \"$3\" >> \"$FAKE_LIST_CALLS\"
  if [ \"${FAKE_LIST_MODE:-completed}\" = \"nonzero\" ]; then
    printf '%s\\n' 'Bearer super-secret-token eyJabcdefghijklmnop.qwertyuiopasdfgh.zxcvbnmasdfghjk' >&2
    exit 1
  fi
  printf '%s\\n' '{\"profiles\":[{\"alias\":\"alpha\"},{\"alias\":\"beta\"}]}'
  exit 0
fi
if [ \"$1\" = \"warmup\" ] && [ \"$2\" = \"--json\" ]; then
  printf '%s\\n' \"$3\" >> \"$FAKE_CALLS\"
  case \"${FAKE_WARMUP_MODE:-completed}\" in
    completed)
      printf '{\"ok\":true,\"results\":[{\"alias\":\"%s\",\"ok\":true}]}\\n' \"$3\"
      exit 0
      ;;
    skipped)
      printf '{\"ok\":true,\"results\":[{\"alias\":\"%s\",\"ok\":true,\"skipped\":true}]}\\n' \"$3\"
      exit 0
      ;;
    semantic-failure)
      printf '{\"ok\":false,\"results\":[{\"alias\":\"%s\",\"ok\":false,\"error\":\"rate limit\"}]}\\n' \"$3\"
      exit 0
      ;;
    malformed)
      printf '%s\\n' 'not-json'
      exit 0
      ;;
    nonzero)
      printf '%s\\n' 'request failed' >&2
      exit 1
      ;;
  esac
fi
exit 2
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary, calls


def run_at(
    tmp_path: Path,
    binary: Path,
    calls: Path,
    when: str,
    *,
    warmup_mode: str = "completed",
    list_mode: str = "completed",
    explicit_aliases: bool = True,
) -> None:
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
            "FAKE_CALLS": str(calls),
            "FAKE_LIST_CALLS": str(tmp_path / "list-calls.log"),
            "FAKE_LIST_MODE": list_mode,
            "FAKE_WARMUP_MODE": warmup_mode,
        }
    )
    if explicit_aliases:
        env.update(
            {
                "CODEX_WARMUP_A_ALIAS": "alpha",
                "CODEX_WARMUP_B_ALIAS": "beta",
            }
        )
    else:
        env.pop("CODEX_WARMUP_A_ALIAS", None)
        env.pop("CODEX_WARMUP_B_ALIAS", None)
    result = subprocess.run([str(SCHEDULER)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def calls_at(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return calls.read_text(encoding="utf-8").splitlines()


def warmup_log(tmp_path: Path) -> str:
    path = tmp_path / "warmup.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def list_calls_at(tmp_path: Path) -> list[str]:
    path = tmp_path / "list-calls.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


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


def test_completed_result_is_logged_as_completed(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)

    run_at(tmp_path, binary, calls, "2026-01-01T00:00:00")

    assert "account=A slot=0 warmup completed" in warmup_log(tmp_path)
    assert list_calls_at(tmp_path) == ["--force"]


def test_profile_discovery_forces_a_fresh_usage_query(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)

    run_at(
        tmp_path,
        binary,
        calls,
        "2026-01-01T00:00:00",
        explicit_aliases=False,
    )

    assert calls_at(calls) == ["alpha"]
    assert "account=A slot=0 warmup completed" in warmup_log(tmp_path)


def test_skipped_result_is_not_logged_as_completed(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)

    run_at(
        tmp_path,
        binary,
        calls,
        "2026-01-01T00:00:00",
        warmup_mode="skipped",
    )

    log = warmup_log(tmp_path)
    assert "account=A slot=0 warmup skipped: active quota window" in log
    assert "warmup completed" not in log


def test_semantic_failure_is_not_masked_by_zero_exit_code(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)

    run_at(
        tmp_path,
        binary,
        calls,
        "2026-01-01T00:00:00",
        warmup_mode="semantic-failure",
    )

    log = warmup_log(tmp_path)
    assert "warmup failed detail=rate limit" in log
    assert "warmup completed" not in log

    # A failed scheduled attempt still consumes its slot. It must not retry on
    # the next 20-second poll and hammer the provider.
    run_at(
        tmp_path,
        binary,
        calls,
        "2026-01-01T00:00:20",
        warmup_mode="completed",
    )
    assert calls_at(calls) == ["alpha"]
    assert list_calls_at(tmp_path) == ["--force"]


def test_failed_profile_refresh_consumes_slot_and_redacts_credentials(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)

    run_at(
        tmp_path,
        binary,
        calls,
        "2026-01-01T00:00:00",
        list_mode="nonzero",
    )
    run_at(
        tmp_path,
        binary,
        calls,
        "2026-01-01T00:00:20",
        list_mode="completed",
    )

    assert calls_at(calls) == []
    assert list_calls_at(tmp_path) == ["--force"]
    log = warmup_log(tmp_path)
    assert "failed: forced profile refresh returned exit_code=1" in log
    assert "super-secret-token" not in log
    assert "eyJabcdefghijklmnop" not in log
    assert "Bearer [REDACTED_TOKEN]" in log
    assert "[REDACTED_JWT]" in log


def test_idle_poll_does_not_refresh_profiles(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "epoch": "2026-01-01T00:00:00",
                "last_slot": {"A": 0, "B": -1},
            }
        ),
        encoding="utf-8",
    )

    run_at(tmp_path, binary, calls, "2026-01-01T00:00:20")

    assert calls_at(calls) == []
    assert list_calls_at(tmp_path) == []


def test_malformed_json_and_nonzero_exit_are_logged_as_failures(tmp_path: Path):
    for mode, expected in (
        ("malformed", "invalid JSON result"),
        ("nonzero", "exit_code=1 detail=request failed"),
    ):
        case_dir = tmp_path / mode
        case_dir.mkdir()
        binary, calls = make_fake_switch(case_dir)

        run_at(
            case_dir,
            binary,
            calls,
            "2026-01-01T00:00:00",
            warmup_mode=mode,
        )

        log = warmup_log(case_dir)
        assert expected in log
        assert "warmup completed" not in log
