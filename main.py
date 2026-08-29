"""CLI entry point for the UAV maritime surveillance simulation."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
import shutil
import threading
import time

import uvicorn

from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend.frame_publisher import FramePublisher
from src.vis.backend.server import create_app


def _free_port(port: int) -> None:
    """Kill any process currently bound to *port* so the server can start.

    On Windows the check covers ``python.exe`` only; on POSIX it targets any
    process that matches the listening socket.
    """
    if sys.platform == "win32":
        try:
            raw = subprocess.check_output(
                ["netstat", "-ano"], text=True, timeout=5
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return
        for line in raw.splitlines():
            if f":{port}" not in line or "LISTENING" not in line:
                continue
            parts = line.strip().split()
            pid = parts[-1]
            if not pid.isdigit():
                continue
            # Only kill python processes — never touch system services
            try:
                info = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                    text=True, timeout=5,
                )
                if "python.exe" not in info.lower() and "python" not in info.lower():
                    print(
                        f"Port {port} held by non-Python PID {pid}, refusing to kill"
                    )
                    continue
            except subprocess.CalledProcessError:
                continue
            print(f"Port {port} occupied by PID {pid} (python.exe) — killing...")
            try:
                subprocess.check_call(
                    ["taskkill", "/PID", pid, "/F"],
                    timeout=10,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"  -> PID {pid} terminated, port {port} released.")
            except subprocess.CalledProcessError as exc:
                print(f"  -> Failed to kill PID {pid}: {exc}")
        return

    # POSIX (Linux / macOS)
    try:
        raw = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], text=True, timeout=5
        )
        pids = [pid.strip() for pid in raw.splitlines() if pid.strip().isdigit()]
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        return

    for pid_str in pids:
        pid = int(pid_str)
        # Never kill our own process
        if pid == os.getpid():
            continue
        print(f"Port {port} occupied by PID {pid} — killing...")
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"  -> PID {pid} terminated, port {port} released.")
        except OSError as exc:
            print(f"  -> Failed to kill PID {pid}: {exc}")


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
) -> dict:
    config = ConfigLoader.load(config_path)
    if config.common.clear_outputs_before_run:
        removed = clear_output_cache()
        print(f"Cleared {removed} cached output item(s)")
    engine = SimulationEngine(config)
    if probe_llm:
        engine.allocator.llm_client.probe(llm_probe_timeout)
        print("LongCat-2.0 connectivity probe passed")
    app = None
    logger = None

    if start_server:
        _free_port(port)
        app = create_app(config, engine.allocator.sm)
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

    summary = engine.run(steps, on_step=publish)
    if not frame_publisher.flush(timeout=120):
        frame_publisher.close()
        raise RuntimeError("timed out while flushing replay frames")
    frame_publisher.close()
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
        probe_llm=not args.skip_llm_probe,
        llm_probe_timeout=args.llm_probe_timeout,
    )
