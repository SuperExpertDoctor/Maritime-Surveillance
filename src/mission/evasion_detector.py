"""Observation-only AIS evasive-maneuver detection.

The detector deliberately emits facts instead of mutating information state.
AIS packets and the assigned UAV history are the only inputs used here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Iterable

from src.mission.config import EvasionConfig
from src.mission.contracts import EvasiveManeuverFact


@dataclass
class _TrackState:
    candidate_hits: int = 0
    episode_id: str | None = None
    clear_since_min: float | None = None
    last_evaluated_min: float = -math.inf


def _get(item, *names, default=None):
    if isinstance(item, dict):
        for name in names:
            if name in item:
                return item[name]
        return default
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _ais_point(item):
    if isinstance(item, (tuple, list)) and len(item) >= 3:
        timestamp, x, y = item[:3]
        if isinstance(x, (tuple, list)) and len(x) == 2:
            return float(timestamp), (float(x[0]), float(x[1])), y
        return float(timestamp), (float(x), float(y)), None
    timestamp = _get(item, "timestamp", "observed_at_min", "time")
    position = _get(item, "reported_position", "position_cells", "position")
    if timestamp is None or position is None or len(position) != 2:
        return None
    mmsi = _get(item, "mmsi", "source_id", default=None)
    return float(timestamp), (float(position[0]), float(position[1])), mmsi


def _observer_point(item):
    if isinstance(item, (tuple, list)):
        if len(item) >= 4 and isinstance(item[0], str):
            uav_id, timestamp, x, y = item[:4]
            return str(uav_id), float(timestamp), (float(x), float(y)), "track"
        if len(item) >= 4:
            timestamp, x, y, uav_id = item[:4]
            return str(uav_id), float(timestamp), (float(x), float(y)), "track"
    uav_id = _get(item, "uav_id", "observer_uav_id", "source_id")
    timestamp = _get(item, "observed_at_min", "timestamp", "time")
    position = _get(item, "position_cells", "observer_position_cells", "position")
    operation = _get(item, "operation", "status", "task_type", default="track")
    if uav_id is None or timestamp is None or position is None or len(position) != 2:
        return None
    return str(uav_id), float(timestamp), (float(position[0]), float(position[1])), str(operation)


def _fit_velocity(points: list[tuple[float, tuple[float, float]]]):
    if len(points) < 2:
        return None
    t0 = points[0][0]
    times = [t - t0 for t, _ in points]
    mean_t = sum(times) / len(times)
    denominator = sum((t - mean_t) ** 2 for t in times)
    if denominator <= 1e-12:
        return None
    mean_x = sum(position[0] for _, position in points) / len(points)
    mean_y = sum(position[1] for _, position in points) / len(points)
    vx = sum((t - mean_t) * (position[0] - mean_x)
             for t, position in zip(times, (p for _, p in points))) / denominator
    vy = sum((t - mean_t) * (position[1] - mean_y)
             for t, position in zip(times, (p for _, p in points))) / denominator
    return vx, vy


def _speed_kn(velocity, cell_size_km: float) -> float:
    return math.hypot(*velocity) * cell_size_km * 60.0 / 1.852


def _angle_deg(left, right) -> float:
    left_norm = math.hypot(*left)
    right_norm = math.hypot(*right)
    if left_norm <= 1e-12 or right_norm <= 1e-12:
        return 0.0
    cosine = max(-1.0, min(1.0, (left[0] * right[0] + left[1] * right[1])
                              / left_norm / right_norm))
    return math.degrees(math.acos(cosine))


def _position_at(points, at_min: float):
    before = [item for item in points if item[0] <= at_min]
    if before:
        return before[-1][1]
    return points[0][1] if points else None


def _distance_to_boundary(position, boundary):
    if boundary is None:
        return math.inf
    x0, y0, x1, y1 = map(float, boundary)
    x, y = position
    return min(x - x0, x1 - x, y - y0, y1 - y)


def _distance_to_obstacle(position, obstacle):
    if hasattr(obstacle, "contains") and obstacle.contains(position):
        return 0.0
    center = _get(obstacle, "center", default=None)
    half_extent = _get(obstacle, "half_extent", "half_extent_cells", default=None)
    if center is None or half_extent is None:
        if isinstance(obstacle, (tuple, list)) and len(obstacle) == 3:
            center, half_extent = obstacle[:2], obstacle[2]
        else:
            return math.inf
    dx = max(abs(float(position[0]) - float(center[0])) - float(half_extent), 0.0)
    dy = max(abs(float(position[1]) - float(center[1])) - float(half_extent), 0.0)
    return math.hypot(dx, dy)


class EvasionDetector:
    """Maintain one clear/candidate/confirmed episode per AIS MMSI."""

    def __init__(self, config: EvasionConfig, *, cell_size_km: float):
        if not isinstance(config, EvasionConfig):
            raise TypeError("config must be EvasionConfig")
        if not math.isfinite(cell_size_km) or cell_size_km <= 0:
            raise ValueError("cell_size_km must be positive and finite")
        self.config = config
        self.cell_size_km = float(cell_size_km)
        self._states: dict[str, _TrackState] = {}

    def evaluate(
        self,
        ais_track: Iterable[object],
        observer_history: Iterable[object],
        *,
        now_min: float | None = None,
        mission_boundary=None,
        obstacles=(),
        contact_id: str | None = None,
    ) -> tuple[EvasiveManeuverFact, ...]:
        if not self.config.enabled:
            return ()
        points_with_ids = [item for item in (_ais_point(raw) for raw in ais_track)
                           if item is not None]
        if not points_with_ids:
            return ()
        points_with_ids.sort(key=lambda item: item[0])
        mmsi = next((item[2] for item in points_with_ids if item[2]), None) or "unknown-mmsi"
        points = [(timestamp, position) for timestamp, position, _ in points_with_ids]
        # A delayed evaluation must not move the AIS windows beyond the last
        # received packet. The next packet, not wall-clock polling, advances
        # the observed trajectory. The separate evaluation clock still drives
        # the five-minute rearm timer.
        evaluation_now = float(now_min if now_min is not None else points[-1][0])
        now = min(evaluation_now, points[-1][0])
        state = self._states.setdefault(str(mmsi), _TrackState())

        observer = self._closest_observer(observer_history, points[-1][1], now)
        condition = False
        details = None
        if observer is not None:
            observer_id, observer_position = observer
            details = self._condition(points, observer_position, now, mission_boundary, obstacles)
            condition = details is not None
        if not condition:
            if state.episode_id is not None and state.clear_since_min is None:
                state.clear_since_min = evaluation_now
            if (state.clear_since_min is not None
                    and evaluation_now - state.clear_since_min >= self.config.rearm_clear_min):
                state.episode_id = None
                state.candidate_hits = 0
            state.last_evaluated_min = evaluation_now
            return ()

        if (state.clear_since_min is not None
                and evaluation_now - state.clear_since_min >= self.config.rearm_clear_min):
            state.episode_id = None
            state.candidate_hits = 0
        state.clear_since_min = None
        state.candidate_hits += 1
        state.last_evaluated_min = evaluation_now
        if state.candidate_hits < self.config.confirmation_samples:
            return ()
        if state.episode_id is None:
            digest = hashlib.sha256(f"{mmsi}:{now:.9f}".encode("ascii")).hexdigest()[:16]
            state.episode_id = f"evasion-{digest}"
            episode_started = True
        else:
            episode_started = False
        position, covariance = details
        fact_id = hashlib.sha256(
            f"{state.episode_id}:{now:.9f}".encode("ascii")
        ).hexdigest()[:20]
        return (EvasiveManeuverFact(
            fact_id=fact_id,
            evasion_episode_id=state.episode_id,
            episode_started=episode_started,
            mmsi=str(mmsi),
            contact_id=contact_id or str(mmsi),
            observed_at_min=now,
            position_cells=position,
            covariance_cells2=covariance,
        ),)

    def _closest_observer(self, history, ship_position, now):
        latest: dict[str, tuple[float, tuple[float, float], str]] = {}
        for raw in history:
            parsed = _observer_point(raw)
            if parsed is None:
                continue
            uav_id, timestamp, position, operation = parsed
            if timestamp > now or operation.lower() not in {
                "probe", "track", "tracking", "observing", "eo",
            }:
                continue
            previous = latest.get(uav_id)
            if previous is None or timestamp > previous[0]:
                latest[uav_id] = (timestamp, position, operation)
        candidates = [
            (math.dist(ship_position, item[1]), uav_id, item[1])
            for uav_id, item in latest.items()
            if math.dist(ship_position, item[1]) <= self.config.observer_range_cells
        ]
        if not candidates:
            return None
        _, uav_id, position = min(candidates, key=lambda item: (item[0], item[1]))
        return uav_id, position

    def _condition(self, points, observer_position, now, mission_boundary, obstacles):
        pre = [item for item in points if now - self.config.history_window_min
               <= item[0] < now - self.config.response_window_min]
        post = [item for item in points if now - self.config.response_window_min
                <= item[0] <= now]
        minimum = self.config.minimum_samples_per_window
        if (len({item[0] for item in pre}) < minimum
                or len({item[0] for item in post}) < minimum):
            return None
        if any(window[-1][0] - window[0][0] < self.config.minimum_window_span_min
               for window in (pre, post)):
            return None
        v_pre = _fit_velocity(pre)
        v_post = _fit_velocity(post)
        if v_pre is None or v_post is None:
            return None
        current_position = post[-1][1]
        if (_distance_to_boundary(current_position, mission_boundary)
                < self.config.forced_maneuver_exclusion_cells):
            return None
        if any(_distance_to_obstacle(current_position, obstacle)
               < self.config.forced_maneuver_exclusion_cells for obstacle in obstacles):
            return None
        speed_pre = _speed_kn(v_pre, self.cell_size_km)
        speed_post = _speed_kn(v_post, self.cell_size_km)
        turn_or_speed = (
            _angle_deg(v_pre, v_post) >= self.config.minimum_course_change_deg
            or speed_post - speed_pre >= self.config.minimum_speed_increase_kn
        )
        direction = (
            current_position[0] - observer_position[0],
            current_position[1] - observer_position[1],
        )
        distance = math.hypot(*direction)
        if distance <= 1e-12:
            return None
        outward = (v_post[0] * direction[0] + v_post[1] * direction[1]) / distance
        outward_kn = outward * self.cell_size_km * 60.0 / 1.852
        previous_position = _position_at(points, now - self.config.response_window_min)
        if previous_position is None:
            return None
        range_increase = math.dist(current_position, observer_position) - math.dist(
            previous_position, observer_position)
        if not (turn_or_speed and outward_kn >= self.config.minimum_outward_speed_kn
                and range_increase >= self.config.minimum_range_increase_cells):
            return None
        residuals = []
        for timestamp, position in (*pre, *post):
            predicted = (
                current_position[0] + v_post[0] * (timestamp - now),
                current_position[1] + v_post[1] * (timestamp - now),
            )
            residuals.append(math.dist(position, predicted) ** 2)
        variance = max(sum(residuals) / max(1, len(residuals) - 4), 1e-4)
        covariance = ((variance, 0.0), (0.0, variance))
        return current_position, covariance


__all__ = ["EvasionDetector"]
