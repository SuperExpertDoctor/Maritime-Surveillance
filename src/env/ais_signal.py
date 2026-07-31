"""AIS signal model used by the simulated maritime targets."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.env.ship import Ship


@dataclass(frozen=True)
class AISSignal:
    mmsi: str
    reported_position: tuple[float, float]
    reported_speed_kn: float
    reported_heading_deg: float
    ship_name: str
    ship_type: str
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "mmsi": self.mmsi,
            "reported_position": list(self.reported_position),
            "reported_speed_kn": self.reported_speed_kn,
            "reported_heading_deg": self.reported_heading_deg,
            "ship_name": self.ship_name,
            "ship_type": self.ship_type,
            "timestamp": self.timestamp,
        }


def _stable_number(value: str) -> int:
    return sum((index + 1) * ord(char) for index, char in enumerate(value))


def generate_ais_signal(ship: "Ship", timestamp: float) -> AISSignal | None:
    """Generate the signal actually broadcast by one physical ship.

    Civil vessels report their measured navigation position with a bounded
    sub-cell error.  Military vessels either remain silent or emit a position
    displaced by more than the configured two-cell classification threshold.
    The classification remains entirely position-based; the truth flag only
    selects the target's communications behaviour.
    """
    mode = getattr(ship, "ais_mode", "civilian")
    if mode == "silent":
        return None
    serial = _stable_number(ship.id)
    phase = serial * 0.017 + float(timestamp) * 0.11
    if mode == "deceptive":
        offset = 3.0 + (serial % 7) * 0.12
        reported = (
            ship.float_position[0] + offset * math.cos(phase),
            ship.float_position[1] + offset * math.sin(phase),
        )
        ship_name = f"UNKNOWN-{serial % 1000:03d}"
        vessel_type = "Cargo"
    else:
        # 0.35 cell is within the documented civilian AIS uncertainty.
        reported = (
            ship.float_position[0] + 0.35 * math.cos(phase),
            ship.float_position[1] + 0.35 * math.sin(phase),
        )
        ship_name = f"MV-CIV-{serial % 1000:03d}"
        vessel_type = "Cargo"
    return AISSignal(
        mmsi=f"{100000000 + serial % 899999999:09d}",
        reported_position=reported,
        reported_speed_kn=ship.speed_kn,
        reported_heading_deg=math.degrees(ship.base_heading) % 360.0,
        ship_name=ship_name,
        ship_type=vessel_type,
        timestamp=float(timestamp),
    )


__all__ = ["AISSignal", "generate_ais_signal"]
