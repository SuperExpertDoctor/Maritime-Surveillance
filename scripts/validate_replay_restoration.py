"""CLI for real-engine replay restoration scenarios."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from scripts.replay_restoration_scenarios import (
    FEATURE_INTEGRATION_SCENARIOS,
    SCENARIOS,
    check_log,
    run_scenario,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--scenario", choices=SCENARIOS)
    selection.add_argument("--suite", choices=("feature-integration",))
    selection.add_argument("--check-log", type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--steps", type=int, default=120)
    parser.add_argument("--transport", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.steps < 1:
        parser.error("--steps must be positive")
    if args.check_log is not None:
        if args.output_dir is not None or args.transport != "fixture" or args.seed != 42 or args.steps != 120:
            parser.error("--check-log cannot be combined with run options")
        payload = check_log(args.check_log)
    else:
        if args.output_dir is None:
            parser.error("--output-dir is required for a run")
        if args.suite:
            payload = {
                "status": "finished",
                "suite": args.suite,
                "runs": [],
            }
            for scenario in FEATURE_INTEGRATION_SCENARIOS:
                result = run_scenario(
                    scenario,
                    seed=args.seed,
                    steps=args.steps,
                    output_dir=args.output_dir / scenario,
                    transport=args.transport,
                )
                payload["runs"].append(result)
                if result["status"] != "finished":
                    payload["status"] = result["status"]
        elif args.scenario:
            payload = run_scenario(
                args.scenario,
                seed=args.seed,
                steps=args.steps,
                output_dir=args.output_dir,
                transport=args.transport,
            )
        else:
            parser.error("one of --scenario, --suite, or --check-log is required")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") in {"finished", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
