"""CLI entry point for the UAV maritime surveillance simulation."""
from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path
import shutil
import threading
import time

import uvicorn

from src.env.simulation import SimulationEngine
from src.mission.strategy_memory import StrategyMemoryStore
from src.schedule.config_loader import ConfigLoader
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend.frame_publisher import FramePublisher
from src.vis.backend.server import create_app


def _check_port_available(port: int) -> None:
    """Fail clearly on an occupied port; never terminate another process."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("0.0.0.0", port))
    except OSError as exc:
        raise RuntimeError(f"visualization port {port} is unavailable: {exc}") from exc


def _run_runtime_loop(engine: SimulationEngine, steps: int, on_step, *, start_server: bool) -> dict:
    """Keep paused live runs responsive without advancing time or replaying idle frames."""
    if not start_server:
        return engine.run(steps, on_step=on_step)

    completed = 0
    while completed < steps or engine.runtime_status == "paused_model":
        if engine.runtime_status == "finished":
            break
        if engine.runtime_status == "paused_model":
            # These boundaries consume commands on the simulation thread and
            # leave the clock untouched. Match step()'s command ordering.
            runtime_results = engine.apply_pending_runtime_commands()
            intent_results = engine.apply_pending_intent_commands()
            vessel_results = engine.apply_pending_vessel_commands()
            if runtime_results or intent_results or vessel_results:
                # Preserve a successful retry's decision payload, but never
                # repeat an old heavy decision for an unrelated edit/abort.
                result = (
                    engine.last_result
                    if runtime_results and engine.runtime_status == "running"
                    else {"trigger_type": "none", "action": None}
                )
                on_step(engine, result)
            if engine.runtime_status == "paused_model":
                time.sleep(0.1)
            continue

        previous_time = engine.clock.time
        result = engine.step()
        if engine.clock.time != previous_time:
            completed += 1
        on_step(engine, result)
        if engine.clock.time == previous_time and engine.runtime_status != "paused_model":
            break
    return engine.summary()


def clear_output_cache(output_dir: str = "outputs") -> int:
    """Remove cached run artifacts while preserving the output directory itself."""
    output_path = Path(output_dir).resolve()
    if output_path.name != "outputs":
        raise ValueError("output cache path must be an outputs directory")
    output_path.mkdir(parents=True, exist_ok=True)
    removed = 0
    for entry in output_path.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed += 1
    return removed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs")
    parser.add_argument("--steps", type=int, default=480)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-server", action="store_true")
    parser.add_argument("--hold-server", action="store_true")
    parser.add_argument("--step-delay", type=float, default=0.05)
    parser.add_argument("--skip-llm-probe", action="store_true")
    parser.add_argument("--llm-probe-timeout", type=float, default=20.0)
    parser.add_argument("--memory-version", default="baseline")
    parser.add_argument("--memory-root", default="outputs/strategy_memory")
    return parser


def main(
    config_path: str = "configs",
    steps: int = 480,
    port: int = 8765,
    start_server: bool = True,
    step_delay: float = 0.05,
    hold_server: bool = False,
    probe_llm: bool = True,
    llm_probe_timeout: float = 20.0,
    memory_version: str = "baseline",
    memory_root: str | os.PathLike[str] = "outputs/strategy_memory",
) -> dict:
    config = ConfigLoader.load(config_path)
    memory_store = StrategyMemoryStore(memory_root)
    if config.common.clear_outputs_before_run:
        output_path = Path("outputs").resolve()
        memory_path = Path(memory_root).resolve()
        if memory_path == output_path or output_path in memory_path.parents:
            raise ValueError(
                "memory-root must be outside outputs when clear_outputs_before_run is enabled"
            )
        removed = clear_output_cache()
        print(f"Cleared {removed} cached output item(s)")
    resolved_memory_version = memory_store.resolve_version(memory_version)
    engine = SimulationEngine(
        config,
        strategy_memory_store=memory_store,
        strategy_memory_version=resolved_memory_version,
    )
    if probe_llm:
        engine.allocator.llm_client.probe(llm_probe_timeout)
        print("LongCat-2.0 connectivity probe passed")
    app = None
    logger = None

    if start_server:
        _check_port_available(port)
        app = create_app(config, engine.allocator.sm, engine=engine)
        app.state.total_steps = steps
        def run_server():
            uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

        threading.Thread(target=run_server, daemon=True).start()
        deadline = time.time() + 5
        while getattr(app.state, "event_loop", None) is None and time.time() < deadline:
            time.sleep(0.05)
        if getattr(app.state, "event_loop", None) is None:
            raise RuntimeError(f"visualization backend did not start on port {port}")
        print(f"可视化服务已启动: http://localhost:{port}")
    else:
        logger = FrameLogger()
    frame_publisher = FramePublisher(
        app.state.frame_logger if app is not None else logger,
        app=app,
    )

    def publish(current_engine: SimulationEngine, result: dict) -> None:
        sm = current_engine.allocator.sm
        llm_cycle = result.get("llm_cycle")
        if app is not None:
            app.state.ships = current_engine.ships
            app.state.uav_entities = current_engine.uavs
            app.state.obstacles = current_engine.obstacles
            app.state.bases = current_engine.bases
            app.state.current_cycle = sm.cycle
            app.state.total_steps = steps
            if llm_cycle is not None:
                app.state.llm_cycle = llm_cycle
        frame_publisher.push_snapshot(current_engine, result, steps)

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

    try:
        summary = _run_runtime_loop(engine, steps, publish, start_server=start_server)
        # The engine currently enters finished only on an operator abort.
        # Capture that before marking normal CLI completion as read-only.
        aborted = engine.runtime_status == "finished"
        if start_server and engine.runtime_status == "running":
            engine._set_runtime_state("finished")
            # Reject commands queued during the last step/publication instead
            # of leaving their receipts pending forever. New writes are gated
            # by the API's finished check.
            engine.apply_pending_runtime_commands()
            engine.apply_pending_intent_commands()
            engine.apply_pending_vessel_commands()
            publish(engine, {"trigger_type": "none", "action": None})
            summary = engine.summary()
    finally:
        try:
            if not frame_publisher.flush(timeout=120):
                raise RuntimeError("timed out while flushing replay frames")
        finally:
            frame_publisher.close()
    output_path = app.state.frame_logger.path if app is not None else logger.path
    summary["jsonl_path"] = output_path
    if engine.runtime_status == "paused_model":
        print(f"仿真提前暂停：完成 {summary['steps']}/{steps} 步，模型决策失败；详见 JSONL 日志。")
    else:
        print(f"仿真运行结束：完成 {summary['steps']}/{steps} 步。")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSONL 日志: {output_path}")
    if hold_server and app is not None and not aborted:
        print(f"网页服务保持只读回放，按 Ctrl+C 停止: http://localhost:{port}")
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
        probe_llm=not args.skip_llm_probe,
        llm_probe_timeout=args.llm_probe_timeout,
        memory_version=args.memory_version,
        memory_root=args.memory_root,
    )
