"""Asynchronous, loss-aware frame publication for simulation runs.

Replay logging and live telemetry have intentionally different delivery
guarantees: every full frame is persisted for replay, while live telemetry is
conflated to keep the dashboard close to the latest simulated state.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import logging
import math
import time
from queue import Empty, Full, Queue
from threading import Condition, Event, Lock, Thread
from typing import Any

from src.vis.backend.frame_builder import build_frame
from src.vis.backend.public_details import model_calls
from src.vis.backend.server import broadcast_payload_sync

_LOGGER = logging.getLogger(__name__)


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
    model_calls: list | None = None


class FramePublisher:
    """Publish replay and live frames without blocking the simulation loop."""

    def __init__(self, logger, app=None, *, live_queue_size: int = 2):
        if isinstance(live_queue_size, bool) or not isinstance(live_queue_size, int) or live_queue_size < 1:
            raise ValueError("live_queue_size must be a positive integer")
        self.logger = logger
        self.app = app
        self._record_queue: Queue[FrameSnapshot] = Queue()
        self._live_queue: Queue[FrameSnapshot] = Queue(maxsize=live_queue_size)
        self._stop = Event()
        self._record_done = Event()
        self._live_done = Event()
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._record_count = 0
        self._record_accepted = 0
        self._record_completed = 0
        self._record_error: Exception | None = None
        self._live_accepted = 0
        self._live_completed = 0
        self._live_error: Exception | None = None
        self._broadcast_future = None
        self._last_matrix_time: float | None = None
        self._next_matrix_time = 0.0
        self._record_done.set()
        self._live_done.set()
        self._record_thread = Thread(target=self._record_loop, name="frame-recorder", daemon=True)
        self._live_thread = Thread(target=self._live_loop, name="frame-live-publisher", daemon=True)
        self._record_thread.start()
        self._live_thread.start()

    @property
    def record_count(self) -> int:
        with self._lock:
            return self._record_count

    @property
    def record_error(self) -> Exception | None:
        with self._lock:
            return self._record_error

    @property
    def live_error(self) -> Exception | None:
        with self._lock:
            return self._live_error

    def push_snapshot(self, engine, result: dict, total_steps: int) -> None:
        """Enqueue a step without serializing, disk I/O, or socket waits."""
        state = engine.allocator.sm
        is_decision_frame = result.get("trigger_type") == "heavy"
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
            llm_cycle=(
                deepcopy(result.get("llm_cycle"))
                if is_decision_frame else None
            ),
            ships=deepcopy(engine.ships),
            uavs=deepcopy(engine.uavs),
            obstacles=deepcopy(engine.obstacles),
            bases=deepcopy(engine.bases),
            model_calls=model_calls(engine, getattr(state, "episode_id", "")),
        )
        with self._condition:
            self._record_accepted += 1
            self._record_done.clear()
        self._record_queue.put_nowait(snapshot)
        if self.app is None:
            return

        # Keep the server's synchronous read model aligned with the exact
        # snapshot that the background live publisher is about to send.  In
        # particular, a normal frame clears the previous decision payload.
        app_state = getattr(self.app, "state", None)
        if app_state is not None and hasattr(app_state, "llm_cycle"):
            app_state.llm_cycle = deepcopy(snapshot.llm_cycle)

        while True:
            try:
                self._live_queue.put_nowait(snapshot)
                with self._condition:
                    self._live_accepted += 1
                    self._live_done.clear()
                    self._condition.notify_all()
                break
            except Full:
                try:
                    self._live_queue.get_nowait()
                except Empty:
                    continue
                else:
                    # The removed item was intentionally conflated, so it is
                    # complete from the live delivery contract's perspective.
                    self._live_queue.task_done()
                    with self._condition:
                        self._live_completed += 1
                        self._condition.notify_all()

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until all accepted replay frames are durable on disk."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        with self._condition:
            target = self._record_accepted
            if self._record_completed >= target:
                return self._record_error is None

        if timeout is None:
            # Queue.join() waits for task_done(), including an item already
            # removed from the queue and currently being written.
            self._record_queue.join()
            with self._condition:
                return (
                    self._record_completed >= target
                    and self._record_error is None
                )

        deadline = time.monotonic() + timeout
        with self._condition:
            while self._record_completed < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return self._record_error is None

    def wait_live_idle(self, timeout: float | None = None) -> bool:
        """Wait until live frames accepted so far have been delivered or dropped."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._condition:
                target = self._live_accepted
                future = self._broadcast_future
                if (
                    self._live_completed >= target
                    and future is None
                ):
                    return self._live_error is None
                if future is not None and future.done():
                    # A completed future may be the final live item, so no
                    # subsequent queue consumer is guaranteed to observe it.
                    pass
                else:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        wait_for = min(remaining, 0.05)
                    else:
                        wait_for = 0.05
                    self._condition.wait(wait_for)
                    continue
            self._wait_for_broadcast()

    def close(self, timeout: float | None = 10.0) -> None:
        self.flush(timeout)
        self._stop.set()
        self._record_thread.join(timeout=timeout)
        self._live_thread.join(timeout=timeout)

    def _record_loop(self) -> None:
        while True:
            try:
                snapshot = self._record_queue.get(timeout=0.05)
            except Empty:
                with self._condition:
                    if self._stop.is_set() and self._record_completed >= self._record_accepted:
                        self._record_done.set()
                        return
                continue
            try:
                self.logger.write(_build(snapshot, realtime=False, include_matrices=True))
                with self._lock:
                    self._record_count += 1
            except Exception as exc:
                with self._condition:
                    if self._record_error is None:
                        self._record_error = exc
                _LOGGER.exception("frame recording failed")
            finally:
                self._record_queue.task_done()
                with self._condition:
                    self._record_completed += 1
                    self._mark_record_idle_locked()
                    self._condition.notify_all()

    def _live_loop(self) -> None:
        while True:
            try:
                snapshot = self._live_queue.get(timeout=0.05)
            except Empty:
                with self._condition:
                    future = self._broadcast_future
                    done = (
                        self._stop.is_set()
                        and self._live_completed >= self._live_accepted
                        and future is None
                    )
                    if done:
                        self._live_done.set()
                        return
                if future is not None and future.done():
                    self._wait_for_broadcast()
                continue
            try:
                self._wait_for_broadcast()
                frame = _build(
                    snapshot,
                    realtime=True,
                    include_matrices=self._should_include_matrices(
                        snapshot.state.current_time,
                    ),
                )
                self._broadcast_future = broadcast_payload_sync(self.app, frame)
                with self._condition:
                    self._condition.notify_all()
            except Exception as exc:
                self._set_live_error(exc, "live frame publication failed")
            finally:
                self._live_queue.task_done()
                with self._condition:
                    self._live_completed += 1
                    self._mark_live_idle_locked()
                    self._condition.notify_all()

    def _mark_record_idle_locked(self) -> None:
        if self._record_completed >= self._record_accepted:
            self._record_done.set()
        else:
            self._record_done.clear()

    def _mark_live_idle_locked(self) -> None:
        if (
            self._live_completed >= self._live_accepted
            and self._broadcast_future is None
        ):
            self._live_done.set()
        else:
            self._live_done.clear()

    def _set_live_error(self, error: Exception, message: str) -> None:
        with self._condition:
            if self._live_error is None:
                self._live_error = error
            self._condition.notify_all()
        _LOGGER.exception(message)

    def _wait_for_broadcast(self) -> None:
        while True:
            with self._condition:
                future = self._broadcast_future
            if future is None:
                return
            try:
                future.result()
            except Exception as exc:
                self._set_live_error(exc, "live broadcast future failed")
            finally:
                with self._condition:
                    if self._broadcast_future is future:
                        self._broadcast_future = None
                    self._mark_live_idle_locked()
                    self._condition.notify_all()
            return

    def _should_include_matrices(self, sim_time: float) -> bool:
        """Include large matrices once per crossed five-minute sim threshold."""
        sim_time = float(sim_time)
        with self._condition:
            if sim_time < self._next_matrix_time:
                return False
            self._last_matrix_time = sim_time
            crossed = sim_time - self._next_matrix_time
            self._next_matrix_time += (math.floor(crossed / 5.0) + 1) * 5.0
            return True


def _build(snapshot: FrameSnapshot, *, realtime: bool, include_matrices: bool) -> dict:
    return build_frame(
        snapshot.state,
        snapshot.cycle,
        snapshot.config,
        total_steps=snapshot.total_steps,
        llm_cycle=snapshot.llm_cycle,
        model_calls=snapshot.model_calls,
        event_history_limit=300,
        ships=snapshot.ships,
        uav_entities=snapshot.uavs,
        obstacles=snapshot.obstacles,
        bases=snapshot.bases,
        realtime=realtime,
        include_matrices=include_matrices,
    )


__all__ = ["FramePublisher", "FrameSnapshot"]
