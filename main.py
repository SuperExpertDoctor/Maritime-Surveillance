"""CLI entry point for the UAV maritime surveillance simulation."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
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
from src.vis.backend.runtime_journal import publish_algorithm_events
from src.vis.backend.server import create_app


def _check_port_available(port: int) -> None:
    """Fail clearly on an occupied port; never terminate another process."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            # SO_REUSEADDR：界面「保存并重启仿真」通过 execv 整进程重启时，
            # 旧进程刚断开的客户端连接还处于 TIME_WAIT，不加会误报端口占用。
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", port))
    except OSError as exc:
        raise RuntimeError(f"visualization port {port} is unavailable: {exc}") from exc


def _run_runtime_loop(engine: SimulationEngine, steps: int, on_step, *, start_server: bool,
                      wall_seconds: float | None = None) -> dict:
    """Keep paused live runs responsive without advancing time or replaying idle frames."""
    if wall_seconds is not None and (not math.isfinite(wall_seconds) or wall_seconds <= 0):
        raise ValueError("wall_seconds must be finite and positive")
    if not start_server and wall_seconds is None:
        return engine.run(steps, on_step=on_step)

    started = time.monotonic()
    completed = 0
    paused_states = {"paused_model", "paused_safety"}
    while completed < steps or engine.runtime_status in paused_states:
        if wall_seconds is not None and time.monotonic() - started >= wall_seconds:
            break
        if engine.runtime_status == "finished":
            break
        if engine.runtime_status in paused_states:
            if not start_server:
                break
            # These boundaries consume commands on the simulation thread and
            # leave the clock untouched. Match step()'s command ordering.
            runtime_results = engine.apply_pending_runtime_commands()
            intent_results = engine.apply_pending_intent_commands()
            vessel_results = engine.apply_pending_vessel_commands()
            if runtime_results or intent_results or vessel_results:
                engine._publish_runtime_state()
                # Preserve a successful retry's decision payload, but never
                # repeat an old heavy decision for an unrelated edit/abort.
                result = (
                    engine.last_result
                    if runtime_results and engine.runtime_status == "running"
                    else {"trigger_type": "none", "action": None}
                )
                on_step(engine, {**result, "command_boundary": True})
            if engine.runtime_status in paused_states:
                time.sleep(0.1)
            continue

        previous_time = engine.clock.time
        result = engine.step(on_command_boundary=on_step)
        if engine.clock.time != previous_time:
            completed += 1
        on_step(engine, result)
        if engine.clock.time == previous_time and engine.runtime_status not in paused_states:
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
    parser.add_argument("--wall-seconds", type=float, default=None,
                        help="stop at a simulation boundary after this wall-clock budget")
    parser.add_argument("--run-report-dir", default=None,
                        help="new directory for final runtime evidence (never overwritten)")
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
    wall_seconds: float | None = None,
    run_report_dir: str | None = None,
) -> dict:
    run_started_label = datetime.now().strftime("%Y%m%d_%H%M%S")
    if wall_seconds is not None and (not math.isfinite(wall_seconds) or wall_seconds <= 0):
        raise ValueError("wall_seconds must be finite and positive")
    report_dir = Path(run_report_dir) if run_report_dir is not None else None
    if report_dir is not None and report_dir.exists():
        raise FileExistsError(f"run report directory already exists: {report_dir}")
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
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=False)
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
        app = create_app(
            config, engine.allocator.sm, engine=engine, config_dir=config_path,
        )
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

    def model_status(record: dict) -> None:
        level = "error" if record["status"] in {"failed", "timeout"} else (
            "warning" if record["status"] == "retry" else "info"
        )
        if app is not None:
            app.state.runtime_journal.append("llm", level, record["status"], **{
                key: record[key] for key in ("sim_time_min", "role", "call_id", "attempt")
                if key in record
            }, episode_id=engine.allocator.sm.episode_id)
        engine.allocator.sm.add_event("runtime_log", {
            "source": "llm", "level": level, **record,
        })

    gateway = getattr(engine.allocator.llm_client, "gateway", None)
    if callable(getattr(gateway, "set_event_sink", None)):
        gateway.set_event_sink(model_status)
    seen_algorithm_events: set[str] = set()
    last_logged_episode = None

    def publish(current_engine: SimulationEngine, result: dict) -> None:
        nonlocal last_logged_episode
        sm = current_engine.allocator.sm
        if sm.episode_id != last_logged_episode:
            seen_algorithm_events.clear()
            last_logged_episode = sm.episode_id
        publish_algorithm_events(
            sm, app.state.runtime_journal if app is not None else None,
            seen_algorithm_events,
        )
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
        if result.get("command_boundary"):
            return

        if result["trigger_type"] != "none":
            detail = (
                f" | role={result.get('blocked_role')} reason={result.get('blocked_reason')}"
                " | 仿真已暂停，请在右侧模型暂停面板点击重试"
                if result["trigger_type"] == "model_blocked" else ""
            )
            if app is not None:
                app.state.runtime_journal.append(
                    "scheduler", "info", result.get("action") or "triggered",
                    sim_time_min=sm.current_time,
                    trigger_type=result["trigger_type"],
                    episode_id=sm.episode_id,
                )
            print(
                f"[t={sm.current_time:.0f}min] Trigger: {result['trigger_type']} "
                f"- {result.get('action')}{detail}"
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

    run_started = time.monotonic()
    try:
        summary = _run_runtime_loop(engine, steps, publish, start_server=start_server,
                                    wall_seconds=wall_seconds)
        elapsed_wall_seconds = time.monotonic() - run_started
        runtime_before_finalize = engine.runtime_status
        # The engine currently enters finished only on an operator abort.
        # Capture that before marking normal CLI completion as read-only.
        aborted = engine.runtime_status == "finished"
        if start_server and (engine.runtime_status == "running" or
                             (wall_seconds is not None and engine.runtime_status in {"paused_model", "paused_safety"})):
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
    if app is not None:
        app.state.runtime_journal.append(
            "runtime", "error" if engine.runtime_status == "paused_model" else "info",
            engine.runtime_status, sim_time_min=engine.allocator.sm.current_time,
            episode_id=engine.allocator.sm.episode_id,
        )
    output_path = app.state.frame_logger.path if app is not None else logger.path
    # Archive the run's replay into outputs/<start-date-time>/ so every live
    # session keeps its recording under a directory named after the moment
    # the program started.
    try:
        run_dir = Path("outputs") / run_started_label
        source = Path(output_path)
        if source.is_file() and source.resolve().parent == Path("outputs").resolve():
            run_dir.mkdir(parents=True, exist_ok=True)
            target = run_dir / source.name
            source.replace(target)
            output_path = str(target)
    except OSError as exc:
        print(f"回放文件归档失败: {exc}")
    summary["jsonl_path"] = output_path
    summary["wall_seconds"] = elapsed_wall_seconds
    summary["runtime_before_finalize"] = runtime_before_finalize
    if report_dir is not None:
        gateway = engine.allocator.llm_client.gateway
        fields = ("call_id", "role", "snapshot_id", "sim_time_min", "model",
                  "provider", "thinking_mode", "success", "failure_category",
                  "initial_failure_category", "validation_errors", "system_prompt_bytes",
                  "user_prompt_bytes", "input_text_bytes", "prompt_format_version",
                  "configured_max_tokens", "retry_skipped_reason",
                  "retry_remaining_seconds", "retry_minimum_seconds")
        attempt_fields = ("attempt", "max_tokens", "input_text_bytes", "timeout_seconds",
                          "elapsed_seconds", "request_elapsed_seconds", "finish_reason", "errors")
        usage_fields = ("prompt_tokens", "completion_tokens", "total_tokens")
        calls = [
            {
                **{key: call.get(key) for key in fields},
                "attempts": [
                    {
                        **{key: attempt.get(key) for key in attempt_fields},
                        "usage": {
                            key: value for key, value in (attempt.get("usage") or {}).items()
                            if key in usage_fields and isinstance(value, int)
                            and not isinstance(value, bool)
                        },
                    }
                    for attempt in call.get("attempts", ())[:3] if isinstance(attempt, dict)
                ],
            }
            for call in gateway.redact_log(gateway.call_log)
        ]
        report = {"entrypoint": "main.py", "transport": "live",
                  "requested_wall_seconds": wall_seconds, "summary": summary,
                  "model_calls": calls,
                  "events": engine.allocator.sm.get_recent_events(0.0)}
        (report_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8")
    if runtime_before_finalize == "paused_model":
        print(f"仿真提前暂停：完成 {summary['steps']}/{steps} 步，模型决策失败；详见 JSONL 日志。")
    elif runtime_before_finalize == "paused_safety":
        print(f"仿真安全暂停：完成 {summary['steps']}/{steps} 步，任务状态不一致；详见 JSONL 日志中的 mission_state_invariant_failed。")
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
        wall_seconds=args.wall_seconds,
        run_report_dir=args.run_report_dir,
    )
