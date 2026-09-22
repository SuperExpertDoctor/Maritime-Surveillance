"""Red-only threat gating and fleet decisions; never a blue observation source."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
from pathlib import Path

from src.mission.contracts import (
    RedMotionParameters,
    RedPlan,
    Vec2,
    VesselClass,
)
from src.mission.llm_gateway import LLMGateway
from src.mission.surveillance_stage import SurveillanceStage
from src.schedule.config_loader import ShipConfig


def swept_min_distance_cells(
    ship_start: Vec2, ship_end: Vec2, uav_start: Vec2, uav_end: Vec2,
) -> float:
    """Minimum simultaneous separation over two linear motion segments."""
    r0 = tuple(ship_start[i] - uav_start[i] for i in range(2))
    dr = tuple(
        (ship_end[i] - ship_start[i]) - (uav_end[i] - uav_start[i])
        for i in range(2)
    )
    squared_speed = sum(value * value for value in dr)
    s = (
        max(0.0, min(1.0, -sum(r0[i] * dr[i] for i in range(2)) / squared_speed))
        if squared_speed else 0.0
    )
    return math.hypot(*(r0[i] + s * dr[i] for i in range(2)))


@dataclass
class _GateState:
    state: str = "normal"
    clear_since_min: float | None = None


class ThreatGate:
    """Distance hysteresis, independent of tracking and all model calls.

    Feed the minimum over all UAVs once per observation (infinity if none).
    Substeps feed swept minima; the next main frame reads the retained state.
    """

    def __init__(self, config: ShipConfig):
        self.config = config
        self._ships: dict[str, _GateState] = {}
        self._episode_revision = 0

    @property
    def episode_revision(self) -> int:
        """Monotonic handoff for episode boundaries between commander frames."""
        return self._episode_revision

    def update(
        self, ship_id: str, vessel_class: VesselClass,
        min_distance_cells: float, now_min: float,
    ) -> str:
        gate = self._ships.setdefault(ship_id, _GateState())
        previous_state = gate.state
        if vessel_class != "type_ii":
            gate.state, gate.clear_since_min = "normal", None
        elif gate.state == "normal":
            if min_distance_cells < self.config.detect_uav_radius_cells:
                gate.state = "evasive"
        elif min_distance_cells > self.config.clear_uav_radius_cells:
            if gate.clear_since_min is None:
                gate.clear_since_min = now_min
                gate.state = "recovering"
            if now_min - gate.clear_since_min >= self.config.clear_hold_min:
                gate.state, gate.clear_since_min = "normal", None
        else:
            gate.state, gate.clear_since_min = "evasive", None
        if (
            (previous_state == "normal" and gate.state == "evasive")
            or (previous_state == "recovering" and gate.state == "normal")
        ):
            self._episode_revision += 1
        return gate.state

    def observe_swept_distance(
        self, ship_id: str, vessel_class: VesselClass,
        min_distance_cells: float, now_min: float,
    ) -> None:
        self.update(ship_id, vessel_class, min_distance_cells, now_min)

    def remove_ship(self, ship_id: str) -> None:
        """Forget runtime gate state for a vessel removed from the episode."""
        self._ships.pop(ship_id, None)


@dataclass(frozen=True)
class RedShipSnapshot:
    ship_id: str
    vessel_class: VesselClass
    surveillance_stage: SurveillanceStage
    position_cells: Vec2
    heading_deg: float
    speed_kn: float
    normal_tangent_deg: float
    ais_enabled: bool


@dataclass(frozen=True)
class RedSnapshot:
    snapshot_id: str
    sim_time_min: float
    ships: tuple[RedShipSnapshot, ...]
    uavs: tuple[tuple[str, Vec2, Vec2], ...]
    active_signature: tuple[tuple[str, SurveillanceStage], ...]
    land_mask_version: int


@dataclass(frozen=True)
class RedPlanInstallation:
    """Red execution handoff: command phase_deg is the phase at installed_at_min."""

    plan: RedPlan
    installed_at_min: float
    active_signature: tuple[tuple[str, SurveillanceStage], ...]

    @property
    def expires_at_min(self) -> float:
        return self.installed_at_min + self.plan.valid_for_min


class RedDecisionBlocked(RuntimeError):
    """No usable model plan: the simulation owner must pause before motion."""


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _vec2(value: object) -> bool:
    return isinstance(value, tuple) and len(value) == 2 and all(_finite_number(v) for v in value)


def _validate_snapshot(snapshot: RedSnapshot) -> None:
    """Check the authority set before either model access or cached delivery."""
    if (
        not isinstance(snapshot, RedSnapshot) or not _identifier(snapshot.snapshot_id)
        or not _finite_number(snapshot.sim_time_min) or snapshot.sim_time_min < 0
        or type(snapshot.land_mask_version) is not int or snapshot.land_mask_version < 0
        or not isinstance(snapshot.ships, tuple) or not isinstance(snapshot.uavs, tuple)
        or not isinstance(snapshot.active_signature, tuple)
    ):
        raise RedDecisionBlocked("invalid red snapshot metadata")
    ship_ids = []
    eligible = []
    for ship in snapshot.ships:
        if (
            not isinstance(ship, RedShipSnapshot) or not _identifier(ship.ship_id)
            or ship.vessel_class not in ("type_i", "type_ii")
            or ship.surveillance_stage not in (
                "undetected", "detected", "probing", "tracking",
            )
            or not _vec2(ship.position_cells) or not _finite_number(ship.heading_deg)
            or not _finite_number(ship.normal_tangent_deg)
            or not _finite_number(ship.speed_kn) or ship.speed_kn < 0
            or type(ship.ais_enabled) is not bool
            or (
                ship.vessel_class == "type_i"
                and ship.surveillance_stage != "undetected"
            )
        ):
            raise RedDecisionBlocked("invalid red snapshot ship")
        ship_ids.append(ship.ship_id)
        if (
            ship.vessel_class == "type_ii"
            and ship.surveillance_stage in ("detected", "probing", "tracking")
        ):
            eligible.append((ship.ship_id, ship.surveillance_stage))
    signature = []
    for item in snapshot.active_signature:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not _identifier(item[0])
            or item[1] not in ("detected", "probing", "tracking")
        ):
            raise RedDecisionBlocked("invalid red active_signature")
        signature.append(item)
    expected_signature = tuple(sorted(eligible))
    if (
        len(ship_ids) != len(set(ship_ids))
        or len(signature) != len(set(signature))
        or tuple(signature) != expected_signature
    ):
        raise RedDecisionBlocked(
            "red snapshot active_signature must exactly cover active type_ii stages"
        )
    uav_ids = []
    for uav in snapshot.uavs:
        if (
            not isinstance(uav, tuple) or len(uav) != 3 or not _identifier(uav[0])
            or not _vec2(uav[1]) or not _vec2(uav[2])
        ):
            raise RedDecisionBlocked("invalid red snapshot UAV")
        uav_ids.append(uav[0])
    if len(uav_ids) != len(set(uav_ids)):
        raise RedDecisionBlocked("duplicate red snapshot UAV ID")


class RedCommander:
    """Consume one complete fleet snapshot per main frame, on the simulation thread.

    This service installs parameters only. Navigation, motion and delivery of
    substep gate observations belong to the simulation owner (T05/T12).
    Feed those observations through this commander's bound ``threat_gate``.
    Call even with an empty active set to retire cleared gate episodes. Snapshot
    IDs are unique per episode; an identical blocked snapshot may be retried
    after resume, but successful duplicate delivery never reinstalls a plan.
    """

    def __init__(
        self, gateway: LLMGateway, config: ShipConfig,
        threat_gate: ThreatGate | None = None,
    ):
        self.gateway = gateway
        self.config = config
        self.threat_gate = threat_gate or ThreatGate(config)
        self._installation: RedPlanInstallation | None = None
        self._installation_map_version: int | None = None
        self._installation_episode_revision: int | None = None
        self._last_snapshot: RedSnapshot | None = None
        self._last_snapshot_episode_revision: int | None = None
        self._seen_snapshot_ids: set[str] = set()
        self._delivered_snapshot_id: str | None = None
        self._delivered_snapshot_episode_revision: int | None = None
        self._last_request_at_min: float | None = None
        self._prompt = (Path(__file__).parent / "prompts" / "red_commander.txt").read_text(encoding="utf-8")

    def _constraints(self) -> dict:
        config = self.config
        return {
            "heading_offset_deg": [-config.heading_offset_max_deg, config.heading_offset_max_deg],
            "speed_kn": [config.speed_min_kn, config.speed_max_kn],
            "zigzag_heading_deg": [0.0, config.zigzag_heading_max_deg],
            "zigzag_period_min": [config.zigzag_period_min_min, config.zigzag_period_max_min],
            "phase_deg": [0.0, 360.0],
            "max_valid_for_min": min(3.0, config.red_plan_valid_min),
            "normal_speed_kn": config.speed_kn,
            "min_evasion_heading_deg": config.min_evasion_heading_deg,
            "min_evasion_speed_delta_kn": config.min_evasion_speed_delta_kn,
        }

    def _validate_plan(self, payload: dict, snapshot: RedSnapshot) -> tuple[str, ...]:
        errors = []
        if set(payload) != {field.name for field in fields(RedPlan)}:
            errors.append("plan fields must exactly match RedPlan")
        if payload.get("schema_version") != "red-plan/v1":
            errors.append("schema_version must be red-plan/v1")
        if payload.get("snapshot_id") != snapshot.snapshot_id:
            errors.append("snapshot_id does not match current snapshot")
        validity = payload.get("valid_for_min")
        constraints = self._constraints()
        if not _finite_number(validity) or not 0 < validity <= constraints["max_valid_for_min"]:
            errors.append("valid_for_min must be finite, positive and within max_valid_for_min")
        if not isinstance(payload.get("notes"), str):
            errors.append("notes must be a string")
        commands = payload.get("commands")
        if not isinstance(commands, list):
            return tuple(errors + ["commands must be an array"])
        command_ids = []
        for index, command in enumerate(commands):
            prefix = f"commands[{index}]"
            if not isinstance(command, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if set(command) != {field.name for field in fields(RedMotionParameters)}:
                errors.append(f"{prefix} fields must exactly match RedMotionParameters")
            ship_id = command.get("ship_id")
            if not isinstance(ship_id, str) or not ship_id:
                errors.append(f"{prefix}.ship_id must be a nonempty string")
            else:
                command_ids.append(ship_id)
            numeric_ok = True
            for name in ("heading_offset_deg", "speed_kn", "zigzag_heading_deg", "zigzag_period_min", "phase_deg"):
                value = command.get(name)
                lower, upper = constraints[name]
                if (
                    not _finite_number(value) or not lower <= value <= upper
                    or (name == "phase_deg" and value == upper)
                ):
                    errors.append(f"{prefix}.{name} must be finite and within {constraints[name]}"
                                  + (" (upper exclusive)" if name == "phase_deg" else ""))
                    numeric_ok = False
            if numeric_ok and not (
                abs(command["heading_offset_deg"]) >= self.config.min_evasion_heading_deg
                or command["zigzag_heading_deg"] >= self.config.min_evasion_heading_deg
                or abs(command["speed_kn"] - self.config.speed_kn) >= self.config.min_evasion_speed_delta_kn
            ):
                errors.append(f"{prefix} must meet at least one minimum maneuver constraint")
        active_ids = {ship_id for ship_id, _stage in snapshot.active_signature}
        if (
            len(command_ids) != len(commands)
            or len(command_ids) != len(set(command_ids))
            or set(command_ids) != active_ids
        ):
            errors.append("commands must exactly cover active_signature without duplicates")
        return tuple(errors)

    @property
    def installation(self) -> RedPlanInstallation | None:
        self._sync_gate_revision()
        return self._installation

    def _retire_installation(self) -> None:
        self._installation = None
        self._installation_map_version = None
        self._installation_episode_revision = None

    def remove_ship(self, ship_id: str) -> None:
        """Invalidate red state immediately when a vessel leaves the fleet."""
        self.threat_gate.remove_ship(ship_id)
        if (
            self._installation is not None
            and any(command.ship_id == ship_id for command in self._installation.plan.commands)
        ):
            self._retire_installation()

    def _sync_gate_revision(self) -> int:
        episode_revision = self.threat_gate.episode_revision
        if (
            self._installation is not None
            and self._installation_episode_revision != episode_revision
        ):
            self._retire_installation()
        if self._delivered_snapshot_episode_revision != episode_revision:
            self._delivered_snapshot_id = None
            self._delivered_snapshot_episode_revision = None
        return episode_revision

    def _mark_delivered(self, snapshot_id: str, episode_revision: int) -> None:
        self._delivered_snapshot_id = snapshot_id
        self._delivered_snapshot_episode_revision = episode_revision

    def decide(self, snapshot: RedSnapshot) -> RedPlan | None:
        _validate_snapshot(snapshot)
        episode_revision = self._sync_gate_revision()
        previous = self._last_snapshot
        if previous is not None:
            if snapshot.snapshot_id == previous.snapshot_id:
                if snapshot != previous:
                    raise RedDecisionBlocked("snapshot ID reused with different content")
                if self._last_snapshot_episode_revision != episode_revision:
                    raise RedDecisionBlocked("snapshot belongs to an older gate revision")
                if self._delivered_snapshot_id == snapshot.snapshot_id:
                    return self._installation.plan if self._installation else None
            elif (
                snapshot.snapshot_id in self._seen_snapshot_ids
                or snapshot.sim_time_min < previous.sim_time_min
            ):
                raise RedDecisionBlocked("stale red snapshot")
        self._last_snapshot = snapshot
        self._last_snapshot_episode_revision = episode_revision
        self._seen_snapshot_ids.add(snapshot.snapshot_id)
        active_signature = snapshot.active_signature
        if not active_signature:
            self._retire_installation()
            self._mark_delivered(snapshot.snapshot_id, episode_revision)
            return None
        installed = self._installation
        can_reuse = (
            installed is not None
            and installed.active_signature == active_signature
            and self._installation_map_version == snapshot.land_mask_version
            and snapshot.sim_time_min < installed.expires_at_min
        )
        if installed is not None:
            if not can_reuse:
                self._retire_installation()
            elif (
                self._last_request_at_min is not None
                and snapshot.sim_time_min - self._last_request_at_min < self.config.red_decision_cycle_min
            ):
                self._mark_delivered(snapshot.snapshot_id, episode_revision)
                return installed.plan
        self._last_request_at_min = snapshot.sim_time_min
        result = self.gateway.request_json(
            role="red_commander", snapshot_id=snapshot.snapshot_id,
            system_prompt=self._prompt,
            user_payload={"snapshot": asdict(snapshot), "constraints": self._constraints()},
            validate=lambda payload: self._validate_plan(payload, snapshot),
        )
        if not result.success:
            if can_reuse:
                self._mark_delivered(snapshot.snapshot_id, episode_revision)
                return self._installation.plan
            raise RedDecisionBlocked("; ".join(result.errors))
        payload = result.payload
        plan = RedPlan(
            schema_version=payload["schema_version"], snapshot_id=payload["snapshot_id"],
            valid_for_min=payload["valid_for_min"],
            commands=tuple(RedMotionParameters(**command) for command in payload["commands"]),
            notes=payload["notes"],
        )
        self._installation = RedPlanInstallation(
            plan, snapshot.sim_time_min, active_signature,
        )
        self._installation_episode_revision = episode_revision
        self._installation_map_version = snapshot.land_mask_version
        self._mark_delivered(snapshot.snapshot_id, episode_revision)
        return plan
