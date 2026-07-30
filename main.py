"""CLI entry point for the UAV maritime surveillance simulation."""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time

import uvicorn

from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.vis.backend.frame_builder import build_frame
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend.server import broadcast_frame_sync, create_app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs")
    parser.add_argument("--steps", type=int, default=480)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-server", action="store_true")
    parser.add_argument("--hold-server", action="store_true")
    parser.add_argument("--step-delay", type=float, default=0.05)
    return parser


def main(
    config_path: str = "configs",
    steps: int = 480,
    port: int = 8765,
    start_server: bool = True,
    step_delay: float = 0.05,
    hold_server: bool = False,
) -> dict:
    config = ConfigLoader.load(config_path)
    engine = SimulationEngine(config)
    app = None
    loop = None
    logger = None

    if start_server:
        app = create_app(config, engine.allocator.sm)
        app.state.total_steps = steps
        loop = asyncio.new_event_loop()

        def run_server():
            asyncio.set_event_loop(loop)
            uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

        threading.Thread(target=run_server, daemon=True).start()
        deadline = time.time() + 5
        while not loop.is_running() and time.time() < deadline:
            time.sleep(0.05)
        print(f"可视化服务已启动: http://localhost:{port}")
    else:
        logger = FrameLogger()

    def publish(current_engine: SimulationEngine, result: dict) -> None:
        sm = current_engine.allocator.sm
        llm_cycle = (
            result.get("llm_cycle")
            or current_engine.allocator.llm_client.last_interaction
        )
        if app is not None and loop is not None:
            app.state.ships = current_engine.ships
            app.state.uav_entities = current_engine.uavs
            app.state.obstacles = current_engine.obstacles
            app.state.current_cycle = sm.cycle
            app.state.total_steps = steps
            app.state.llm_cycle = llm_cycle
            future = broadcast_frame_sync(app, loop)
            if future is not None:
                future.result(timeout=10)
        else:
            frame = build_frame(
                sm,
                sm.cycle,
                config,
                total_steps=steps,
                llm_cycle=llm_cycle,
                ships=current_engine.ships,
                uav_entities=current_engine.uavs,
                obstacles=current_engine.obstacles,
            )
            logger.write(frame)

        if result["trigger_type"] != "none":
            print(
                f"[t={sm.current_time:.0f}min] Trigger: {result['trigger_type']} "
                f"- {result.get('action')}"
            )
        if int(sm.current_time) % 60 == 0:
            summary = current_engine.summary()
            print(
                f"[t={sm.current_time:.0f}min] 覆盖率 {summary['coverage_pct']:.1f}% | "
                f"发现 {summary['detected_ships']}/{summary['ship_count']} | "
                f"Heavy {summary['heavy_triggers']}"
            )
        if step_delay > 0:
            time.sleep(step_delay)

    summary = engine.run(steps, on_step=publish)
    output_path = app.state.frame_logger.path if app is not None else logger.path
    summary["jsonl_path"] = output_path
    print("仿真结束。")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSONL 日志: {output_path}")
    if hold_server and app is not None:
        print(f"服务将持续运行，按 Ctrl+C 停止: http://localhost:{port}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return summary


if __name__ == "__main__":
    args = _parser().parse_args()
    main(
        config_path=args.config,
        steps=args.steps,
        port=args.port,
        start_server=not args.no_server,
        step_delay=args.step_delay,
        hold_server=args.hold_server,
    )
