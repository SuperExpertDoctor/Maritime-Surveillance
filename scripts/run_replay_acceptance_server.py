"""Serve existing replay artifacts for deterministic browser acceptance tests."""
from __future__ import annotations

import argparse
from pathlib import Path
import socket

import uvicorn

from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend import server


def _require_replay_directory(value: str) -> Path:
    output_dir = Path(value).expanduser().resolve()
    if not output_dir.is_dir():
        raise ValueError(f"replay output directory does not exist: {output_dir}")
    if not any(output_dir.glob("*.jsonl")):
        raise ValueError(f"replay output directory contains no JSONL artifacts: {output_dir}")
    return output_dir


def _require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"acceptance server port {port} is already in use") from exc


def create_replay_app(output_dir: str | Path, config_path: str = "configs"):
    """Create a read-only app backed by the supplied, already-written artifacts."""
    resolved_output_dir = _require_replay_directory(str(output_dir))
    config = ConfigLoader.load(config_path)
    state_manager = StateManager(config)
    server.OUTPUT_DIR = str(resolved_output_dir)
    return server.create_app(
        config,
        state_manager,
        replay_mode=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--port", type=int, default=18866)
    parser.add_argument("--config", default="configs")
    args = parser.parse_args()
    try:
        output_dir = _require_replay_directory(args.output_dir)
        _require_free_port(args.port)
        app = create_replay_app(output_dir, args.config)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
