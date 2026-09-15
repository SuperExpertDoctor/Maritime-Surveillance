import json
import subprocess
import sys


def _run(script, *args):
    return subprocess.run(
        [sys.executable, script, *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_evaluation_script_dry_run_never_requests_a_model(tmp_path):
    result = _run(
        "scripts/evaluate_mixed_maritime.py",
        "--scenario", "mixed-ais",
        "--seed", "42",
        "--steps", "10",
        "--output-dir", str(tmp_path),
    )
    payload = json.loads(result.stdout)

    assert payload["mode"] == "dry-run"
    assert payload["planned_episodes"] == 1
    assert payload["api_calls"] == 0


def test_strategy_validation_cli_reports_full_budget_without_live_calls(tmp_path):
    result = _run(
        "scripts/validate_strategy_memory.py",
        "--memory-id", "M0001",
        "--baseline-version", "baseline",
        "--phase", "validation",
        "--output-dir", str(tmp_path),
    )
    payload = json.loads(result.stdout)

    assert payload["mode"] == "dry-run"
    assert payload["planned_episodes"] == 60
    assert payload["api_calls"] == 0
