"""Short-run check: no sustained mid-map holding; every available UAV works."""
import sys

sys.path.insert(0, "/home/ubuntu/repos/Maritime-Surveillance")

from dataclasses import replace

from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader


def install_deterministic_llm(engine: SimulationEngine) -> None:
    def decide(_sm, _ivt, candidate_result, required_search_regions=0):
        engine.allocator.llm_client.last_interaction = {
            "success": True, "attempts": 1,
        }
        return {
            "search_regions": [
                {
                    "id": f"S{index + 1}",
                    "bbox": list(candidate["bbox"]),
                    "priority": "medium",
                }
                for index, candidate in enumerate(
                    candidate_result.candidate_regions[:required_search_regions]
                )
            ],
            "notes": "test",
        }

    engine.allocator.llm_client.decide = decide


def main() -> int:
    steps = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    config = ConfigLoader.load()
    config.ship = replace(
        config.ship,
        population=replace(
            config.ship.population, type_i_ratio=0.0, type_ii_ratio=1.0,
        ),
        type_ii_ais_on_probability=0.0,
    )
    engine = SimulationEngine(config, seed=23)
    install_deterministic_llm(engine)

    midmap_streaks: dict[str, int] = {}
    worst: dict[str, int] = {}
    hold_frames = 0
    for step in range(steps):
        engine.step()
        for uav in engine.uavs:
            if uav.status == "holding" and uav.id not in engine._holding_base_by_uav:
                midmap_streaks[uav.id] = midmap_streaks.get(uav.id, 0) + 1
                worst[uav.id] = max(worst.get(uav.id, 0), midmap_streaks[uav.id])
                hold_frames += 1
            else:
                midmap_streaks[uav.id] = 0
        if step % 30 == 0:
            statuses = [u.status for u in engine.uavs]
            print(
                f"t={engine.clock.time:.0f} statuses={statuses}",
                flush=True,
            )

    print(f"\nmid-map hold frames total: {hold_frames}")
    print(f"worst consecutive mid-map hold streaks per UAV: {worst}")
    max_streak = max(worst.values(), default=0)
    print(f"max consecutive mid-map holding: {max_streak} steps")
    # Holds may legitimately span a few steps while retask edges settle;
    # anything beyond the timeout constant is a failure.
    limit = engine._midmap_hold_timeout_min / engine.clock.dt_min + 2
    ok = max_streak <= limit
    print(f"limit={limit:.0f} steps -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
