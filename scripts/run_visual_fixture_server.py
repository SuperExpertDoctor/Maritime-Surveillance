"""Start a read-only fixture backend for the browser acceptance suite."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import tempfile

import uvicorn

from scripts.evaluate_mixed_maritime import _FixtureGateway
from scripts.replay_restoration_scenarios import run_scenario
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, Region
from src.vis.backend import server


def _extend_replay_to_480(source: Path, target: Path) -> None:
    """Keep real early events and extend the final state for timeline checks."""
    frames = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not frames:
        raise RuntimeError(f"browser replay fixture contains no frames: {source}")
    if len(frames) > 480:
        raise RuntimeError(f"browser replay fixture exceeds 480 frames: {source}")
    while len(frames) < 480:
        frame = copy.deepcopy(frames[-1])
        frame_id = len(frames) + 1
        frame["frame_id"] = frame_id
        frame["sim_time_min"] = float(frame_id)
        frame["timestamp"] = (
            f"{frame_id // 60:02d}:{frame_id % 60:02d}:00"
        )
        frame["total_steps"] = 480
        frame["events"] = []
        frame["llm_cycle"] = None
        frames.append(frame)
    target.write_text(
        "".join(
            json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n"
            for frame in frames
        ),
        encoding="utf-8",
    )


def prepare_replay_compatibility_artifacts(output_dir: str | Path) -> None:
    """Add the legacy V06/V07 replay files to the generic browser fixture."""
    output_path = Path(output_dir)
    for scenario, steps, filename in (
        ("V06", 40, "v06-seed42-frames.jsonl"),
        # V07 reaches the real tracking phase at frame 43; retain a few
        # stable frames before extending the replay to the browser budget.
        ("V07", 45, "v07-seed42-frames.jsonl"),
    ):
        scenario_dir = output_path / f".{scenario.lower()}-seed42"
        result = run_scenario(
            scenario,
            seed=42,
            steps=steps,
            output_dir=scenario_dir,
            transport="fixture",
        )
        if result.get("status") != "finished":
            raise RuntimeError(
                f"browser replay fixture {scenario} did not finish: "
                f"{result.get('blocked_reason')}"
            )
        source = scenario_dir / "frames.jsonl"
        if not source.is_file():
            raise RuntimeError(f"browser replay fixture is missing {source}")
        _extend_replay_to_480(source, output_path / filename)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    config = ConfigLoader.load(args.config)
    engine = SimulationEngine(
        config,
        seed=101,
        llm_gateway=_FixtureGateway(),
        episode_id="browser-fixture-101",
    )
    # Keep browser verification independent from a developer's existing
    # outputs/ directory and from other local backend processes.
    server.OUTPUT_DIR = tempfile.mkdtemp(prefix="maritime-browser-fixture-")
    prepare_replay_compatibility_artifacts(server.OUTPUT_DIR)
    app = server.create_app(config, engine.allocator.sm, engine=engine)
    app.state.ships = engine.ships
    app.state.uav_entities = engine.uavs
    app.state.obstacles = engine.obstacles
    app.state.bases = engine.bases
    app.state.total_steps = 480
    engine.allocator.sm.set_search_regions([
        Region(
            id="SEARCH-FIXTURE",
            bbox=BBox(8, 8, 13, 13),
            type="search",
            priority="high",
            info_value=0.42,
            avg_info=0.18,
            completion_pct=24.0,
        ),
    ])
    app.state.llm_cycle = {
        "model": "fixture-model",
        "success": True,
        "system_prompt": "Return only legal task regions.",
        "user_prompt": "Fixture mission snapshot.",
        "response": '{"search_regions":[{"id":"SEARCH-FIXTURE"}]}',
        "validation": {"is_valid": True, "errors": []},
        "attempts": [{"response": "fixture response"}],
    }
    for cycle in range(app.state.total_steps):
        app.state.current_cycle = cycle
        app.state.frame_logger.write(server._build_frame_inner(app))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
