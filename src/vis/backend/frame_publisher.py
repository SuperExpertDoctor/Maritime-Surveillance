"""Asynchronous, loss-aware frame publication for simulation runs.

Replay logging and live telemetry have intentionally different delivery
guarantees: every full frame is persisted for replay, while live telemetry is
conflated to keep the dashboard close to the latest simulated state.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any

from src.vis.backend.frame_builder import build_frame
from src.vis.backend.server import broadcast_payload_sync


@dataclass(frozen=True)
class FrameSnapshot:
    """Immutable state captured at a simulation step for background work."""

    state: Any
    cycle: int
    config: Any
    total_steps: int
    llm_cycle: dict | None
    ships: list
    uavs: list
    obstacles: list
    bases: list


class FramePublisher:
    """Publish replay and live frames without blocking the simulation loop."""

    def __init__(self, logger, app=None, *, live_queue_size: int = 2):
        self.logger = logger
        self.app = app
        self._record_queue: Queue[FrameSnapshot] = Queue()
        self._live_queue: Queue[FrameSnapshot] = Queue(maxsize=live_queue_size)
        self._stop = Event()
        self._record_done = Event()
        self._live_done = Event()
        self._lock = Lock()
        self._record_count = 0
        self._broadcast_future = None
        self._record_thread = Thread(target=self._record_loop, name="frame-recorder", daemon=True)
        self._live_thread = Thread(target=self._live_loop, name="frame-live-publisher", daemon=True)
        self._record_thread.start()
        self._live_thread.start()

    @property
    def record_count(self) -> int:
        with self._lock:
            return self._record_count

    def push_snapshot(self, engine, result: dict, total_steps: int) -> None:
        """Enqueue a step without serializing, disk I/O, or socket waits."""
        state = engine.allocator.sm
        snapshot = FrameSnapshot(
            # The writer must retain every historical frame.  References alone
            # would be mutated by later simulation steps before the recorder
            # consumes them, so take an in-memory copy but defer all frame
            # construction, JSON encoding, I/O, and network work.
            state=deepcopy(state),
            cycle=state.cycle,
            config=engine.config,
            total_steps=total_steps,
            # The UI remembers the latest successful decision.  Persist the
            # bulky LLM payload only on the decision frame itself.
            llm_cycle=result.get("llm_cycle"),
            ships=deepcopy(engine.ships),
            uavs=deepcopy(engine.uavs),
            obstacles=deepcopy(engine.obstacles),
            bases=deepcopy(engine.bases),
        )
        self._record_done.clear()
        self._record_queue.put_nowait(snapshot)
        if self.app is None:
            return
        self._live_done.clear()
        try:
            self._live_queue.put_nowait(snapshot)
        except Full:
            try:
                self._live_queue.get_nowait()
            except Empty:
                pass
            self._live_queue.put_nowait(snapshot)

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until all accepted replay frames are durable on disk."""
        if self._record_queue.empty():
            self._record_done.set()
        return self._record_done.wait(timeout)

    def close(self, timeout: float | None = 10.0) -> None:
        self.flush(timeout)
        self._stop.set()
        self._record_thread.join(timeout=timeout)
        self._live_thread.join(timeout=timeout)

    def _record_loop(self) -> None:
        while not self._stop.is_set() or not self._record_queue.empty():
            try:
                snapshot = self._record_queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                self.logger.write(_build(snapshot, realtime=False, include_matrices=True))
                with self._lock:
                    self._record_count += 1
            finally:
                self._record_queue.task_done()
                if self._record_queue.empty():
                    self._record_done.set()

    def _live_loop(self) -> None:
        while not self._stop.is_set() or not self._live_queue.empty():
            try:
                snapshot = self._live_queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                if self._broadcast_future is not None and not self._broadcast_future.done():
                    continue
                frame = _build(
                    snapshot,
                    realtime=True,
                    include_matrices=snapshot.state.current_time % 5 == 0,
                )
                self._broadcast_future = broadcast_payload_sync(self.app, frame)
            finally:
                self._live_queue.task_done()
                if self._live_queue.empty():
                    self._live_done.set()


def _build(snapshot: FrameSnapshot, *, realtime: bool, include_matrices: bool) -> dict:
    return build_frame(
        snapshot.state,
        snapshot.cycle,
        snapshot.config,
        total_steps=snapshot.total_steps,
        llm_cycle=snapshot.llm_cycle,
        ships=snapshot.ships,
        uav_entities=snapshot.uavs,
        obstacles=snapshot.obstacles,
        bases=snapshot.bases,
        realtime=realtime,
        include_matrices=include_matrices,
    )


__all__ = ["FramePublisher", "FrameSnapshot"]
