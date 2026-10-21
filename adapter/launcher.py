"""PI worker launcher: spawn and supervise the vendored coding-agent worker."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_RESOLVER = "./harness/packages/coding-agent/src/experimental/source-resolver.ts"
WORKER_ENTRY = "harness/agent/worker.ts"
_LOGGER = logging.getLogger(__name__)


def spawn_pi_worker(
    *,
    port: int,
    runtime_dir: str | os.PathLike[str],
    worker_token: str,
    log_path: str | os.PathLike[str] | None = None,
    node_bin: str | None = None,
) -> subprocess.Popen:
    """Start ``harness/agent/worker.ts`` against the local adapter API.

    The worker reads ADAPTER_BASE_URL / ADAPTER_WORKER_TOKEN /
    ADAPTER_PI_RUNTIME_DIR / LONGCAT_API_KEY from its environment; the
    parent's os.environ (with configs/.env and .env already loaded) is
    inherited so the LongCat key never appears on the command line.
    """
    node = node_bin or shutil.which("node")
    if node is None:
        raise RuntimeError("node runtime not found on PATH")
    runtime = Path(runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["ADAPTER_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["ADAPTER_WORKER_TOKEN"] = worker_token
    env["ADAPTER_PI_RUNTIME_DIR"] = str(runtime)
    if log_path is not None:
        log = open(log_path, "a", encoding="utf-8")
        stdout, stderr = log, subprocess.STDOUT
    else:
        stdout = stderr = subprocess.DEVNULL
    return subprocess.Popen(
        [node, "--import", SOURCE_RESOLVER, WORKER_ENTRY],
        cwd=REPO_ROOT,
        env=env,
        stdout=stdout,
        stderr=stderr,
    )


class PiWorkerSupervisor:
    """Restart the PI worker after a crash.

    The worker's session persists under runtime_dir (pi-coding-agent
    ``session.jsonl``), so a respawned worker resumes its conversation
    instead of starting blank. Exponential backoff caps restart churn.
    """

    def __init__(
        self,
        *,
        port: int,
        runtime_dir: str | os.PathLike[str],
        worker_token: str,
        log_path: str | os.PathLike[str] | None = None,
        max_restarts: int = 20,
    ) -> None:
        self._kwargs = {
            "port": port,
            "runtime_dir": runtime_dir,
            "worker_token": worker_token,
            "log_path": log_path,
        }
        self.max_restarts = max_restarts
        self.process: subprocess.Popen | None = None
        self.restarts = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.process = spawn_pi_worker(**self._kwargs)
        self._thread = threading.Thread(
            target=self._watch, name="pi-worker-supervisor", daemon=True
        )
        self._thread.start()

    def _watch(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            proc = self.process
            if proc is None:
                return
            code = proc.wait()
            if self._stop.is_set():
                return
            if self.restarts >= self.max_restarts:
                _LOGGER.error("pi worker exited (code %s); restart budget exhausted", code)
                return
            self.restarts += 1
            _LOGGER.warning(
                "pi worker exited (code %s); restart %d in %.0fs",
                code, self.restarts, backoff,
            )
            self._stop.wait(backoff)
            if self._stop.is_set():
                return
            try:
                self.process = spawn_pi_worker(**self._kwargs)
                backoff = min(backoff * 2, 60.0)
            except Exception:
                _LOGGER.exception("pi worker respawn failed")
                backoff = min(backoff * 2, 60.0)

    def stop(self, timeout: float = 8.0) -> None:
        self._stop.set()
        proc = self.process
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
