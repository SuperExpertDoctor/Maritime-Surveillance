"""Shortest Dubins paths for the fixed-wing vehicle model.

The public coordinate system follows the visualiser: ``x``/column grows to
the right, ``y``/row grows downwards, and a positive heading turns clockwise.
Internally the solver mirrors ``y`` so the standard six Dubins families can
be used without changing their well-known formulae.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Iterator, Sequence


Pose = tuple[float, float, float]


def _mod2pi(angle: float) -> float:
    return angle % (2.0 * math.pi)


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


@dataclass(frozen=True)
class DubinsResult:
    """Computed path and its sampled poses.

    ``segments`` stores normalised lengths: arc values are radians and a
    straight value is distance divided by the turning radius.  Iteration is
    supported for compatibility with the tuple-shaped API in GOAL.md.
    """

    path_type: str
    total_length: float
    waypoints: list[Pose]
    segments: tuple[float, float, float]

    def __iter__(self) -> Iterator[object]:
        yield self.path_type
        yield self.total_length
        yield self.waypoints


class DubinsPath:
    """Compute the globally shortest path across all six Dubins families."""

    _WORDS = ("LSL", "LSR", "RSL", "RSR", "LRL", "RLR")

    @classmethod
    def compute(
        cls,
        start_pose: Sequence[float],
        end_pose: Sequence[float],
        R_min: float,
        step_size: float | None = None,
    ) -> DubinsResult:
        if R_min <= 0 or not math.isfinite(R_min):
            raise ValueError("R_min must be a finite positive number")
        if len(start_pose) != 3 or len(end_pose) != 3:
            raise ValueError("poses must be (x, y, heading) triples")

        sx, sy_screen, syaw_screen = map(float, start_pose)
        gx, gy_screen, gyaw_screen = map(float, end_pose)
        values = (sx, sy_screen, syaw_screen, gx, gy_screen, gyaw_screen)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("pose values must be finite")

        # Mirror the visualiser's downward y axis to the mathematical plane.
        sy, gy = -sy_screen, -gy_screen
        syaw, gyaw = -syaw_screen, -gyaw_screen
        dx, dy = gx - sx, gy - sy
        distance = math.hypot(dx, dy)

        # Identical poses are a valid zero-length path.
        if distance <= 1e-12 and abs(_wrap_pi(gyaw - syaw)) <= 1e-12:
            pose = (sx, sy_screen, _wrap_pi(syaw_screen))
            return DubinsResult("LSL", 0.0, [pose], (0.0, 0.0, 0.0))

        d = distance / R_min
        theta = _mod2pi(math.atan2(dy, dx)) if distance > 0 else 0.0
        alpha = _mod2pi(syaw - theta)
        beta = _mod2pi(gyaw - theta)

        candidates: list[tuple[float, str, tuple[float, float, float]]] = []
        for word in cls._WORDS:
            params = getattr(cls, f"_{word.lower()}")(alpha, beta, d)
            if params is not None:
                candidates.append((sum(params), word, params))
        if not candidates:
            raise RuntimeError("no feasible Dubins path found")

        normalised_length, word, segments = min(candidates, key=lambda item: item[0])
        sample_step = step_size if step_size is not None else min(0.25, R_min / 4.0)
        if sample_step <= 0:
            raise ValueError("step_size must be positive")
        sampled_math = cls._sample((sx, sy, syaw), word, segments, R_min, sample_step)

        # Force the requested terminal pose to eliminate accumulated floating
        # point drift while retaining all intermediate curvature samples.
        sampled_math[-1] = (gx, gy, _wrap_pi(gyaw))
        waypoints = [(x, -y, _wrap_pi(-yaw)) for x, y, yaw in sampled_math]
        return DubinsResult(word, normalised_length * R_min, waypoints, segments)

    @staticmethod
    def _lsl(a: float, b: float, d: float):
        p2 = 2.0 + d * d - 2.0 * math.cos(a - b) + 2.0 * d * (math.sin(a) - math.sin(b))
        if p2 < -1e-12:
            return None
        tmp = math.atan2(math.cos(b) - math.cos(a), d + math.sin(a) - math.sin(b))
        return _mod2pi(-a + tmp), math.sqrt(max(0.0, p2)), _mod2pi(b - tmp)

    @staticmethod
    def _rsr(a: float, b: float, d: float):
        p2 = 2.0 + d * d - 2.0 * math.cos(a - b) + 2.0 * d * (-math.sin(a) + math.sin(b))
        if p2 < -1e-12:
            return None
        tmp = math.atan2(math.cos(a) - math.cos(b), d - math.sin(a) + math.sin(b))
        return _mod2pi(a - tmp), math.sqrt(max(0.0, p2)), _mod2pi(-b + tmp)

    @staticmethod
    def _lsr(a: float, b: float, d: float):
        p2 = -2.0 + d * d + 2.0 * math.cos(a - b) + 2.0 * d * (math.sin(a) + math.sin(b))
        if p2 < -1e-12:
            return None
        p = math.sqrt(max(0.0, p2))
        tmp = math.atan2(-math.cos(a) - math.cos(b), d + math.sin(a) + math.sin(b)) - math.atan2(-2.0, p)
        return _mod2pi(-a + tmp), p, _mod2pi(-b + tmp)

    @staticmethod
    def _rsl(a: float, b: float, d: float):
        p2 = d * d - 2.0 + 2.0 * math.cos(a - b) - 2.0 * d * (math.sin(a) + math.sin(b))
        if p2 < -1e-12:
            return None
        p = math.sqrt(max(0.0, p2))
        tmp = math.atan2(math.cos(a) + math.cos(b), d - math.sin(a) - math.sin(b)) - math.atan2(2.0, p)
        return _mod2pi(a - tmp), p, _mod2pi(b - tmp)

    @staticmethod
    def _rlr(a: float, b: float, d: float):
        value = (6.0 - d * d + 2.0 * math.cos(a - b) + 2.0 * d * (math.sin(a) - math.sin(b))) / 8.0
        if abs(value) > 1.0 + 1e-12:
            return None
        p = _mod2pi(2.0 * math.pi - math.acos(max(-1.0, min(1.0, value))))
        t = _mod2pi(a - math.atan2(math.cos(a) - math.cos(b), d - math.sin(a) + math.sin(b)) + p / 2.0)
        return t, p, _mod2pi(a - b - t + p)

    @staticmethod
    def _lrl(a: float, b: float, d: float):
        value = (6.0 - d * d + 2.0 * math.cos(a - b) + 2.0 * d * (-math.sin(a) + math.sin(b))) / 8.0
        if abs(value) > 1.0 + 1e-12:
            return None
        p = _mod2pi(2.0 * math.pi - math.acos(max(-1.0, min(1.0, value))))
        t = _mod2pi(-a - math.atan2(math.cos(a) - math.cos(b), d + math.sin(a) - math.sin(b)) + p / 2.0)
        return t, p, _mod2pi(b - a - t + p)

    @classmethod
    def _sample(
        cls,
        start: Pose,
        word: str,
        segments: Iterable[float],
        radius: float,
        step_size: float,
    ) -> list[Pose]:
        x, y, yaw = start
        points: list[Pose] = [(x, y, _wrap_pi(yaw))]
        for mode, normalised in zip(word, segments):
            segment_distance = normalised * radius
            steps = max(1, int(math.ceil(segment_distance / step_size)))
            sx, sy, syaw = x, y, yaw
            for index in range(1, steps + 1):
                fraction = index / steps
                amount = normalised * fraction
                if mode == "S":
                    dist = amount * radius
                    nx = sx + dist * math.cos(syaw)
                    ny = sy + dist * math.sin(syaw)
                    nyaw = syaw
                elif mode == "L":
                    nyaw = syaw + amount
                    nx = sx + radius * (math.sin(nyaw) - math.sin(syaw))
                    ny = sy + radius * (-math.cos(nyaw) + math.cos(syaw))
                else:  # R
                    nyaw = syaw - amount
                    nx = sx + radius * (-math.sin(nyaw) + math.sin(syaw))
                    ny = sy + radius * (math.cos(nyaw) - math.cos(syaw))
                points.append((nx, ny, _wrap_pi(nyaw)))
            x, y, yaw = points[-1]
        return points


__all__ = ["DubinsPath", "DubinsResult", "Pose"]
