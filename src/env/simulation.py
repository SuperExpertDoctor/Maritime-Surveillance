"""Headless simulation engine shared by the CLI, tests, and web service."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections.abc import Mapping

import numpy as np

from src.env.base_station import BaseStation
from src.env.ais_signal import generate_ais_signal
from src.env.eo_sensor import EOSensor
from src.env.obstacle import (
    Island,
    Thunderstorm,
    mainland_land_mask,
    default_obstacles,
    obstacle_grid_mask,
    obstacle_intersects_mask,
)
from src.env.sar_sensor import SARSensor
from src.env.ship import Ship, ShipType, formation_offsets
from src.env.sim_clock import SimClock
from src.env.uav_entity import UAVEntity
from src.control.common.contracts import (
    ActionSpec,
    BaseObservation,
    ControlEvent,
    ControlMode,
    ControlOwner,
    ControlTask,
    OperationMode,
    RecoveryPlan,
    SensorMode,
)
from src.control.common.coordinator import (
    ControlCoordinator,
    ControlCoordinatorError,
    EmergencyRevokeRequired,
)
from src.control.common.executor import UAVDynamicsExecutor
from src.control.common.factory import ControlFactory, ControlProvider
from src.control.common.observation import ObservationProvider
from src.control.common.operation_registry import OperationRegistry
from src.control.common.ownership import ControlOwnership
from src.control.common.safety import SafetyEnvelope
from src.control.heuristic.return_to_base import (
    NoSafeRecoveryPath,
    RecoveryPlanner,
)
from src.schedule.config_loader import AppConfig
from src.schedule.datatypes import GridCoord, Region
from src.schedule.task_allocator import TaskAllocator
from src.utils.coverage_planner import CoveragePlanner
from src.utils.ais_discriminator import AISDiscriminator
from src.utils.obstacle_avoider import ObstacleAvoider
from src.utils.phase_coordinator import PhaseCoordinator
from src.utils.conflict_detector import (
    detect_conflicts,
    resolve_conflicts,
    uav_id_priority,
)
from src.utils.search_route_planner import (
    SearchRoutePlan,
    SearchRouteRequest,
    plan_search_route,
)


class SimulationEngine:
    def __init__(
        self,
        config: AppConfig,
        seed: int = 42,
        *,
        control_providers: Mapping[ControlMode | str, ControlProvider] | None = None,
    ):
        self.config = config
        self.seed = seed
        self._control_providers = dict(control_providers or {})
        self.reset_generation = 0
        self.rng = random.Random(seed)
        random.seed(seed)
        self.clock = SimClock()
        base_positions = self._generate_base_positions()
        self.bases = [
            BaseStation(
                GridCoord(*position),
                config.uav.refuel_time_min,
                capacity=config.environment.base_capacity,
                base_id=f"Base-{index + 1}",
            )
            for index, position in enumerate(base_positions)
        ]
        self.base = self.bases[0]
        self.allocator = TaskAllocator(config)
        self.allocator.llm_client.assert_ready()
        self.allocator.sm.set_base_positions(base_positions)
        self.land_mask = mainland_land_mask(
            config.grid.resolution,
            config.environment.mainland_width_cells,
        )
        self.allocator.sm.set_land_mask(self.land_mask)
        self.allocator.sm.scenario_seed = seed
        self.allocator.sm.scenario_generation = self.reset_generation
        self.coverage_planner = CoveragePlanner(sample_step=0.2)
        self.obstacle_avoider = ObstacleAvoider(max_iterations=1000, seed=seed)
        self.phase_coordinator = PhaseCoordinator()
        island_count = self.rng.randint(
            config.environment.island_count_min,
            config.environment.island_count_max,
        )
        self._storm_target_count = self.rng.randint(
            config.environment.thunderstorm_count_min,
            config.environment.thunderstorm_count_max,
        )
        self.obstacles = default_obstacles(
            seed,
            base_positions=base_positions,
            island_count=island_count,
            thunderstorm_count=self._storm_target_count,
            base_clearance_cells=config.environment.base_obstacle_clearance_cells,
            resolution=config.grid.resolution,
            land_mask=self.land_mask,
        )
        self._next_storm_id = 1 + sum(
            isinstance(obstacle, Thunderstorm) for obstacle in self.obstacles
        )
        self.obstacle_mask = obstacle_grid_mask(
            self.obstacles,
            config.grid.resolution,
            config.environment.storm_safety_margin_cells,
            include_islands=True,
        )
        self.allocator.sm.set_environment_obstacles(self.obstacles, self.obstacle_mask)

        self.uavs = [
            UAVEntity(
                f"UAV-{index + 1}",
                self.bases[index % len(self.bases)].position,
                config.uav.sortie_endurance_h,
                config.uav.cruise_speed_kmh,
                cell_size_km=config.grid.cell_size_km,
                R_min=1.0,
            )
            for index in range(config.uav.count_max)
        ]
        for index, uav in enumerate(self.uavs):
            uav.heading_rad = self._inward_heading(self.bases[index % len(self.bases)].position)
            # StateManager drives Hungarian assignment before the first
            # entity-to-state sync.  Publish each alternating coastal launch
            # position now so the first plan does not treat the whole fleet
            # as if it departed from Base-1.
            self.allocator.sm.update_uav_status(
                uav.id,
                "idle",
                uav.position,
                fuel_remaining_pct=uav.fuel_remaining_pct,
                heading_deg=uav.heading_deg,
                sensor_mode=uav.sensor_mode,
            )
        for uav in self.uavs:
            uav.sar_sensor = SARSensor(
                swath_width_cells=config.sensor.sar.swath_km / config.grid.cell_size_km,
                detection_probability=config.sensor.sar.detection_probability,
                grid_shape=config.grid.resolution,
            )
            uav.eo_sensor = EOSensor(
                fov_deg=config.sensor.eoir.fov_deg,
                max_range_cells=config.sensor.eoir.detection_range_km / config.grid.cell_size_km,
            )
            uav.storm_avoider.eo_detection_range_cells = uav.eo_sensor.max_range_cells

        self._control_event_sequence = 1
        self._return_reservation_sequence = 1
        self._coordinator_tasks: dict[str, ControlTask] = {}
        self._next_sortie_number: dict[str, int] = {
            uav.id: 1 for uav in self.uavs
        }
        self._emergency_failures: dict[str, str] = {}
        self._control_runtime_enabled = True
        action_spec = self._control_action_spec()
        self.control_ownership = ControlOwnership(tuple(uav.id for uav in self.uavs))
        self.observation_provider = ObservationProvider(config)
        self.safety_envelope = SafetyEnvelope(action_spec)
        self.dynamics_executor = UAVDynamicsExecutor()
        self.operation_registry = OperationRegistry(self.allocator.sm)
        self.control_factory = ControlFactory(
            config.control,
            action_spec=action_spec,
        )
        for mode, provider in self._control_providers.items():
            resolved_mode = ControlMode(mode)
            if resolved_mode is ControlMode.HEURISTIC:
                raise ValueError("the built-in heuristic provider cannot be replaced")
            self.control_factory.register(resolved_mode, provider)
        configured_modes = {
            uav.id: ControlMode(
                config.control.per_uav.get(uav.id, config.control.default_mode)
            )
            for uav in self.uavs
        }
        self.control_coordinator = ControlCoordinator(
            config=config.control,
            state_manager=self.allocator.sm,
            ownership=self.control_ownership,
            observations=self.observation_provider,
            safety=self.safety_envelope,
            executor=self.dynamics_executor,
            factory=self.control_factory,
            operation_registry=self.operation_registry,
            configured_modes=configured_modes,
            bases=self._control_base_observations,
        )
        for uav in self.uavs:
            if configured_modes[uav.id] is not ControlMode.HEURISTIC:
                self.control_coordinator.start_work(
                    uav.id,
                    sortie_number=self._next_sortie_number[uav.id],
                    current_time=0.0,
                    dt_min=self.clock.dt_min,
                )
        self.ships = self._create_ships()
        self._refresh_ais_signals(0.0)
        self.ais_discriminator = AISDiscriminator(
            config.ship.ais_discrepancy_threshold_cells
        )
        self.heavy_triggers = 0
        self.light_triggers = 0
        self.llm_successes = 0
        self.region_signatures: list[tuple] = []
        self.track_creations = 0
        self.status_history: dict[str, list[str]] = defaultdict(lambda: ["idle"])
        self.lifecycle_cycles: dict[str, int] = {
            uav.id: 0 for uav in self.uavs
        }
        self._sortie_searched: dict[str, bool] = {
            uav.id: False for uav in self.uavs
        }
        self._search_started_at: dict[str, float] = {}
        self._tracking_started_at: dict[str, float] = {}
        self._ais_tracking_started_at: dict[str, float] = {}
        self._ais_measurements: dict[str, list[tuple[float, float]]] = {}
        self.ais_discriminations = 0
        self.civilian_releases = 0
        self.storm_avoidance_events = 0
        self._storm_levels: dict[str, int] = {}
        self._storm_level3_started_at: dict[str, float] = {}
        self._lifecycle_mode = False
        self._lifecycle_completed = False
        self._return_base_by_uav: dict[str, BaseStation] = {}
        self._holding_base_by_uav: dict[str, BaseStation] = {}
        self._freshness_patrol_uavs: set[str] = set()
        self._return_reason_counts: dict[str, int] = defaultdict(int)
        self.departed_ship_count = 0
        self._departed_groups: set[str] = set()
        self.last_result: dict = {"trigger_type": "none", "action": None}

    def _create_ships(self) -> list[Ship]:
        cfg = self.config.ship
        count = self.rng.randint(cfg.target_min, cfg.target_max)
        group_limit = min(cfg.group_max, cfg.max_groups, max(1, count - 1))
        group_count = self.rng.randint(1, group_limit)
        group_sizes = [1] * group_count
        # A fleet has at least one actual formation rather than only singleton
        # targets.  Any surplus is spread across groups deterministically.
        group_sizes[0] += 1
        for index in range(count - sum(group_sizes)):
            group_sizes[index % group_count] += 1
        carrier_group = 0 if cfg.carrier_max > 0 and count >= 3 else None
        if carrier_group is not None and group_sizes[carrier_group] < 3:
            for donor in range(1, len(group_sizes)):
                if group_sizes[donor] > 1:
                    group_sizes[donor] -= 1
                    group_sizes[carrier_group] += 1
                    break
            if group_sizes[carrier_group] < 3:
                carrier_group = None
        ships: list[Ship] = []
        islands = [item for item in self.obstacles if isinstance(item, Island)]
        ship_index = 0
        for group, size in enumerate(group_sizes):
            center = self._random_ship_group_center(islands)
            heading = self.rng.uniform(0, 2 * math.pi)
            military = group == carrier_group or self.rng.choice((True, False))
            has_carrier = group == carrier_group
            offsets = formation_offsets(size, has_carrier)
            cos_h, sin_h = math.cos(heading), math.sin(heading)
            for member in range(size):
                ship_type = (
                    ShipType.AIRCRAFT_CARRIER
                    if group == carrier_group and member == 0
                    else ShipType.DESTROYER
                )
                # Local (forward, right) → world offset rotated by group heading
                fwd, right = offsets[member]
                world_dx = fwd * cos_h - right * sin_h
                world_dy = fwd * sin_h + right * cos_h
                position = GridCoord(
                    int(round(center[0] + world_dx)),
                    int(round(center[1] + world_dy)),
                )
                speed = cfg.carrier_speed_kn if ship_type is ShipType.AIRCRAFT_CARRIER else cfg.destroyer_speed_kn
                ship = Ship(
                    f"Ship-{group + 1}-{member + 1}",
                    position,
                    speed,
                    cfg.zigzag_amplitude_km,
                    cfg.zigzag_period_min,
                    self.config.grid.cell_size_km,
                    ship_type=ship_type,
                    group_id=f"G{group + 1}",
                    base_heading=heading,
                    formation_offset=offsets[member],
                    actual_military=military,
                    zigzag_heading_deg=cfg.zigzag_heading_deg,
                    max_turn_rate_deg_min=cfg.max_turn_rate_deg_min,
                    yaw_time_constant_min=cfg.yaw_time_constant_min,
                    heading_control_gain_per_min=cfg.heading_control_gain_per_min,
                    turn_speed_loss_fraction=cfg.turn_speed_loss_fraction,
                )
                ship.ais_mode = (
                    "civilian"
                    if not military
                    else ("silent" if (group + member) % 2 == 0 else "deceptive")
                )
                ships.append(ship)
                ship_index += 1
        return ships

    def _generate_base_positions(self) -> tuple[tuple[int, int], ...]:
        cfg = self.config.environment
        cols, rows = self.config.grid.resolution
        if not 1 <= cfg.base_count <= 3:
            raise ValueError("base_count must be between 1 and 3")
        mainland_width = max(2, min(int(cfg.mainland_width_cells), cols - 1))
        inland_column_end = max(2, min(mainland_width - 1, 3))
        candidates = [
            (col, row)
            for col in range(1, inland_column_end)
            for row in range(2, rows - 2)
        ]
        self.rng.shuffle(candidates)
        selected: list[tuple[int, int]] = []
        for candidate in candidates:
            if all(math.dist(candidate, existing) >= cfg.base_min_distance_cells for existing in selected):
                selected.append(candidate)
                if len(selected) == cfg.base_count:
                    return tuple(selected)
        raise RuntimeError("unable to place the requested separated coastal bases")

    def _random_ship_group_center(self, islands: list[Island]) -> tuple[float, float]:
        mainland_width = self.config.environment.mainland_width_cells
        for _ in range(200):
            center = (self.rng.uniform(mainland_width + 2.0, 25.0), self.rng.uniform(4.0, 25.0))
            col, row = int(round(center[0])), int(round(center[1]))
            if (
                not self.land_mask[col, row]
                and all(not island.contains(center) and island.distance_to_boundary(center) >= 2.0 for island in islands)
            ):
                return center
        return 15.0, 15.0

    def _inward_heading(self, position: GridCoord) -> float:
        if position.col < self.config.environment.mainland_width_cells:
            return 0.0
        margin = self.config.environment.base_land_margin
        cols, rows = self.config.grid.resolution
        if position.row <= margin:
            return math.pi / 2.0
        if position.row >= rows - 1 - margin:
            return -math.pi / 2.0
        if position.col <= margin:
            return 0.0
        if position.col >= cols - 1 - margin:
            return math.pi
        return -math.pi / 2.0

    def reset(self, seed: int | None = None) -> "SimulationEngine":
        """Fully rebuild the scenario, optionally reproducing a supplied seed."""
        previous_seed = self.seed
        generation = self.reset_generation + 1
        next_seed = self.seed + 1 if seed is None else int(seed)
        self.__init__(
            self.config,
            next_seed,
            control_providers=self._control_providers,
        )
        self.reset_generation = generation
        self.allocator.sm.scenario_generation = generation
        self.allocator.sm.add_event("environment_reset", {
            "previous_seed": previous_seed,
            "seed": next_seed,
            "generation": generation,
            "base_ids": [base.id for base in self.bases],
        })
        return self

    def step(self) -> dict:
        t = self.clock.tick()
        sm = self.allocator.sm
        sm.current_time = t

        self._update_obstacles()
        self._update_ships(t)
        self._refresh_ais_signals(t)

        for uav in self.uavs:
            fuel_low = self._step_controlled_uav(uav, t)
            self._record_storm_avoidance(uav, t)
            # GOAL2: proactive fuel warning at 25% — gives the scheduler time
            # to pre-assign a replacement before the critical 8% return trigger.
            if (
                uav.fuel_remaining_pct <= 0.25
                and not uav.fuel_warning_sent
                and uav.status in ("searching", "tracking", "transit")
            ):
                uav.fuel_warning_sent = True
                self.allocator.trigger_manager.notify_event(
                    "uav_fuel_low_warning",
                    time=t,
                    uav_id=uav.id,
                    fuel_pct=round(uav.fuel_remaining_pct, 3),
                    status=uav.status,
                )
            if uav.status == "searching":
                self._sortie_searched[uav.id] = True
                self._search_started_at.setdefault(uav.id, t)
            lifecycle_search_due = (
                self._lifecycle_mode
                and self.lifecycle_cycles[uav.id]
                < self.config.uav.lifecycle_required_cycles
                and uav.status == "searching"
                and t - self._search_started_at[uav.id]
                >= self.config.uav.lifecycle_search_dwell_min
            )
            tracking_due = (
                self._lifecycle_mode
                and self.lifecycle_cycles[uav.id]
                < self.config.uav.lifecycle_required_cycles
                and uav.status == "tracking"
                and uav.id in self._tracking_started_at
                and t - self._tracking_started_at[uav.id]
                >= self.config.uav.lifecycle_search_dwell_min
            )
            return_reason = next(
                (
                    reason
                    for reason, triggered in (
                        ("fuel_low", fuel_low),
                        ("lifecycle_search", lifecycle_search_due),
                        ("lifecycle_tracking", tracking_due),
                        ("range_reserve", self._needs_reserve_return(uav)),
                    )
                    if triggered
                ),
                None,
            )
            if return_reason is not None:
                self._return_reason_counts[return_reason] += 1
                sm.add_event("return_triggered", {
                    "uav_id": uav.id,
                    "reason": return_reason,
                    "fuel_remaining_pct": round(uav.fuel_remaining_pct, 4),
                })
                self._begin_return(uav, t)

        self._update_sensors_and_detections(t)
        self._update_lifecycle_mode(t)
        self._process_refuelling(t)
        self._sync_state_from_entities()

        result = self.allocator.step(t)
        self.last_result = result
        if result["trigger_type"] == "heavy":
            self.heavy_triggers += 1
            interaction = result.get("llm_cycle") or {}
            self.llm_successes += int(bool(interaction.get("success")))
            signature = tuple(
                (region["id"], tuple(region["bbox"]))
                for region in result.get("search_regions", [])
            )
            if signature and (not self.region_signatures or signature != self.region_signatures[-1]):
                self.region_signatures.append(signature)
        elif result["trigger_type"] == "light":
            self.light_triggers += 1
        self._sync_assignments()
        self._detect_and_resolve_path_conflicts(t)
        self._record_statuses()
        return result

    def _step_controlled_uav(self, uav: UAVEntity, current_time: float) -> bool:
        """Run one coordinator tick and return the low-fuel edge trigger."""
        if uav.id in self._emergency_failures:
            return False
        lease = self.control_coordinator.current_lease(uav.id)
        if not self.control_coordinator.has_controller(uav.id):
            return False

        if lease.owner in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
            try:
                self._maybe_revoke_for_range(uav, current_time)
            except NoSafeRecoveryPath as exc:
                self._enter_emergency_failure(uav, "no_safe_recovery_path", exc)
                return False
            lease = self.control_coordinator.current_lease(uav.id)

        try:
            tick = self.control_coordinator.step_uav(
                uav,
                current_time=current_time,
                dt_min=self.clock.dt_min,
            )
        except NoSafeRecoveryPath as exc:
            if (
                lease.owner is ControlOwner.SYSTEM
                and self.control_coordinator.operation_mode(uav.id)
                is OperationMode.RETURN
            ):
                self._enter_emergency_failure(uav, "no_safe_recovery_path", exc)
                return False
            self._handle_control_fault(uav, current_time, exc, lease)
            return False
        except Exception as exc:
            self._handle_control_fault(uav, current_time, exc, lease)
            return False

        self._record_control_tick(uav, tick)
        fuel_low = (
            uav.fuel_remaining_pct <= 0.08
            and uav.status not in ("returning", "idle", "refueling")
            and not getattr(uav, "_fuel_low_reported", False)
        )
        if fuel_low:
            uav._fuel_low_reported = True
        return fuel_low

    def _record_control_tick(self, uav: UAVEntity, tick) -> None:
        """Bridge immutable control output into legacy entity diagnostics."""
        active_task = self.control_coordinator.active_task(uav.id)
        if active_task is not None:
            self._coordinator_tasks[uav.id] = active_task
        command = tick.execution.applied_command
        if command.operation_mode is OperationMode.TRACK:
            uav.target_group_id = command.target_contact_id
            if command.target_contact_id:
                uav._mission_kind = "track_entry"
        elif command.operation_mode not in (OperationMode.TRACK,):
            uav.target_group_id = None
        if command.sensor_mode is SensorMode.SAR:
            uav.sar_look_direction = uav.sar_look_direction or "right"
            uav.sar_scan_heading_rad = uav.heading_rad
            uav.sar_heading_error_deg = 0.0
            uav.sar_imaging = True
        else:
            uav.sar_imaging = False
        for event in tick.emitted_events:
            if event.event_type == "search_complete":
                self._record_search_completion_event(uav, event)
            elif event.event_type == "task_failed":
                self.allocator.sm.add_event("task_failed", {
                    "uav_id": uav.id,
                    **dict(event.payload),
                })

        if (
            command.operation_mode is OperationMode.RETURN
            and uav.id in self._return_base_by_uav
            and math.dist(
                uav.float_position,
                (
                    self._return_base_by_uav[uav.id].position.col,
                    self._return_base_by_uav[uav.id].position.row,
                ),
            ) <= 0.05
        ):
            uav.status = "refueling"
            uav.sensor_mode = "off"
            self._land_for_refuelling(uav)

    def _record_search_completion_event(
        self, uav: UAVEntity, event: ControlEvent
    ) -> None:
        task = self.control_coordinator.active_task(uav.id)
        region_id = task.task_id if task and task.task_type is OperationMode.COVERAGE else None
        if region_id is None:
            region_id = event.payload.get("task_id")
        region = next(
            (
                item
                for item in self.allocator.sm.get_search_regions()
                if item.id == region_id
            ),
            None,
        )
        if region is not None:
            region.status = "completed"
            region.completion_pct = 100.0
            region.assigned_uav_id = None
        uav.completed_searches_since_refuel += 1
        self._sortie_searched[uav.id] = True
        self.allocator.sm.clear_uav_assignment(uav.id)
        self.allocator.trigger_manager.notify_event(
            "search_complete",
            time=event.timestamp_min,
            uav_id=uav.id,
            region_id=region_id,
        )
        self.allocator.sm.add_event("search_complete", {
            "uav_id": uav.id,
            "region_id": region_id,
        })

    def _land_for_refuelling(self, uav: UAVEntity) -> None:
        base = self._return_base_by_uav.get(uav.id)
        if base is not None and base.land_uav(uav.id):
            return
        if base is not None:
            self._holding_base_by_uav[uav.id] = base
            uav.start_holding(base.position)
            if self.control_coordinator.has_controller(uav.id):
                lease = self.control_coordinator.current_lease(uav.id)
                if lease.owner is ControlOwner.SYSTEM:
                    holding_task = ControlTask(
                        f"holding:{uav.id}:{self.clock.time}",
                        OperationMode.HOLDING,
                    )
                    self.control_coordinator.assign_system_task(
                        uav.id,
                        holding_task,
                        current_time=self.clock.time,
                    )
                    self._coordinator_tasks[uav.id] = holding_task

    def _maybe_revoke_for_range(
        self, uav: UAVEntity, current_time: float, *, force: bool = False
    ) -> bool:
        """Reserve a validated base before a work command can run out of range."""
        lease = self.control_coordinator.current_lease(uav.id)
        if lease.owner not in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
            return False
        reserve_cells = self.config.control.safety.reserve_range_cells
        planner = RecoveryPlanner()
        try:
            candidates = planner.evaluate(
                uav.pose,
                uav.remaining_range_cells,
                self._control_base_observations(),
                self.allocator.sm.obstacle_mask,
                self.allocator.sm.obstacle_version,
                uav.R_min,
                reserve_cells,
            )
        except (RuntimeError, ValueError) as exc:
            candidates = ()
            planning_error = str(exc)
        else:
            planning_error = "no candidate satisfies the range and safety contract"
        if not candidates:
            error = NoSafeRecoveryPath(
                "none",
                self.allocator.sm.obstacle_version,
                planning_error,
            )
            self._emit_no_safe_recovery_path(uav, current_time, error)
            raise error

        candidate = candidates[0]
        max_speed = self._control_action_spec().max_speed_cells_min
        threshold = (
            candidate.path_length_cells
            + candidate.reserve_cells
            + max_speed * self.clock.dt_min
        )
        if not force and uav.remaining_range_cells > threshold:
            return False

        base = next(
            (item for item in self.bases if item.id == candidate.base.base_id),
            None,
        )
        if base is None or self._base_maintenance_load(base) >= base.capacity:
            self._emit_no_safe_recovery_path(
                uav,
                current_time,
                NoSafeRecoveryPath(
                    candidate.base.base_id,
                    self.allocator.sm.obstacle_version,
                    "recovery base reservation is no longer available",
                ),
            )
            raise NoSafeRecoveryPath(
                candidate.base.base_id,
                self.allocator.sm.obstacle_version,
                "recovery base reservation is no longer available",
            )

        reservation_id = f"{uav.id}:return:{self._return_reservation_sequence}"
        self._return_reservation_sequence += 1
        plan = RecoveryPlan(
            base_id=candidate.base.base_id,
            base_position=candidate.base.position,
            reservation_id=reservation_id,
            path=candidate.path,
            path_length_cells=candidate.path_length_cells,
            reserve_cells=candidate.reserve_cells,
            planning_map_version=candidate.planning_map_version,
        )
        self._return_base_by_uav[uav.id] = base
        try:
            self.control_coordinator.revoke_for_return(
                uav.id,
                plan,
                current_time=current_time,
            )
        except Exception:
            if self._return_base_by_uav.get(uav.id) is base:
                self._return_base_by_uav.pop(uav.id, None)
            raise
        self._coordinator_tasks[uav.id] = ControlTask(
            reservation_id,
            OperationMode.RETURN,
            recovery_plan=plan,
        )
        uav.plan_return(plan.path)
        self._prepare_return_state(uav, current_time)
        self.allocator.sm.add_event("return_reserved", {
            "uav_id": uav.id,
            "base_id": base.id,
            "reservation_id": reservation_id,
            "reason": "range_reserve",
        })
        return True

    def _emit_no_safe_recovery_path(
        self,
        uav: UAVEntity,
        current_time: float,
        error: NoSafeRecoveryPath,
    ) -> None:
        self.allocator.trigger_manager.notify_event(
            "no_safe_recovery_path",
            time=current_time,
            uav_id=uav.id,
            base_id=error.base_id,
            reason=error.reason,
        )
        self.allocator.sm.add_event("no_safe_recovery_path", {
            "uav_id": uav.id,
            "base_id": error.base_id,
            "reason": error.reason,
        })

    def _handle_control_fault(
        self,
        uav: UAVEntity,
        current_time: float,
        error: Exception,
        lease,
    ) -> None:
        """Recover work-controller faults through the same reservation transaction."""
        if lease.owner not in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
            self._enter_emergency_failure(uav, "controller_fault", error)
            return
        reason = (
            "invalid_command_limit"
            if isinstance(error, EmergencyRevokeRequired)
            else "controller_fault"
        )
        self.allocator.trigger_manager.notify_event(
            reason,
            time=current_time,
            uav_id=uav.id,
            error=str(error),
        )
        self.allocator.sm.add_event(reason, {
            "uav_id": uav.id,
            "error": str(error),
        })
        try:
            self._request_recovery_return(uav, current_time, reason)
        except NoSafeRecoveryPath as recovery_error:
            self._enter_emergency_failure(
                uav,
                "no_safe_recovery_path",
                recovery_error,
            )

    def _request_recovery_return(
        self,
        uav: UAVEntity,
        current_time: float,
        reason: str,
    ) -> None:
        """Create a reserved return plan and then transfer the control lease."""
        lease = self.control_coordinator.current_lease(uav.id)
        if lease.owner not in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
            raise ControlCoordinatorError(
                f"{uav.id} does not have a work lease for recovery"
            )
        try:
            self._maybe_revoke_for_range(uav, current_time, force=True)
        except NoSafeRecoveryPath:
            raise
        if self.control_coordinator.current_lease(uav.id).owner is not ControlOwner.SYSTEM:
            raise ControlCoordinatorError(
                f"{uav.id} recovery did not install SYSTEM ownership"
            )
        self.allocator.sm.add_event("return_triggered", {
            "uav_id": uav.id,
            "reason": reason,
            "fuel_remaining_pct": round(uav.fuel_remaining_pct, 4),
        })

    def _prepare_return_state(self, uav: UAVEntity, current_time: float) -> None:
        sm = self.allocator.sm
        self._freshness_patrol_uavs.discard(uav.id)
        self._search_started_at.pop(uav.id, None)
        self._tracking_started_at.pop(uav.id, None)
        self._ais_tracking_started_at.pop(uav.id, None)
        self._ais_measurements.pop(uav.id, None)
        group_id = uav.target_group_id
        if group_id:
            report = sm.get_target_report(group_id)
            track = sm.get_track_region_for_group(group_id)
            if track is not None and track.assigned_uav_id == uav.id:
                sm.release_track_region(track.id, uav.id, create_marker=True)
                self.allocator.trigger_manager.notify_event(
                    "target_lost",
                    time=current_time,
                    uav_id=uav.id,
                    group_id=group_id,
                )
                sm.add_event("target_lost", {
                    "uav_id": uav.id,
                    "group_id": group_id,
                })
                if report is not None:
                    sm.add_event("target_handoff_report", {
                        "uav_id": uav.id,
                        "group_id": report.group_id,
                        "position": report.position,
                        "observed_at": report.observed_at,
                    })
            for member in self.ships:
                if member.group_id == group_id:
                    member.set_tracked(False)
        uav.target_group_id = None
        for region in sm.get_search_regions():
            if region.assigned_uav_id == uav.id:
                region.assigned_uav_id = None
        sm.clear_uav_assignment(uav.id)
        uav.status = "returning"
        uav.sensor_mode = "off"

    def _enter_emergency_failure(
        self, uav: UAVEntity, reason: str, error: Exception
    ) -> None:
        self._emergency_failures[uav.id] = reason
        uav.sensor_mode = "off"
        self.allocator.trigger_manager.notify_event(
            "emergency_failure",
            time=self.clock.time,
            uav_id=uav.id,
            reason=reason,
            error=str(error),
        )
        self.allocator.sm.add_event("emergency_failure", {
            "uav_id": uav.id,
            "reason": reason,
            "error": str(error),
        })

    def _queue_control_event(
        self,
        event_type: str,
        uav_id: str,
        current_time: float,
        payload: Mapping[str, object] | None = None,
    ) -> ControlEvent:
        event = ControlEvent(
            self._control_event_sequence,
            current_time,
            event_type,
            "simulation",
            uav_id,
            payload or {},
        )
        self._control_event_sequence += 1
        self.control_coordinator.queue_event(event)
        return event

    def run(self, steps: int = 480, on_step=None) -> dict:
        for _ in range(steps):
            result = self.step()
            if on_step is not None:
                on_step(self, result)
        return self.summary()

    def summary(self) -> dict:
        coverage = self.allocator.sm.get_coverage_stats()
        return {
            "steps": int(self.clock.time),
            **coverage,
            "heavy_triggers": self.heavy_triggers,
            "light_triggers": self.light_triggers,
            "llm_success_rate": (
                self.llm_successes / self.heavy_triggers if self.heavy_triggers else 0.0
            ),
            "detected_ships": sum(ship.detected for ship in self.ships),
            "ship_count": len(self.ships),
            "region_changes": len(self.region_signatures),
            "track_creations": self.track_creations,
            "ais_discriminations": self.ais_discriminations,
            "civilian_releases": self.civilian_releases,
            "storm_avoidance_events": self.storm_avoidance_events,
            "departed_ship_count": self.departed_ship_count,
            "base_refuel_counts": {base.id: base.refuel_count for base in self.bases},
            "return_reason_counts": dict(self._return_reason_counts),
            "markers": len(self.allocator.sm.get_active_markers()),
            "lifecycle_cycles": dict(self.lifecycle_cycles),
            "min_lifecycle_cycles": min(self.lifecycle_cycles.values(), default=0),
            "status_history": dict(self.status_history),
            "scenario_seed": self.seed,
            "reset_generation": self.reset_generation,
        }

    def _control_action_spec(self) -> ActionSpec:
        """Build simulation-scale bounds from the configured cruise speed."""
        nominal_speed = (
            self.config.uav.cruise_speed_kmh
            / self.config.grid.cell_size_km
            / 60.0
        )
        min_speed = nominal_speed * self.config.control.safety.min_speed_fraction
        max_speed = nominal_speed * self.config.control.safety.max_speed_fraction
        if min_speed <= 0.0 or max_speed < min_speed:
            raise ValueError("control safety speed fractions produce invalid bounds")
        max_turn = max_speed / 1.0
        return ActionSpec(-max_turn, max_turn, min_speed, max_speed)

    def _control_base_observations(self) -> tuple[BaseObservation, ...]:
        return tuple(
            BaseObservation(
                base_id=base.id,
                position=(float(base.position.col), float(base.position.row)),
                capacity=base.capacity,
                reserved_load=self._base_maintenance_load(base),
            )
            for base in sorted(self.bases, key=lambda item: item.id)
        )

    def _update_obstacles(self) -> None:
        previous_mask = self.obstacle_mask
        active = []
        dissipated_storms = []
        for obstacle in self.obstacles:
            if hasattr(obstacle, "step"):
                if obstacle.step(self.clock.dt_min, self.config.grid.resolution):
                    if isinstance(obstacle, Thunderstorm) and obstacle_intersects_mask(
                        obstacle,
                        self.land_mask,
                        safety_margin=1.0,
                    ):
                        dissipated_storms.append(obstacle)
                    else:
                        active.append(obstacle)
                elif isinstance(obstacle, Thunderstorm):
                    dissipated_storms.append(obstacle)
            else:
                active.append(obstacle)
        for storm in dissipated_storms:
            self.allocator.sm.add_event("storm_dissipated", {"storm_id": storm.id})
            self.allocator.trigger_manager.notify_event(
                "storm_dissipated",
                time=self.clock.time,
                storm_id=storm.id,
                position={"col": storm.center[0], "row": storm.center[1]},
                size=storm.size,
            )
        while sum(isinstance(obstacle, Thunderstorm) for obstacle in active) < self._storm_target_count:
            replacement = self._spawn_thunderstorm(active)
            if replacement is None:
                break
            active.append(replacement)
            self.allocator.sm.add_event("storm_spawned", {"storm_id": replacement.id})
            self.allocator.trigger_manager.notify_event(
                "storm_spawned",
                time=self.clock.time,
                storm_id=replacement.id,
                position={"col": replacement.center[0], "row": replacement.center[1]},
                size=replacement.size,
            )
        self.obstacles = active
        self.obstacle_mask = obstacle_grid_mask(
            active,
            self.config.grid.resolution,
            self.config.environment.storm_safety_margin_cells,
            include_islands=True,
        )
        self.allocator.sm.set_environment_obstacles(active, self.obstacle_mask)
        if not np.array_equal(previous_mask, self.obstacle_mask):
            self._replan_conflicting_routes()

    def _spawn_thunderstorm(self, obstacles) -> Thunderstorm | None:
        """Restore the configured moving-storm density after dissipation."""
        cols, rows = self.config.grid.resolution
        for _ in range(200):
            size = self.rng.randint(1, 2)
            half_extent = size / 2.0
            center = (
                self.rng.uniform(half_extent + 1.0, cols - half_extent - 1.0),
                self.rng.uniform(half_extent + 1.0, rows - half_extent - 1.0),
            )
            candidate = Thunderstorm(
                center=center,
                size=size,
                move_vector=(self.rng.uniform(-0.05, 0.05), self.rng.uniform(-0.05, 0.05)),
                lifetime=self.rng.choice((-1.0, self.rng.uniform(90.0, 240.0))),
                intensity=self.rng.uniform(0.3, 1.0),
                id=f"storm-{self._next_storm_id}",
            )
            if obstacle_intersects_mask(candidate, self.land_mask, safety_margin=1.0):
                continue
            if any(
                candidate.distance_to_boundary((base.position.col + 0.5, base.position.row + 0.5))
                < self.config.environment.base_obstacle_clearance_cells
                for base in self.bases
            ):
                continue
            if any(
                isinstance(other, Thunderstorm)
                and math.dist(candidate.center, other.center)
                < candidate.half_extent + other.half_extent + 1.0
                for other in obstacles
            ):
                continue
            if any(
                isinstance(other, Island)
                and other.distance_to_boundary(candidate.center)
                < candidate.half_extent + 1.0
                for other in obstacles
            ):
                continue
            self._next_storm_id += 1
            return candidate
        return None

    def _update_ships(self, current_time: float) -> None:
        islands = [item for item in self.obstacles if isinstance(item, Island)]
        groups: dict[str, list[Ship]] = defaultdict(list)
        for ship in self.ships:
            if ship.group_id and not ship.departed:
                groups[ship.group_id].append(ship)
        for members in groups.values():
            # Formation leader (member 0) navigates normally; followers
            # steer to maintain their assigned station offsets.
            leader = members[0] if members else None
            for ship in members:
                if ship.is_formation_leader or len(members) == 1:
                    ship.step(self.clock.dt_min, islands)
                else:
                    ship.step(self.clock.dt_min, islands, leader=leader)
        departed_groups = {
            ship.group_id for ship in self.ships
            if ship.group_id and ship.departed
        }
        for group_id in departed_groups - self._departed_groups:
            self._departed_groups.add(group_id)
            members = [ship for ship in self.ships if ship.group_id == group_id]
            for ship in members:
                ship.departed = True
                ship.set_tracked(False)
            self.departed_ship_count += len(members)
            self._release_departed_group(group_id, current_time)

    def _refresh_ais_signals(self, current_time: float) -> None:
        """Publish physical AIS broadcasts at the configured minute cadence."""
        interval = self.config.ship.ais_update_interval_min
        if current_time - getattr(self, "_last_ais_update", float("-inf")) < interval:
            return
        for ship in self.ships:
            if not ship.departed:
                ship.set_ais_signal(generate_ais_signal(ship, current_time))
        self._last_ais_update = current_time

    def _release_departed_group(self, group_id: str, current_time: float) -> None:
        self._release_target_group(group_id, current_time, "target_departed")

    def _replan_conflicting_routes(self) -> None:
        sm = self.allocator.sm
        regions = {region.id: region for region in sm.get_active_search_regions()}
        for uav in self.uavs:
            if uav.status in ("idle", "refueling", "holding", "tracking"):
                continue
            if not self.obstacle_avoider.path_conflicts(
                uav.remaining_path,
                self.obstacle_mask,
            ):
                continue

            if self.control_coordinator.has_controller(uav.id):
                self._queue_control_event(
                    "route_blocked",
                    uav.id,
                    sm.current_time,
                    {"reason": "dynamic_obstacle"},
                )
                sm.add_event("route_blocked", {
                    "uav_id": uav.id,
                    "reason": "dynamic_obstacle",
                })
                continue

            if uav.status == "returning":
                self._set_return_route(uav, sm.current_time)
            elif uav.mission_kind in ("search",):
                state = sm.get_uav(uav.id)
                region = regions.get(state.assigned_region_id if state else None)
                if region is None:
                    self._begin_return(uav, sm.current_time)
                    continue
                try:
                    self._assign_search_route(uav, region)
                except (RuntimeError, ValueError) as exc:
                    region.status = "stale"
                    region.assigned_uav_id = None
                    sm.clear_uav_assignment(uav.id)
                    sm.add_event("route_plan_failed", {
                        "uav_id": uav.id,
                        "region_id": region.id,
                        "error": str(exc),
                    })
                    self._begin_return(uav, sm.current_time)
                    continue
            elif uav.mission_kind == "track_entry" and uav.target_group_id:
                center = self._group_center(uav.target_group_id)
                if center is None:
                    self._begin_return(uav, sm.current_time)
                    continue
                uav.start_tracking(uav.target_group_id, center)
            else:
                continue
            sm.add_event("route_replanned", {
                "uav_id": uav.id,
                "reason": "dynamic_obstacle",
            })

    def _record_storm_avoidance(self, uav: UAVEntity, current_time: float) -> None:
        if uav.status != "tracking":
            self._storm_levels.pop(uav.id, None)
            self._storm_level3_started_at.pop(uav.id, None)
            return
        level = int(uav.avoidance_level)
        previous = self._storm_levels.get(uav.id, 0)
        if level != previous:
            if level:
                self.storm_avoidance_events += 1
                self.allocator.sm.add_event("storm_avoidance", {
                    "uav_id": uav.id,
                    "level": level,
                })
            elif previous:
                self.allocator.sm.add_event("storm_avoidance_cleared", {
                    "uav_id": uav.id,
                })
            self._storm_levels[uav.id] = level
        if level == 3:
            started = self._storm_level3_started_at.setdefault(uav.id, current_time)
            if current_time - started >= 3.0:
                self._lose_target_to_storm(uav, current_time)
        else:
            self._storm_level3_started_at.pop(uav.id, None)

    def _lose_target_to_storm(self, uav: UAVEntity, current_time: float) -> None:
        """Escalate a persistent level-3 cloud cover to a real track loss."""
        group_id = uav.target_group_id
        if not group_id:
            return
        sm = self.allocator.sm
        track = sm.get_track_region_for_group(group_id)
        if track is not None:
            sm.release_track_region(track.id, uav.id, create_marker=True)
        for member in self.ships:
            if member.group_id == group_id:
                member.set_tracked(False)
        for tracker in self.uavs:
            if tracker.target_group_id != group_id:
                continue
            tracker.target_group_id = None
            sm.clear_uav_assignment(tracker.id)
            sm.update_uav_status(
                tracker.id, "idle", tracker.position,
                fuel_remaining_pct=tracker.fuel_remaining_pct,
            )
            if self.control_coordinator.has_controller(tracker.id):
                self._queue_control_event(
                    "target_lost",
                    tracker.id,
                    current_time,
                    {"group_id": group_id, "contact_id": group_id},
                )
            self._storm_levels.pop(tracker.id, None)
            self._storm_level3_started_at.pop(tracker.id, None)
        self.allocator.trigger_manager.notify_event(
            "target_lost", time=current_time, uav_id=uav.id, group_id=group_id,
        )
        sm.add_event("target_lost_storm", {
            "uav_id": uav.id,
            "group_id": group_id,
        })

    def _update_sensors_and_detections(self, current_time: float) -> None:
        sm = self.allocator.sm
        for uav in self.uavs:
            if uav.status == "searching" and uav.sar_imaging:
                footprint = uav.sar_sensor.compute_swath_footprint(
                    uav.float_position,
                    uav.heading_rad,
                    uav.sar_look_direction,
                    along_track_cells=uav.sar_along_track_cells,
                )
                uav.sar_footprint = footprint
                for cell in footprint:
                    sm.scan_cell(cell, current_time, is_track=False)
                footprint_set = set(footprint)
                for ship in self.ships:
                    if ship.detected or ship.position not in footprint_set:
                        continue
                    if self.rng.random() <= uav.sar_sensor.detection_probability:
                        self._handle_detection(uav, ship, current_time)
            elif uav.status == "searching":
                # A search route may be in its entry/exit settling segment.
                # It moves normally, but invalid SAR samples never improve
                # the information field or produce a target detection.
                uav.sar_footprint = []
            elif uav.status == "tracking" and uav.target_group_id:
                center = self._group_center(uav.target_group_id)
                if center is not None:
                    sm.scan_cell(GridCoord(int(round(center[0])), int(round(center[1]))), current_time, True)
                    self._process_ais_tracking(uav, center, current_time)
            else:
                uav.sar_footprint = []
                if uav.status != "tracking":
                    uav.eo_fov = None

    def _process_ais_tracking(
        self,
        uav: UAVEntity,
        target_position: tuple[float, float],
        current_time: float,
    ) -> None:
        """Accumulate EO fixes, then perform delayed AIS discrimination."""
        group_id = uav.target_group_id
        if group_id is None:
            return
        members = [
            ship for ship in self.ships
            if ship.group_id == group_id and not ship.departed
        ]
        if not members or all(ship.discrimination is not None for ship in members):
            return
        storms = [item for item in self.obstacles if isinstance(item, Thunderstorm)]
        measurement = uav.measure_target(target_position, storms)
        if measurement is None:
            return
        estimate = self.ais_discriminator.estimate_target_position(uav.pose, measurement)
        samples = self._ais_measurements.setdefault(uav.id, [])
        samples.append(estimate)
        if len(samples) > 12:
            del samples[:-12]
        # The scheduler receives this EO-derived estimate only.  It never
        # receives the target_position truth value used by the sensor model.
        self.allocator.sm.record_target_observation(
            group_id,
            GridCoord(int(round(estimate[0])), int(round(estimate[1]))),
            uav.id,
            current_time,
        )
        started = self._ais_tracking_started_at.setdefault(uav.id, current_time)
        if current_time - started < self.config.ship.ais_discrimination_delay_min:
            return
        estimate_median = tuple(float(np.median([point[index] for point in samples])) for index in (0, 1))
        result = self.ais_discriminator.discriminate_formation(
            [member.ais_signal for member in members],
            estimate_median,
        )
        result_data = result.to_dict()
        for member in members:
            member.is_military = result.is_military
            member.discrimination = result_data
            member.estimated_position = estimate_median
        self.ais_discriminations += 1
        self.allocator.sm.add_event("ais_discriminated", {
            "group_id": group_id,
            "uav_id": uav.id,
            **result_data,
        })
        if result.is_military:
            self.allocator.trigger_manager.notify_event(
                "target_military", time=current_time, uav_id=uav.id, group_id=group_id,
            )
            return
        self.civilian_releases += 1
        self._release_target_group(group_id, current_time, "civilian_released")

    def _release_target_group(
        self,
        group_id: str,
        current_time: float,
        event_type: str,
    ) -> None:
        """Release a civilian or departed target without creating a loss marker."""
        sm = self.allocator.sm
        sm.clear_target_report(group_id)
        for member in self.ships:
            if member.group_id == group_id:
                member.set_tracked(False)
        track = sm.get_track_region_for_group(group_id)
        if track is not None:
            sm.release_track_region(track.id, create_marker=False)
        for uav in self.uavs:
            if uav.target_group_id != group_id:
                continue
            self._tracking_started_at.pop(uav.id, None)
            self._ais_tracking_started_at.pop(uav.id, None)
            self._ais_measurements.pop(uav.id, None)
            uav.target_group_id = None
            sm.clear_uav_assignment(uav.id)
            if not self.control_coordinator.has_controller(uav.id) and not self._resume_search(uav):
                sm.update_uav_status(
                    uav.id, "idle", uav.position,
                    fuel_remaining_pct=uav.fuel_remaining_pct,
                )
            elif self.control_coordinator.has_controller(uav.id):
                self._queue_control_event(
                    event_type,
                    uav.id,
                    current_time,
                    {"group_id": group_id, "contact_id": group_id},
                )
        self.allocator.trigger_manager.notify_event(
            event_type, time=current_time, group_id=group_id,
        )
        sm.add_event(event_type, {"group_id": group_id})

    def _resume_search(self, uav: UAVEntity) -> bool:
        """Immediately assign an unclaimed active search region when present."""
        sm = self.allocator.sm
        for region in sm.get_active_search_regions():
            if region.assigned_uav_id is not None:
                continue
            region.assigned_uav_id = uav.id
            try:
                self._assign_search_route(uav, region)
            except (RuntimeError, ValueError):
                region.assigned_uav_id = None
                continue
            sm.update_uav_status(
                uav.id,
                uav.status,
                uav.position,
                assigned_region_id=region.id,
                fuel_remaining_pct=uav.fuel_remaining_pct,
            )
            return True
        return False

    def _handle_detection(self, uav: UAVEntity, ship: Ship, current_time: float) -> None:
        sm = self.allocator.sm
        if not ship.detected:
            ship.mark_detected()
            sm.add_event("ship_detected", {
                "ship_id": ship.id,
                "group_id": ship.group_id,
                "uav_id": uav.id,
                "position": ship.position,
            })
        sm.record_target_observation(
            ship.group_id,
            ship.position,
            uav.id,
            current_time,
        )
        existing = sm.get_track_region_for_group(ship.group_id)
        if existing is not None:
            return

        for region in sm.get_search_regions():
            if region.assigned_uav_id == uav.id:
                region.assigned_uav_id = None
        track = sm.create_track_region(ship.group_id, ship.position)
        track.assigned_uav_id = uav.id
        self._resolve_search_track_conflicts(
            current_time,
            protected_uav_ids={uav.id},
        )
        self.track_creations += 1
        self._tracking_started_at[uav.id] = current_time
        # Keep the legacy entity/state association for rendering and handoff
        # bookkeeping; the tracking controller is installed by the queued event.
        uav.target_group_id = ship.group_id
        for member in self.ships:
            if member.group_id == ship.group_id:
                member.set_tracked(True)
        sm.update_uav_status(
            uav.id,
            "transit",
            uav.position,
            assigned_region_id=track.id,
            target_group_id=ship.group_id,
            fuel_remaining_pct=uav.fuel_remaining_pct,
        )
        self.allocator.trigger_manager.notify_event(
            "target_found",
            time=current_time,
            uav_id=uav.id,
            group_id=ship.group_id,
            position={"col": ship.position.col, "row": ship.position.row},
        )
        self._queue_control_event(
            "target_found",
            uav.id,
            current_time,
            {
                "contact_id": ship.group_id,
                "group_id": ship.group_id,
                "position": {"col": ship.position.col, "row": ship.position.row},
            },
        )
        sm.add_event("target_found", {
            "uav_id": uav.id,
            "group_id": ship.group_id,
            "position": ship.position,
        })

    def _resolve_search_track_conflicts(
        self,
        current_time: float,
        protected_uav_ids: set[str] | None = None,
    ) -> None:
        """Retire overlapping searches and redirect their assigned UAVs."""
        protected = protected_uav_ids or set()
        retired = self.allocator.retire_search_track_conflicts()
        if not retired:
            return

        entities = {entity.id: entity for entity in self.uavs}
        for _, assigned_uav_id in retired:
            if not assigned_uav_id:
                continue
            self.allocator.sm.clear_uav_assignment(assigned_uav_id)
            if assigned_uav_id in protected:
                continue
            entity = entities.get(assigned_uav_id)
            if entity is None or entity.status in {
                "idle", "tracking", "returning", "holding", "refueling",
            }:
                continue
            if self.control_coordinator.has_controller(entity.id):
                self._queue_control_event(
                    "route_blocked",
                    entity.id,
                    current_time,
                    {"reason": "search_region_retired"},
                )
                self._begin_return(entity, current_time)
                continue
            if not self._resume_search(entity):
                self._begin_return(entity, current_time)

    def _begin_return(
        self,
        uav: UAVEntity,
        current_time: float,
        *,
        release_marker: bool = True,
    ) -> None:
        if self.control_coordinator.has_controller(uav.id):
            lease = self.control_coordinator.current_lease(uav.id)
            if lease.owner in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
                try:
                    self._request_recovery_return(
                        uav,
                        current_time,
                        "lifecycle_or_task_return",
                    )
                except NoSafeRecoveryPath as exc:
                    self._enter_emergency_failure(
                        uav,
                        "no_safe_recovery_path",
                        exc,
                    )
                return
        sm = self.allocator.sm
        self._freshness_patrol_uavs.discard(uav.id)
        self._search_started_at.pop(uav.id, None)
        self._tracking_started_at.pop(uav.id, None)
        self._ais_tracking_started_at.pop(uav.id, None)
        self._ais_measurements.pop(uav.id, None)
        if uav.target_group_id:
            report = sm.get_target_report(uav.target_group_id)
            track = sm.get_track_region_for_group(uav.target_group_id)
            if track is not None and track.assigned_uav_id == uav.id:
                sm.release_track_region(track.id, uav.id, create_marker=release_marker)
                self.allocator.trigger_manager.notify_event(
                    "target_lost", time=current_time, uav_id=uav.id,
                    group_id=uav.target_group_id,
                )
                sm.add_event("target_lost", {
                    "uav_id": uav.id,
                    "group_id": uav.target_group_id,
                })
                if report is not None:
                    sm.add_event("target_handoff_report", {
                        "uav_id": uav.id,
                        "group_id": report.group_id,
                        "position": report.position,
                        "observed_at": report.observed_at,
                    })
            for member in self.ships:
                if member.group_id == uav.target_group_id:
                    member.set_tracked(False)
        for region in sm.get_search_regions():
            if region.assigned_uav_id == uav.id:
                region.assigned_uav_id = None

        self._set_return_route(uav, current_time)
        sm.clear_uav_assignment(uav.id)
        sm.update_uav_status(uav.id, "returning", uav.position, fuel_remaining_pct=uav.fuel_remaining_pct)
        self.allocator.trigger_manager.notify_event(
            "uav_returned", time=current_time, uav_id=uav.id,
        )
        sm.add_event("uav_returned", {"uav_id": uav.id})

    def _set_return_route(self, uav: UAVEntity, current_time: float) -> None:
        accepting = self._available_recovery_bases(exclude_uav_id=uav.id)
        # Recovery doctrine: fly to the nearest base that can still maintain
        # this airframe.  Current refuelling slots and inbound reservations
        # both consume capacity, so simultaneous returns cannot overbook it.
        bases = sorted(
            accepting or self.bases,
            key=lambda base: (
                math.dist(
                    uav.float_position,
                    (base.position.col, base.position.row),
                ),
                base.id,
            ),
        )
        if not accepting:
            self.allocator.sm.add_event("no_recovery_capacity", {
                "uav_id": uav.id,
                "fallback_base_id": bases[0].id,
            })
        center = (
            (self.config.grid.resolution[0] - 1) / 2,
            (self.config.grid.resolution[1] - 1) / 2,
        )
        local_mask = self.obstacle_mask.copy()
        col, row = uav.position
        local_mask[max(0, col - 1):col + 2, max(0, row - 1):row + 2] = False
        errors = []
        best_path: tuple[float, float, BaseStation, list] | None = None
        for base_index, base in enumerate(bases):
            arrival_heading = math.atan2(
                base.position.row - center[1],
                base.position.col - center[0],
            )
            goal = (
                float(base.position.col),
                float(base.position.row),
                arrival_heading,
            )
            planners = (
                self.obstacle_avoider,
                ObstacleAvoider(
                    max_iterations=2400,
                    seed=(
                        self.seed
                        + int(current_time)
                        + len(uav.id)
                        + base_index * 101
                    ),
                ),
                # Moving storm footprints can invalidate a previously safe
                # return corridor between two frames.  Keep a deterministic
                # high-budget fallback before declaring the coastal recovery
                # route impossible; the map is small, while terminating the
                # full eight-hour run on a transient RRT* miss is not safe.
                ObstacleAvoider(
                    max_iterations=8000,
                    seed=(
                        self.seed
                        + int(current_time) * 17
                        + len(uav.id) * 31
                        + base_index * 1009
                    ),
                ),
            )
            for planner in planners:
                try:
                    path = planner.plan_path(
                        uav.pose,
                        goal,
                        local_mask,
                        uav.R_min,
                    )
                except (RuntimeError, ValueError) as exc:
                    errors.append(str(exc))
                    continue
                length = sum(
                    math.dist(start[:2], end[:2])
                    for start, end in zip(path, path[1:])
                )
                if length > uav.remaining_range_cells:
                    errors.append(f"{base.id}: insufficient fuel for {length:.2f} cells")
                    break
                candidate_key = (
                    math.dist(
                        uav.float_position,
                        (base.position.col, base.position.row),
                    ),
                    length,
                )
                if best_path is None or candidate_key < best_path[:2]:
                    best_path = (*candidate_key, base, path)
                break
        if best_path is not None:
            _, _, base, path = best_path
            self._return_base_by_uav[uav.id] = base
            uav.plan_return(path)
            return
        raise RuntimeError(
            f"no land recovery base has a safe return path for {uav.id}: "
            + "; ".join(errors)
        )

    def _base_maintenance_load(
        self,
        base: BaseStation,
        *,
        exclude_uav_id: str | None = None,
    ) -> int:
        """Concurrent maintenance load, including inbound reservations."""
        inbound = sum(
            assigned is base
            and uav_id != exclude_uav_id
            and not base.is_refueling(uav_id)
            for uav_id, assigned in self._return_base_by_uav.items()
        )
        return base.occupancy + inbound

    def _available_recovery_bases(
        self,
        *,
        exclude_uav_id: str | None = None,
    ) -> list[BaseStation]:
        return [
            base for base in self.bases
            if self._base_maintenance_load(
                base,
                exclude_uav_id=exclude_uav_id,
            ) < base.capacity
        ]

    def _nearest_available_base(
        self,
        position,
        *,
        exclude_uav_id: str | None = None,
    ) -> BaseStation | None:
        available = self._available_recovery_bases(
            exclude_uav_id=exclude_uav_id,
        )
        if not available:
            return None
        return min(
            available,
            key=lambda base: math.dist(
                position,
                (base.position.col, base.position.row),
            ),
        )

    def _process_search_completions(self, current_time: float) -> None:
        sm = self.allocator.sm
        for uav in self.uavs:
            if not uav.search_complete_pending:
                continue
            region_id = sm.get_uav(uav.id).assigned_region_id if sm.get_uav(uav.id) else None
            assigned_region = None
            for region in sm.get_search_regions():
                if region.id == region_id:
                    assigned_region = region
                    break

            # A completed first-pass region is a valid persistent patrol cell
            # once broad coverage exists.  Retain the real LLM-approved
            # partition and restart SAR locally, avoiding an idle interval and
            # a fresh cross-map transit solely to revisit stale information.
            if (
                assigned_region is not None
                and self._should_continue_freshness_patrol(uav, current_time)
                and not self._needs_reserve_return(uav, include_idle=True)
            ):
                try:
                    uav.search_complete_pending = False
                    assigned_region.status = "active"
                    assigned_region.assigned_uav_id = uav.id
                    self._assign_search_route(
                        uav,
                        assigned_region,
                        allow_revisit=True,
                    )
                    sm.update_uav_status(
                        uav.id,
                        uav.status,
                        uav.position,
                        assigned_region_id=assigned_region.id,
                        fuel_remaining_pct=uav.fuel_remaining_pct,
                    )
                    sm.add_event("search_revisit_started", {
                        "uav_id": uav.id,
                        "region_id": assigned_region.id,
                    })
                    continue
                except (RuntimeError, ValueError) as exc:
                    self._freshness_patrol_uavs.discard(uav.id)
                    sm.add_event("revisit_route_plan_failed", {
                        "uav_id": uav.id,
                        "region_id": assigned_region.id,
                        "error": str(exc),
                    })

            if assigned_region is not None:
                assigned_region.status = "completed"
                assigned_region.completion_pct = 100.0
                assigned_region.assigned_uav_id = None
            self.allocator.trigger_manager.notify_event(
                "search_complete", time=current_time, uav_id=uav.id, region_id=region_id,
            )
            sm.add_event("search_complete", {"uav_id": uav.id, "region_id": region_id})
            uav.search_complete_pending = False
            uav.completed_searches_since_refuel += 1
            sm.clear_uav_assignment(uav.id)
            if (
                (
                    self._lifecycle_mode
                    and self.lifecycle_cycles[uav.id]
                    < self.config.uav.lifecycle_required_cycles
                )
                or self._needs_reserve_return(uav, include_idle=True)
            ):
                self._begin_return(uav, current_time)

    def _should_continue_freshness_patrol(
        self,
        uav: UAVEntity,
        current_time: float,
    ) -> bool:
        cfg = self.config.uav
        if current_time < cfg.freshness_patrol_start_min:
            return False
        coverage = self.allocator.sm.get_coverage_stats()["coverage_pct"]
        if coverage < cfg.freshness_patrol_coverage_threshold_pct:
            return False
        if uav.id in self._freshness_patrol_uavs:
            return True
        if len(self._freshness_patrol_uavs) >= cfg.freshness_patrol_count:
            return False
        self._freshness_patrol_uavs.add(uav.id)
        return True

    def _update_lifecycle_mode(self, current_time: float) -> None:
        """Start base rotations after the four-hour coverage gate is secure."""
        if self._lifecycle_mode or self._lifecycle_completed:
            return
        cfg = self.config.uav
        coverage = self.allocator.sm.get_coverage_stats()["coverage_pct"]
        if (
            current_time < cfg.lifecycle_rotation_start_min
            or coverage < cfg.lifecycle_coverage_threshold_pct
        ):
            return
        self._lifecycle_mode = True
        sm = self.allocator.sm
        sm.lifecycle_mode = True
        for region in sm.get_search_regions():
            region.status = "stale"
            region.assigned_uav_id = None
        sm.set_search_regions([])
        sm.add_event("lifecycle_rotation_started", {
            "coverage_pct": coverage,
            "required_cycles": cfg.lifecycle_required_cycles,
        })
        for uav in self.uavs:
            sm.clear_uav_assignment(uav.id)
            if (
                self._sortie_searched[uav.id]
                and uav.status not in ("returning", "refueling")
            ):
                self._begin_return(uav, current_time)

    def _needs_reserve_return(self, uav: UAVEntity, include_idle: bool = False) -> bool:
        if uav.status in ("returning", "refueling", "holding"):
            return False
        if uav.status == "idle" and not include_idle:
            return False
        base = self._nearest_available_base(
            uav.float_position,
            exclude_uav_id=uav.id,
        )
        if base is None:
            # All bases are currently full.  Returning to the closest coast
            # minimizes fuel risk; the UAV enters that base's holding pattern
            # until a maintenance position opens.
            base = self._nearest_base(uav.float_position)
        direct_home = math.dist(
            uav.float_position,
            (base.position.col, base.position.row),
        )
        usable_remaining_range = uav.remaining_range_cells * 0.95
        # The user-facing rule is measured to the closest base by direct
        # distance.  A fixed-wing aircraft cannot fly that chord instantly:
        # it needs a curvature-constrained turn-in and may need to clear a
        # no-fly cell.  Triggering only at the chord threshold can leave too
        # little fuel for a legal Dubins return route.  This guard may make
        # the UAV return earlier, never later, than the required 95% rule.
        # One turn-in is needed to commit to the return course and another
        # can be required when a moving storm invalidates that course.  The
        # four-cell clearance covers the configured small storm plus the
        # obstacle avoider's one-cell safety envelope.
        turn_and_clearance = 2.0 * math.pi * uav.R_min + 4.0
        navigable_home = direct_home + turn_and_clearance
        return (
            direct_home > usable_remaining_range
            or navigable_home > usable_remaining_range
        )

    def _process_refuelling(self, current_time: float) -> None:
        for uav in self.uavs:
            if uav.status != "refueling":
                continue
            base = self._return_base_by_uav.get(
                uav.id,
                self._nearest_base(uav.float_position),
            )
            if not base.is_refueling(uav.id) and not base.land_uav(uav.id):
                self._holding_base_by_uav[uav.id] = base
                uav.start_holding(base.position)
                self.allocator.sm.add_event("base_capacity_full", {
                    "uav_id": uav.id,
                    "base_id": base.id,
                    "occupancy": base.occupancy,
                    "capacity": base.capacity,
                })
                self.allocator.trigger_manager.notify_event(
                    "base_capacity_full",
                    time=current_time,
                    uav_id=uav.id,
                    base_id=base.id,
                )
        for base in self.bases:
            for uav_id in base.step(self.clock.dt_min):
                uav = next(item for item in self.uavs if item.id == uav_id)
                uav.position = base.position
                uav.base_position = base.position
                uav.refuel()
                self._return_base_by_uav.pop(uav.id, None)
                if self.control_coordinator.has_controller(uav.id):
                    self.control_coordinator.reset_after_refuel(
                        uav.id,
                        current_time=current_time,
                    )
                    self._next_sortie_number[uav.id] += 1
                    if (
                        self.control_coordinator.configured_mode(uav.id)
                        is not ControlMode.HEURISTIC
                    ):
                        self.control_coordinator.start_work(
                            uav.id,
                            sortie_number=self._next_sortie_number[uav.id],
                            current_time=current_time,
                            dt_min=self.clock.dt_min,
                        )
                    self._coordinator_tasks.pop(uav.id, None)
                if self._sortie_searched[uav.id]:
                    self.lifecycle_cycles[uav.id] += 1
                self._sortie_searched[uav.id] = False
                self.allocator.sm.clear_uav_assignment(uav.id)
                self.allocator.trigger_manager.notify_event(
                    "uav_refueled", time=current_time, uav_id=uav.id,
                )
                self.allocator.sm.add_event("uav_refueled", {
                    "uav_id": uav.id,
                    "base_id": base.id,
                    "base_position": base.position,
                })
        for uav in self.uavs:
            if uav.status != "holding":
                continue
            base = self._holding_base_by_uav.get(uav.id, self._nearest_base(uav.float_position))
            if not base.can_accept():
                continue
            if base.land_uav(uav.id):
                uav.position = base.position
                uav.status = "refueling"
                uav.sensor_mode = "off"
                self._return_base_by_uav[uav.id] = base
                self._holding_base_by_uav.pop(uav.id, None)
                self.allocator.sm.add_event("holding_released", {
                    "uav_id": uav.id,
                    "base_id": base.id,
                })
        if (
            self._lifecycle_mode
            and min(self.lifecycle_cycles.values(), default=0)
            >= self.config.uav.lifecycle_required_cycles
        ):
            self._lifecycle_mode = False
            self._lifecycle_completed = True
            self.allocator.sm.lifecycle_mode = False
            self.allocator.trigger_manager.notify_event(
                "lifecycle_completed",
                time=current_time,
            )
            self.allocator.sm.add_event("lifecycle_completed", {
                "cycles": dict(self.lifecycle_cycles),
            })

    def _nearest_base(self, position) -> BaseStation:
        return min(
            self.bases,
            key=lambda base: math.dist(
                position,
                (base.position.col, base.position.row),
            ),
        )

    def _sync_state_from_entities(self) -> None:
        sm = self.allocator.sm
        for entity in self.uavs:
            state = sm.get_uav(entity.id)
            sm.update_uav_status(
                entity.id,
                entity.status,
                entity.position,
                assigned_region_id=state.assigned_region_id if state else None,
                fuel_remaining_pct=entity.fuel_remaining_pct,
                target_group_id=entity.target_group_id,
                heading_deg=entity.heading_deg,
                sensor_mode=entity.sensor_mode,
            )
            lease = self.control_coordinator.current_lease(entity.id)
            sm.update_uav_control(
                entity.id,
                self.control_coordinator.configured_mode(entity.id).value,
                lease.owner.value,
                self.control_coordinator.operation_mode(entity.id).value,
                lease.generation,
                self.control_coordinator.safety_intervened(entity.id),
            )
        for track in sm.get_track_regions():
            center = self._group_center(track.target_group_id)
            if center:
                sm.update_track_region_center(
                    track.id, GridCoord(int(round(center[0])), int(round(center[1])))
                )
        self._resolve_search_track_conflicts(sm.current_time)

    def _sync_assignments(self) -> None:
        sm = self.allocator.sm
        region_by_id = {region.id: region for region in sm.get_search_regions()}
        entity_by_id = {uav.id: uav for uav in self.uavs}
        assignments = []
        for state in sm.get_all_uavs():
            entity = entity_by_id[state.id]
            if (
                state.status != "transit"
                or not state.assigned_region_id
                or entity.status not in ("idle", "holding")
            ):
                continue
            if (
                self.control_coordinator.configured_mode(entity.id)
                is not ControlMode.HEURISTIC
            ):
                continue
            region = region_by_id.get(state.assigned_region_id)
            if region is None:
                continue
            assignments.append((entity, region, self._search_route_request(entity, region)))

        if not assignments:
            return

        plans: dict[str, SearchRoutePlan] = {}
        errors: dict[str, Exception] = {}
        if len(assignments) == 1:
            entity, _, request = assignments[0]
            try:
                plans[entity.id] = plan_search_route(request)
            except Exception as exc:
                errors[entity.id] = exc
        else:
            # Workers receive only immutable route-planning snapshots.  The
            # main process remains the sole owner of UAV/StateManager state.
            with ProcessPoolExecutor(max_workers=min(4, len(assignments))) as executor:
                futures = {
                    executor.submit(plan_search_route, request): entity.id
                    for entity, _, request in assignments
                }
                for future in as_completed(futures):
                    uav_id = futures[future]
                    try:
                        plans[uav_id] = future.result()
                    except Exception as exc:
                        errors[uav_id] = exc

        for entity, region, _ in assignments:
            try:
                if entity.id in errors:
                    raise errors[entity.id]
                self._apply_search_route_plan(entity, region, plans[entity.id])
                if entity.mission_kind == "search":
                    self._install_coverage_task(entity, region, sm.current_time)
            except Exception as exc:
                region.status = "stale"
                region.assigned_uav_id = None
                sm.clear_uav_assignment(entity.id)
                entity.status = "idle"
                sm.add_event("route_plan_failed", {
                    "uav_id": entity.id,
                    "region_id": region.id,
                    "error": str(exc),
                })

    def _install_coverage_task(
        self, uav: UAVEntity, region: Region, current_time: float
    ) -> None:
        task = ControlTask(
            region.id,
            OperationMode.COVERAGE,
            region_bbox=region.bbox,
        )
        lease = self.control_coordinator.current_lease(uav.id)
        active = self.control_coordinator.active_task(uav.id)
        if (
            self.control_coordinator.has_controller(uav.id)
            and active == task
            and lease.owner is ControlOwner.HEURISTIC
        ):
            self._coordinator_tasks[uav.id] = task
            return
        if not self.control_coordinator.has_controller(uav.id):
            self.control_coordinator.start_work(
                uav.id,
                sortie_number=self._next_sortie_number[uav.id],
                current_time=current_time,
                dt_min=self.clock.dt_min,
                task=task,
            )
        elif lease.owner is ControlOwner.SYSTEM:
            self.control_coordinator.assign_task(
                uav.id,
                task,
                current_time=current_time,
            )
        elif lease.owner is ControlOwner.HEURISTIC and active != task:
            self.control_coordinator.assign_task(
                uav.id,
                task,
                current_time=current_time,
            )
        else:
            return
        self._coordinator_tasks[uav.id] = task

    def _assign_search_route(
        self,
        uav: UAVEntity,
        region,
        *,
        allow_revisit: bool = False,
        direction: str | None = None,
    ) -> None:
        request = self._search_route_request(
            uav, region, allow_revisit=allow_revisit, direction=direction,
        )
        self._apply_search_route_plan(uav, region, plan_search_route(request))

    def _search_route_request(
        self,
        uav: UAVEntity,
        region,
        *,
        allow_revisit: bool = False,
        direction: str | None = None,
    ) -> SearchRouteRequest:
        swath_width = self.config.sensor.sar.swath_km / self.config.grid.cell_size_km
        scan_times = self.allocator.sm.info_field.last_scan_time
        coverage_pct = self.allocator.sm.get_coverage_stats()["coverage_pct"]
        numeric_id = int("".join(char for char in uav.id if char.isdigit()) or 0)
        return SearchRouteRequest(
            uav_id=uav.id,
            start_pose=uav.pose,
            bbox=tuple(region.bbox),
            swath_width=swath_width,
            r_min=uav.R_min,
            obstacle_mask=np.asarray(self.obstacle_mask, dtype=bool).copy(),
            unscanned_mask=~np.isfinite(scan_times),
            allow_revisit=allow_revisit or coverage_pct >= 80.0,
            direction=direction,
            seed=self.seed + numeric_id * 997,
        )

    def _apply_search_route_plan(
        self,
        uav: UAVEntity,
        region,
        plan: SearchRoutePlan,
    ) -> None:
        if not plan.scanned_swath_count:
            region.status = "completed"
            region.completion_pct = 100.0
            region.assigned_uav_id = None
            self.allocator.sm.clear_uav_assignment(uav.id)
            uav.status = "idle"
            return
        uav.assign_mission(
            region.bbox,
            plan.path,
            transit_end_index=plan.transit_end_index,
            scan_ranges=plan.scan_ranges,
        )

    def _group_center(self, group_id: str | None):
        members = [
            ship for ship in self.ships
            if ship.group_id == group_id and not ship.departed
        ]
        if not members:
            return None
        return (
            sum(ship.float_position[0] for ship in members) / len(members),
            sum(ship.float_position[1] for ship in members) / len(members),
        )

    def _tracking_speed_commands(self) -> dict[str, float]:
        """Apply cooperative phase spacing to UAVs sharing an orbit."""
        commands: dict[str, float] = {}
        groups = {
            uav.target_group_id
            for uav in self.uavs
            if uav.status == "tracking" and uav.target_group_id
        }
        nominal = (
            self.config.uav.cruise_speed_kmh
            / self.config.grid.cell_size_km
            / 60.0
        )
        for group_id in groups:
            members = [
                uav for uav in self.uavs
                if uav.status == "tracking" and uav.target_group_id == group_id
            ]
            if len(members) < 2:
                continue
            center = self._group_center(group_id)
            if center is None:
                continue
            phase_errors = self.phase_coordinator.compute_phase_offsets(
                [{"position": uav.float_position} for uav in members],
                center,
            )
            speeds = self.phase_coordinator.adjust_airspeeds(
                phase_errors,
                nominal,
            )
            commands.update(
                (uav.id, speed) for uav, speed in zip(members, speeds)
            )
        return commands

    def _detect_and_resolve_path_conflicts(self, current_time: float) -> None:
        """Detect conflicts and make the lower numeric UAV ID yield continuously."""
        uav_dicts = [
            {
                "id": uav.id,
                "status": uav.status,
                "planned_path": [
                    list(pose) for pose in uav.remaining_path[:60]
                ],
            }
            for uav in self.uavs
        ]
        conflicts = detect_conflicts(
            uav_dicts,
            cell_size_km=self.config.grid.cell_size_km,
            time_horizon_steps=30,
            min_separation_cells=0.5,
        )
        if not conflicts:
            return

        entities = {uav.id: uav for uav in self.uavs}
        to_replan = resolve_conflicts(conflicts, entities)

        sm = self.allocator.sm
        search_regions = {
            region.id: region for region in sm.get_active_search_regions()
        }
        for uav_id in to_replan:
            uav = entities.get(uav_id)
            if uav is None or uav.status in ("idle", "refueling", "holding", "returning"):
                continue
            if self.control_coordinator.has_controller(uav.id):
                self._queue_control_event(
                    "route_blocked",
                    uav.id,
                    current_time,
                    {"reason": "path_conflict"},
                )
                sm.add_event("route_blocked", {
                    "uav_id": uav.id,
                    "reason": "path_conflict",
                })
                continue
            conflicting_uavs = [
                c.uav_b if c.uav_a == uav_id else c.uav_a
                for c in conflicts
                if uav_id in (c.uav_a, c.uav_b)
            ]
            yield_to = max(conflicting_uavs, key=uav_id_priority)
            sm.add_event("path_conflict_resolved", {
                "uav_id": uav_id,
                "yield_to": yield_to,
                "priority_rule": "higher_numeric_uav_id_keeps_trajectory",
                "conflicts": [
                    {"with": c.uav_b if c.uav_a == uav_id else c.uav_a,
                     "cell": list(c.cell),
                     "offset": c.step_offset_a if c.uav_a == uav_id else c.step_offset_b}
                    for c in conflicts
                    if uav_id in (c.uav_a, c.uav_b)
                ],
            })
            # Replan only the yielding UAV.  Searching airframes switch the
            # coverage orientation, producing a continuous Dubins/RRT* route
            # that retains their task without stopping or teleporting.
            state = sm.get_uav(uav_id)
            region = search_regions.get(
                state.assigned_region_id if state is not None else None
            )
            if uav.mission_kind == "search" and region is not None:
                try:
                    width = region.bbox.col_end - region.bbox.col_start
                    height = region.bbox.row_end - region.bbox.row_start
                    alternate_direction = "vertical" if width >= height else "horizontal"
                    self._assign_search_route(
                        uav,
                        region,
                        allow_revisit=True,
                        direction=alternate_direction,
                    )
                except (RuntimeError, ValueError):
                    sm.add_event("conflict_replan_failed", {
                        "uav_id": uav_id,
                        "region_id": region.id if hasattr(region, "id") else "unknown",
                    })
            elif uav.mission_kind == "track_entry" and uav.target_group_id:
                center = self._group_center(uav.target_group_id)
                if center is not None:
                    uav.start_tracking(uav.target_group_id, center)
            else:
                # Returning airframes keep their fuel-safe route; other
                # unsupported mission states are recorded but never mutated
                # with a zero-length waypoint that would not delay motion.
                sm.add_event("conflict_replan_deferred", {
                    "uav_id": uav_id,
                })

    def _record_statuses(self) -> None:
        for uav in self.uavs:
            history = self.status_history[uav.id]
            if history[-1] != uav.status:
                history.append(uav.status)


__all__ = ["SimulationEngine"]
