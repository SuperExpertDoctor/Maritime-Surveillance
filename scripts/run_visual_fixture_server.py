"""Start a read-only fixture backend for the browser acceptance suite."""
from __future__ import annotations

import argparse
import tempfile

import uvicorn

from scripts.evaluate_mixed_maritime import _FixtureGateway
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, Region
from src.vis.backend import server


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
