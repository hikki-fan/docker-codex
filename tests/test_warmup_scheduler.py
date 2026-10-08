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
  if [ \"${FAKE_LIST_MODE:-completed}\" = \"post_nonzero\" ] && [ \"$(wc -l < \"$FAKE_LIST_CALLS\")\" -gt 1 ]; then
    printf '%s\\n' 'post-refresh failure' >&2
    exit 1
  fi
  if [ -n \"$FAKE_LIST_PAYLOAD\" ]; then
    printf '%s\\n' \"$FAKE_LIST_PAYLOAD\"
    exit 0
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
    extra_env: dict[str, str] | None = None,
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
    if extra_env:
        env.update(extra_env)
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
    assert "account=A slot=0 post-refresh primary:" in warmup_log(tmp_path)
    assert list_calls_at(tmp_path) == ["--force", "--force"]


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


def test_early_active_skip_does_not_consume_slot_and_succeeds_on_delayed_retry(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)
    state_file = tmp_path / "state.json"

    # 1. At 00:00:00, slot 0 is due. Warmup returns skipped (active quota window).
    run_at(tmp_path, binary, calls, "2026-01-01T00:00:00", warmup_mode="skipped")
    assert calls_at(calls) == ["alpha"]
    assert "account=A slot=0 warmup skipped: active quota window" in warmup_log(tmp_path)

    # Verify slot was NOT consumed in state, and persistent retry state was created
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_slot"]["A"] == -1
    assert state["retry"]["A"]["slot"] == 0
    assert state["retry"]["A"]["attempts"] == 1
    assert state["retry"]["A"]["reason"] == "active_skip"

    # 2. At 00:00:20 (20s later), idle/regular poll runs.
    # Must NOT retry yet (anti-storm rate limit; retry delay >= 60s).
    run_at(tmp_path, binary, calls, "2026-01-01T00:00:20", warmup_mode="completed")
    assert calls_at(calls) == ["alpha"]  # No additional calls made

    # 3. At 00:01:00 (60s later), active quota window has expired upstream.
    # Scheduler retries and completes successfully.
    run_at(tmp_path, binary, calls, "2026-01-01T00:01:00", warmup_mode="completed")
    assert calls_at(calls) == ["alpha", "alpha"]
    log = warmup_log(tmp_path)
    assert "account=A slot=0 warmup completed" in log
    assert "account=A slot=0 post-refresh primary:" in log

    # Verify slot is now consumed and retry state cleared
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_slot"]["A"] == 0
    assert state["retry"]["A"] is None


def test_failure_rate_limiting_and_bounded_retries(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)
    state_file = tmp_path / "state.json"

    # Attempt 1 at 00:00:00 fails
    run_at(tmp_path, binary, calls, "2026-01-01T00:00:00", warmup_mode="nonzero")
    assert calls_at(calls) == ["alpha"]
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_slot"]["A"] == -1
    assert state["retry"]["A"]["attempts"] == 1

    # At 20s later: rate-limited, no retry!
    run_at(tmp_path, binary, calls, "2026-01-01T00:00:20", warmup_mode="nonzero")
    assert calls_at(calls) == ["alpha"]

    # Attempts 2, 3, 4 at 60s intervals
    run_at(tmp_path, binary, calls, "2026-01-01T00:01:00", warmup_mode="nonzero")
    assert calls_at(calls) == ["alpha", "alpha"]

    run_at(tmp_path, binary, calls, "2026-01-01T00:02:00", warmup_mode="nonzero")
    assert calls_at(calls) == ["alpha", "alpha", "alpha"]

    run_at(tmp_path, binary, calls, "2026-01-01T00:03:00", warmup_mode="nonzero")
    assert calls_at(calls) == ["alpha", "alpha", "alpha", "alpha"]

    # Attempt 5 (default MAX_RETRIES = 5) at 00:04:00
    run_at(tmp_path, binary, calls, "2026-01-01T00:04:00", warmup_mode="nonzero")
    assert calls_at(calls) == ["alpha", "alpha", "alpha", "alpha", "alpha"]

    # Retry limit reached: slot is now consumed to bound failures and prevent infinite hammering
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_slot"]["A"] == 0
    assert state["retry"]["A"] is None
    log = warmup_log(tmp_path)
    assert "account=A slot=0 warmup retry limit reached (5 attempts); consuming slot" in log

    # Further run at 00:05:00 in same slot is a no-op
    run_at(tmp_path, binary, calls, "2026-01-01T00:05:00", warmup_mode="nonzero")
    assert calls_at(calls) == ["alpha", "alpha", "alpha", "alpha", "alpha"]


def test_post_refresh_parses_primary_usage_and_logs_sanitized(tmp_path: Path):
    rich_payload = json.dumps({
        "profiles": [
            {
                "alias": "alpha",
                "is_current": True,
                "account": {
                    "email": "user@internal.example",
                    "account_id": "acct_secret_abc123",
                    "plan": "team",
                },
                "usage": {
                    "fetched_at": "2026-01-01T00:00:05Z",
                    "primary": {
                        "label": "5h",
                        "used_percent": 18.5,
                        "resets_at": 1767243600,
                        "resets_in_seconds": 18000,
                    }
                }
            },
            {
                "alias": "beta",
                "usage": {
                    "primary": {
                        "label": "5h",
                        "used_percent": 0.0,
                        "resets_at": 1767252600,
                    }
                }
            }
        ]
    })
    binary, calls = make_fake_switch(tmp_path)
    state_file = tmp_path / "state.json"

    run_at(
        tmp_path,
        binary,
        calls,
        "2026-01-01T00:00:00",
        extra_env={"FAKE_LIST_PAYLOAD": rich_payload},
    )

    # 1 list call for discovery + 1 list call for post-refresh
    assert list_calls_at(tmp_path) == ["--force", "--force"]
    log = warmup_log(tmp_path)
    assert "account=A slot=0 warmup completed" in log
    assert "account=A slot=0 post-refresh primary: used=18.5% reset=" in log

    # Assert no sensitive credentials/tokens/emails are in logs
    assert "user@internal.example" not in log
    assert "acct_secret_abc123" not in log
    assert "Bearer" not in log

    # Check state file records sanitized usage
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "last_post_refresh" in state
    post_a = state["last_post_refresh"]["A"]
    assert post_a["slot"] == 0
    assert post_a["used_percent"] == 18.5
    assert post_a["resets_at"] == 1767243600

    # Ensure state file does not contain credentials
    state_raw = state_file.read_text(encoding="utf-8")
    assert "user@internal.example" not in state_raw
    assert "acct_secret_abc123" not in state_raw


def test_legacy_state_file_compatibility_and_upgrade(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)
    state_file = tmp_path / "state.json"

    # Write legacy version 1 state with only epoch and last_slot
    legacy_state = {
        "version": 1,
        "epoch": "2026-01-01T00:00:00",
        "last_slot": {"A": 1, "B": 1},
    }
    state_file.write_text(json.dumps(legacy_state), encoding="utf-8")

    # Run at 10:00:00 (slot 2 for A; B slot 2 is not due until 12:30)
    run_at(tmp_path, binary, calls, "2026-01-01T10:00:00")
    assert calls_at(calls) == ["alpha"]

    # Verify state was upgraded to version 2 with valid structure
    upgraded = json.loads(state_file.read_text(encoding="utf-8"))
    assert upgraded["version"] == 2
    assert upgraded["epoch"] == "2026-01-01T00:00:00"
    assert upgraded["last_slot"] == {"A": 2, "B": 1}
    assert "retry" in upgraded
    assert upgraded["retry"]["A"] is None
    assert "last_post_refresh" in upgraded


def test_unversioned_partial_legacy_state_loads_cleanly(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)
    state_file = tmp_path / "state.json"

    # Minimal state without version or B slot or epoch
    state_file.write_text(json.dumps({"last_slot": {"A": 0}}), encoding="utf-8")

    # Run at 02:30:00 (slot 0 for B)
    run_at(tmp_path, binary, calls, "2026-01-01T02:30:00")
    assert calls_at(calls) == ["beta"]

    upgraded = json.loads(state_file.read_text(encoding="utf-8"))
    assert upgraded["version"] == 2
    assert upgraded["last_slot"]["A"] == 0
    assert upgraded["last_slot"]["B"] == 0


def test_active_skip_bounded_retries(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)
    state_file = tmp_path / "state.json"

    # 5 consecutive active skips at 60s intervals
    for i in range(5):
        t = f"2026-01-01T00:0{i}:00"
        run_at(tmp_path, binary, calls, t, warmup_mode="skipped")
        assert len(calls_at(calls)) == i + 1

    # On attempt 5, retry limit reached: slot consumed
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_slot"]["A"] == 0
    assert state["retry"]["A"] is None
    log = warmup_log(tmp_path)
    assert "account=A slot=0 warmup retry limit reached (5 attempts); consuming slot" in log

    # Sixth run in same slot: slot is already marked, no calls made
    run_at(tmp_path, binary, calls, "2026-01-01T00:05:00", warmup_mode="skipped")
    assert len(calls_at(calls)) == 5


def test_post_refresh_failure_does_not_unmark_slot(tmp_path: Path):
    binary, calls = make_fake_switch(tmp_path)
    state_file = tmp_path / "state.json"

    run_at(
        tmp_path,
        binary,
        calls,
        "2026-01-01T00:00:00",
        list_mode="post_nonzero",
        warmup_mode="completed",
    )

    assert calls_at(calls) == ["alpha"]
    log = warmup_log(tmp_path)
    assert "account=A slot=0 warmup completed" in log
    assert "account=A slot=0 post-refresh failed exit_code=1" in log

    # Slot remains completed and saved despite post-refresh failure
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_slot"]["A"] == 0


def test_singleton_lock_prevents_duplicate_scheduler(tmp_path: Path):
    import fcntl
    binary, calls = make_fake_switch(tmp_path)
    lock_file = tmp_path / "state.json.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # Hold singleton lock
    handle = lock_file.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        run_at(tmp_path, binary, calls, "2026-01-01T00:00:00")
        assert calls_at(calls) == []
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
