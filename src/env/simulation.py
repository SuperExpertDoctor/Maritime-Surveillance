"""Headless simulation engine shared by the CLI, tests, and web service."""
from __future__ import annotations

import math
import random
import time
import hashlib
from uuid import uuid4
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

import numpy as np

from src.env.base_station import BaseStation
from src.env.ais_signal import generate_ais_signal
from src.env.emitter import RadarEmitter
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
from src.env.ship import Ship, create_ship_population
from src.mission.opponent_population import OpponentPopulation
from src.env.sim_clock import SimClock
from src.env.uav_entity import UAVEntity
from src.sensor.passive import PassivePositionResolver, PassiveSensor
from src.control.common.contracts import (
    ActionSpec,
    BaseObservation,
    ControlEvent,
    ControlMode,
    ControlOwner,
    ControlTask,
    CoverageExecutionConfig,
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
from src.control.common.safety import (
    ProbeValidationError,
    SafetyEnvelope,
    UnsafeControlState,
)
from src.control.heuristic.navigation import AStarNavigator
from src.control.heuristic.return_to_base import (
    NoSafeRecoveryPath,
    RecoveryPlanner,
    recovery_route_blocked,
)
from src.schedule.config_loader import AppConfig
from src.schedule.datatypes import BBox, GridCoord, Region
from src.schedule.task_allocator import TaskAllocator
from src.mission.contracts import (
    AssignmentBatch,
    CommandResult,
    ContactSnapshot,
    CovarianceKernel,
    EvidenceRecord,
    PassiveBearingObservation,
    PassivePosition,
    PointKernel,
    ProbeSession,
    TaskCandidate,
    TaskRecord,
    VisualDetection,
    VesselCommand,
)
from src.mission.contact_assessor import ContactAssessor
from src.mission.coverage_service import CoverageCompletion, CoverageService
from src.mission.evasion_detector import EvasionDetector
from src.mission.handoff import HandoffManager
from src.mission.outcome_evaluator import (
    EvaluationTick,
    OutcomeEvaluator,
    VesselTruthSample,
)
from src.mission.intent_commands import (
    IntentCommandQueue,
    RuntimeCommandQueue,
)
from src.mission.intent_store import IntentStore
from src.mission.vessel_commands import VesselCommandQueue, VesselCommandResult
from src.mission.trajectory_features import advance_probe, build_features
from src.mission.red_commander import (
    RedCommander,
    RedDecisionBlocked,
    RedShipSnapshot,
    RedSnapshot,
    ThreatGate,
)
from src.mission.surveillance_stage import SurveillanceStageRegistry
from src.mission.strategy_memory import StrategyMemoryStore
from src.utils.coverage_planner import CoveragePlanner
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


_SEARCH_TASK_KINDS = frozenset({"search", "direction_search", "investigation"})


@dataclass(frozen=True)
class _VesselRemovalPlan:
    vessel_id: str
    revision: int
    contact_ids: tuple[str, ...]
    assigned_uav_ids: tuple[str, ...]
    handoff_ids: tuple[str, ...]


class SimulationEngine:
    def __init__(
        self,
        config: AppConfig,
        seed: int = 42,
        *,
        control_providers: Mapping[ControlMode | str, ControlProvider] | None = None,
        llm_gateway=None,
        episode_id: str | None = None,
        strategy_memory_store=None,
        strategy_memory_version: str | None = None,
    ):
        self.config = config
        self.seed = seed
        self.episode_id = episode_id or f"episode-{uuid4().hex}"
        self._llm_gateway = llm_gateway
        self._control_providers = dict(control_providers or {})
        self.reset_generation = 0
        memory_store = strategy_memory_store or StrategyMemoryStore()
        resolved_memory_version = memory_store.resolve_version(
            "baseline" if strategy_memory_version is None else strategy_memory_version
        )
        self.rng = random.Random(seed)
        self.clock = SimClock()
        self.vessel_commands = VesselCommandQueue(
            maxsize=config.mission.intent.mutation_queue_limit,
        )
        self._vessel_revisions: dict[str, int] = {}
        self._next_scenario_vessel_number = 1
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
        self.allocator = TaskAllocator(
            config,
            llm_gateway=llm_gateway,
            strategy_memory_store=memory_store,
        )
        self.allocator.set_strategy_memory_version(resolved_memory_version)
        if llm_gateway is None:
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
        island_mask = obstacle_grid_mask(
            [item for item in self.obstacles if isinstance(item, Island)],
            config.grid.resolution,
            include_islands=True,
        )
        self.ship_land_mask = np.logical_or(self.land_mask, island_mask)
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
        self.allocator.sm.episode_id = self.episode_id
        self.allocator.sm.configure_coverage_metrics(
            self._intent_searchable_mask(), self.episode_id,
        )
        self.allocator.sm.configure_coverage_service(
            self._intent_searchable_mask(),
        )
        self.intents = IntentStore(
            self._intent_searchable_mask(), config.mission.intent,
        )
        self.intent_commands = IntentCommandQueue(
            config.mission.intent.mutation_queue_limit,
        )
        # Runtime commands have the same bounded capacity but never share the
        # intent idempotency namespace.
        self.runtime_commands = RuntimeCommandQueue(
            config.mission.intent.mutation_queue_limit,
        )
        self._evaluation_contact_links: dict[str, str] = {}
        self._outcome_evaluator = OutcomeEvaluator(self.episode_id)
        self.handoff_manager = HandoffManager()
        self.allocator.sm.handoff_manager = self.handoff_manager
        self.evasion_detector = EvasionDetector(
            config.mission.evasion,
            cell_size_km=config.grid.cell_size_km,
        )
        self._ais_history: dict[str, list[tuple[float, tuple[float, float], str]]] = defaultdict(list)
        self._ais_force_refresh_ids: set[str] = set()
        self._vessel_contact_ids: dict[str, set[str]] = defaultdict(set)
        self._evasion_observer_history: list[dict] = []

        self.uavs = [
            UAVEntity(
                f"UAV-{index + 1}",
                self.bases[index % len(self.bases)].position,
                config.uav.sortie_endurance_h,
                config.uav.cruise_speed_kmh,
                cell_size_km=config.grid.cell_size_km,
                R_min=1.0,
            )
            for index in range(config.uav.count)
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
        coverage_execution = self._coverage_execution_config()
        self._uav_position_history: dict[str, list[tuple[float, tuple[float, float]]]] = {
            uav.id: [(0.0, uav.float_position)] for uav in self.uavs
        }

        self._control_event_sequence = 1
        self._return_reservation_sequence = 1
        self._coordinator_tasks: dict[str, ControlTask] = {}
        self._mission_task_records: dict[str, TaskRecord] = {}
        self._coverage_assignment_generations: dict[tuple[str, str], int] = {}
        self._pending_coverage_completions: list[dict[str, object]] = []
        self._next_probe_number = 1
        self._next_sortie_number: dict[str, int] = {
            uav.id: 1 for uav in self.uavs
        }
        self._emergency_failures: dict[str, str] = {}
        self._active_path_conflicts: set[tuple[str, str]] = set()
        self._control_runtime_enabled = True
        action_spec = self._control_action_spec()
        self.control_ownership = ControlOwnership(tuple(uav.id for uav in self.uavs))
        self.observation_provider = ObservationProvider(config)
        self.safety_envelope = SafetyEnvelope(action_spec)
        self.dynamics_executor = UAVDynamicsExecutor(coverage_execution)
        self.operation_registry = OperationRegistry(self.allocator.sm)
        self.control_factory = ControlFactory(
            config.control,
            action_spec=action_spec,
            contact_config=config.mission.contact,
            coverage_execution=coverage_execution,
            coverage_config=config.mission.coverage,
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
        heuristic = config.control.heuristic
        self.ship_navigator = AStarNavigator(
            xy_resolution=heuristic.astar_xy_resolution_cells,
            heading_bins=heuristic.astar_heading_bins,
            candidate_limit=heuristic.astar_candidate_limit,
            primitive_length=heuristic.astar_primitive_length_cells,
            sample_step=heuristic.path_sample_step_cells,
        )
        self.ships = self._create_ships()
        self._emitter_track_ids = {
            ship.id: f"EMITTER-{index + 1:03d}"
            for index, ship in enumerate(self.ships)
            if ship.radar_emitter is not None
        }
        self.passive_sensors = {
            uav.id: PassiveSensor(
                config.sensor.passive,
                seed=int.from_bytes(
                    hashlib.sha256(
                        f"{seed}:passive:{uav.id}".encode("ascii")
                    ).digest()[:8],
                    "big",
                ),
            )
            for uav in self.uavs
        }
        self._passive_position_resolver = PassivePositionResolver(
            association_radius_cells=config.sensor.passive.position_association_radius_cells,
            detection_range_cells=config.sensor.passive.detection_range_cells,
        )
        self._next_passive_sample_min = float(config.sensor.passive.measurement_interval_min)
        self._passive_sample_index = 0
        self._ship_position_history: dict[str, list[tuple[float, tuple[float, float]]]] = {
            ship.id: [(0.0, ship.float_position)] for ship in self.ships
        }
        self.surveillance_stages = SurveillanceStageRegistry()
        for ship in self.ships:
            self.surveillance_stages.register(
                ship.id, ship.vessel_class, 0.0,
            )
        self._vessel_revisions = {ship.id: 1 for ship in self.ships}
        self.red_commander = RedCommander(
            self.allocator.llm_client.gateway,
            config.ship,
            threat_gate=ThreatGate(config.ship),
        )
        self.contact_assessor = ContactAssessor(
            gateway=self.allocator.llm_client.gateway,
            config=config.mission.contact,
        )
        self._red_snapshot_sequence = 0
        self._installed_red_plan_id = None
        self.opponent_population = OpponentPopulation(
            config.ship.opponent_population, seed=self.seed,
            start_time=self.clock.time,
        )
        self._refresh_ais_signals(0.0)
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
        self._departed_contacts: set[str] = set()
        self.last_result: dict = {"trigger_type": "none", "action": None}
        self._runtime_status = "running"
        self._blocked_role: str | None = None
        self._decision_failure_streak = 0
        self._mission_invariant_failure_signatures: set[tuple[str, ...]] = set()
        self._retired_command_results: dict[str, CommandResult] = {}
        self._publish_runtime_state()

    def _intent_searchable_mask(self) -> np.ndarray:
        """The entire task grid is the fixed reconnaissance responsibility.

        Vessel terrain and aircraft maneuver constraints must not shrink the
        mission area or its coverage denominator.
        """
        return np.ones(self.config.grid.resolution, dtype=bool)

    @property
    def runtime_status(self) -> str:
        """Read-only lifecycle status exposed to API and operator views."""
        return getattr(self, "_runtime_status", "running")

    @property
    def editing_allowed(self) -> bool:
        return self.vessel_mutation_allowed

    @property
    def vessel_mutation_allowed(self) -> bool:
        """Whether live vessel commands may be accepted by the engine."""
        return self.runtime_status != "finished"

    def vessel_command_result(self, command_id: str):
        return self.vessel_commands.get(command_id)

    def scenario_vessels(self) -> tuple[dict, ...]:
        return tuple({
            "scenario_entity_id": ship.id,
            "revision": self._vessel_revisions.get(ship.id, 1),
            "position": [float(ship.float_position[0]), float(ship.float_position[1])],
            "vessel_class": ship.vessel_class,
            "ais_enabled": ship.ais_enabled,
            "ais_controllable": ship.vessel_class == "type_ii",
            "surveillance_stage": self.surveillance_stages.snapshot(ship.id).stage,
        } for ship in self.ships)

    def apply_pending_vessel_commands(self) -> tuple[VesselCommandResult, ...]:
        """Apply queued scenario edits on the simulation thread only."""
        results: list[VesselCommandResult] = []
        for command in self.vessel_commands.drain():
            if command.episode_id != self.episode_id:
                result = VesselCommandResult(
                    command.command_id, "rejected", command.vessel_id, None,
                    "episode_conflict",
                )
            elif not self.vessel_mutation_allowed:
                result = VesselCommandResult(
                    command.command_id, "rejected", command.vessel_id, None,
                    "mutation_closed",
                )
            else:
                try:
                    if command.operation == "create":
                        if (
                            self.opponent_population.owns_command(command.command_id)
                            and sum(not ship.departed for ship in self.ships)
                            >= self.opponent_population.config.max_active
                        ):
                            raise ValueError("opponent_capacity_reached")
                        vessel = self._create_scenario_vessel(command)
                        self.ships.append(vessel)
                        self._vessel_revisions[vessel.id] = 1
                        self.surveillance_stages.register(
                            vessel.id, vessel.vessel_class, self.clock.time,
                        )
                        self._ship_position_history[vessel.id] = [
                            (self.clock.time, vessel.float_position)
                        ]
                        if vessel.radar_emitter is not None:
                            self._emitter_track_ids[vessel.id] = (
                                f"EMITTER-{vessel.id}"
                            )
                        self.allocator.sm.add_event("vessel_created", {
                            "vessel_id": vessel.id,
                            "vessel_class": vessel.vessel_class,
                            "position": list(vessel.float_position),
                            "ais_enabled": vessel.ais_enabled,
                        })
                        result = VesselCommandResult(
                            command.command_id, "applied", vessel.id, 1, None,
                        )
                    elif command.operation == "set_ais":
                        result = self._apply_set_ais(command)
                    else:
                        removal = self._plan_vessel_removal(command)
                        self._commit_vessel_removal(removal)
                        result = VesselCommandResult(
                            command.command_id, "applied", removal.vessel_id,
                            removal.revision + 1, None,
                        )
                except ValueError as exc:
                    code = str(exc)
                    current_revision = self._vessel_revisions.get(command.vessel_id)
                    result = VesselCommandResult(
                        command.command_id, "rejected", command.vessel_id,
                        current_revision, code,
                    )
            self.vessel_commands.complete(result)
            results.append(result)
        return tuple(results)

    def _require_vessel_revision(self, vessel_id: str, expected_revision: int) -> Ship:
        vessel = next((item for item in self.ships if item.id == vessel_id), None)
        if vessel is None:
            raise ValueError("vessel_not_found")
        revision = self._vessel_revisions.get(vessel_id, 0)
        if revision != expected_revision:
            raise ValueError("revision_conflict")
        return vessel

    def _apply_set_ais(self, command: VesselCommand) -> VesselCommandResult:
        if type(command.ais_enabled) is not bool:
            raise ValueError("ais_enabled must be bool")
        vessel = self._require_vessel_revision(
            command.vessel_id, command.expected_revision,
        )
        vessel.set_ais_enabled(command.ais_enabled)
        revision = command.expected_revision + 1
        self._vessel_revisions[vessel.id] = revision
        if command.ais_enabled:
            self._ais_force_refresh_ids.add(vessel.id)
        else:
            vessel.set_ais_signal(None)
        self.allocator.trigger_manager.notify_event(
            "ais_transmission_changed",
            time=self.clock.time,
            vessel_id=vessel.id,
            ais_enabled=command.ais_enabled,
        )
        self.allocator.sm.add_event("ais_transmission_changed", {
            "vessel_id": vessel.id,
            "ais_enabled": command.ais_enabled,
            "revision": revision,
        })
        return VesselCommandResult(
            command.command_id, "applied", vessel.id, revision, None,
        )

    def _plan_vessel_removal(self, command: VesselCommand) -> _VesselRemovalPlan:
        vessel = self._require_vessel_revision(
            command.vessel_id, command.expected_revision,
        )
        sm = self.allocator.sm
        contact_ids: set[str] = set()
        for contact_id, physical_id in self._evaluation_contact_links.items():
            if physical_id == vessel.id:
                contact_ids.add(sm.resolve_contact_id(contact_id))
        contact_ids.update(
            sm.resolve_contact_id(contact_id)
            for contact_id in self._vessel_contact_ids.get(vessel.id, ())
        )
        try:
            contact_ids.add(sm.resolve_contact_id(vessel.id))
        except (KeyError, TypeError):
            pass
        signal = vessel.ais_signal
        for contact in sm.contacts.list_snapshots():
            if contact.contact_id == vessel.id or (
                signal is not None and contact.ais_mmsi == signal.mmsi
            ):
                contact_ids.add(contact.contact_id)
        contact_ids.update(
            sm.resolve_contact_id(item.contact_id)
            for item in sm.get_probe_sessions()
            if item.contact_id in contact_ids
        )

        assigned_uav_ids: set[str] = set()
        for uav in self.uavs:
            task = self.control_coordinator.active_task(uav.id)
            if (
                uav.target_group_id in contact_ids
                or (task is not None and task.target_contact_id in contact_ids)
            ):
                assigned_uav_ids.add(uav.id)
        for record in self._mission_task_records.values():
            if record.contact_id in contact_ids and record.assigned_uav_id:
                assigned_uav_ids.add(record.assigned_uav_id)

        handoff_ids = tuple(sorted(
            attempt.handoff_id
            for attempt in self.handoff_manager.attempts()
            if sm.resolve_contact_id(attempt.contact_id) in contact_ids
            and attempt.state in {"required", "pending"}
        ))
        return _VesselRemovalPlan(
            vessel.id,
            self._vessel_revisions[vessel.id],
            tuple(sorted(contact_ids)),
            tuple(sorted(assigned_uav_ids)),
            handoff_ids,
        )

    def _commit_vessel_removal(self, plan: _VesselRemovalPlan) -> None:
        """Commit a preflighted removal without a fallible validation call."""
        now = float(self.clock.time)
        sm = self.allocator.sm
        contact_ids = set(plan.contact_ids)
        removed_mmsis = {
            contact.ais_mmsi
            for contact in sm.contacts.list_snapshots()
            if contact.contact_id in contact_ids and contact.ais_mmsi
        }
        removed_ship = next(
            (ship for ship in self.ships if ship.id == plan.vessel_id), None
        )
        if removed_ship is not None and removed_ship.ais_signal is not None:
            removed_mmsis.add(removed_ship.ais_signal.mmsi)
        probe_ids = {
            probe.probe_id
            for probe in sm.get_probe_sessions()
            if sm.resolve_contact_id(probe.contact_id) in contact_ids
        }

        # Release every task binding before removing the physical vessel.
        for uav_id in plan.assigned_uav_ids:
            uav = next((item for item in self.uavs if item.id == uav_id), None)
            task = self.control_coordinator.active_task(uav_id)
            if task is not None and (
                (
                    task.target_contact_id is not None
                    and sm.resolve_contact_id(task.target_contact_id) in contact_ids
                )
                or task.probe_id in probe_ids
            ):
                self._close_mission_task(
                    uav_id,
                    task,
                    status="blocked",
                    reason="vessel_removed",
                    current_time=now,
                )
            for record_id, record in tuple(self._mission_task_records.items()):
                if record.contact_id in contact_ids and record.assigned_uav_id == uav_id:
                    self._mission_task_records[record_id] = replace(
                        record,
                        status="blocked",
                        assigned_uav_id=None,
                        finished_at_min=now,
                        release_reason="vessel_removed",
                    )
            self._coordinator_tasks.pop(uav_id, None)
            self._tracking_started_at.pop(uav_id, None)
            self._ais_tracking_started_at.pop(uav_id, None)
            self._ais_measurements.pop(uav_id, None)
            if uav is not None:
                uav.target_group_id = None
                uav.assigned_region = None
                uav.status = "idle"
                uav.sensor_mode = "off"
            sm.clear_uav_assignment(uav_id)
            if self.control_coordinator.has_controller(uav_id):
                lease = self.control_coordinator.current_lease(uav_id)
                if lease.owner in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
                    self.control_coordinator.ownership.release_to_system(lease, now)
                if self.control_coordinator.current_lease(uav_id).owner is ControlOwner.SYSTEM:
                    self.control_coordinator.assign_system_task(
                        uav_id,
                        ControlTask(f"holding:vessel-removed:{uav_id}:{now}", OperationMode.HOLDING),
                        current_time=now,
                    )
                self.control_coordinator.operation_registry._release_binding(uav_id)

        # Invalidate handoffs while their contact references are still resolvable.
        for contact_id in sorted(contact_ids):
            failed = self.handoff_manager.fail_for_contact(
                contact_id, now, "vessel_removed",
            )
            for attempt in failed:
                sm.add_event("handoff_failed", {
                    "handoff_id": attempt.handoff_id,
                    "contact_id": attempt.contact_id,
                    "failure_reason": attempt.failure_reason,
                })

        for probe in sm.get_probe_sessions():
            if sm.resolve_contact_id(probe.contact_id) in contact_ids:
                sm.clear_probe_session(probe.probe_id)
        for region in tuple(sm.get_track_regions()):
            if (
                region.target_group_id is not None
                and sm.resolve_contact_id(region.target_group_id) in contact_ids
            ):
                sm.release_track_region(region.id, create_marker=False)
        for contact_id in sorted(contact_ids):
            try:
                sm.contacts.release(contact_id, now, "vessel_removed")
            except KeyError:
                pass
        for contact_id, physical_id in tuple(self._evaluation_contact_links.items()):
            if physical_id == plan.vessel_id or sm.resolve_contact_id(contact_id) in contact_ids:
                self._evaluation_contact_links.pop(contact_id, None)

        self.red_commander.remove_ship(plan.vessel_id)
        self.surveillance_stages.remove(plan.vessel_id)
        self._ais_force_refresh_ids.discard(plan.vessel_id)
        self._vessel_contact_ids.pop(plan.vessel_id, None)
        self.ships[:] = [ship for ship in self.ships if ship.id != plan.vessel_id]
        self._vessel_revisions.pop(plan.vessel_id, None)
        self._ship_position_history.pop(plan.vessel_id, None)
        self._emitter_track_ids.pop(plan.vessel_id, None)
        sm.add_event("vessel_removed", {
            "vessel_id": plan.vessel_id,
            "revision": plan.revision + 1,
            "contact_ids": list(plan.contact_ids),
        })
        remaining_mmsis = {
            ship.ais_signal.mmsi
            for ship in self.ships
            if ship.ais_signal is not None
        }
        for mmsi in removed_mmsis - remaining_mmsis:
            self._ais_history.pop(mmsi, None)

    def _create_scenario_vessel(self, command: VesselCommand) -> Ship:
        x, y = map(float, command.position_cells)
        cols, rows = self.config.grid.resolution
        margin = 1.0
        if not (margin <= x < cols - margin and margin <= y < rows - margin):
            raise ValueError("invalid_position")
        cell = (int(math.floor(x)), int(math.floor(y)))
        if self.ship_land_mask[cell[0], cell[1]] or self.obstacle_mask[cell[0], cell[1]]:
            raise ValueError("invalid_position")
        if any(math.dist((x, y), ship.float_position) < 1.0 for ship in self.ships):
            raise ValueError("vessel_spacing_conflict")
        vessel_id = f"scenario-vessel-{self._next_scenario_vessel_number}"
        side = 1.0
        waypoints = (
            (x, y, 0.0),
            (min(cols - margin - 0.01, x + side), y, 0.0),
            (min(cols - margin - 0.01, x + side), min(rows - margin - 0.01, y + side), math.pi / 2),
            (x, min(rows - margin - 0.01, y + side), math.pi),
        )
        emitter = None
        if command.vessel_class == "type_ii":
            emitter_seed = int.from_bytes(
                hashlib.sha256(
                    f"{self.seed}:{command.command_id}:manual-emitter".encode("ascii")
                ).digest()[:8],
                "big",
            )
            emitter = RadarEmitter(
                vessel_id,
                seed=emitter_seed,
                config=self.config.sensor.emitter,
            )
        vessel = Ship(
            vessel_id,
            GridCoord(int(math.floor(x)), int(math.floor(y))),
            self.config.ship.speed_kn,
            cell_size_km=self.config.grid.cell_size_km,
            normal_route=waypoints,
            patrol_route=waypoints,
            ais_position_noise_cells=self.config.ship.ais_position_noise_cells,
            max_turn_rate_deg_min=self.config.ship.max_turn_rate_deg_min,
            yaw_time_constant_min=self.config.ship.yaw_time_constant_min,
            heading_control_gain_per_min=self.config.ship.heading_control_gain_per_min,
            turn_speed_loss_fraction=self.config.ship.turn_speed_loss_fraction,
            max_acceleration_kn_per_min=self.config.ship.max_acceleration_kn_per_min,
            land_mask=self.ship_land_mask,
            navigator=self.ship_navigator,
            navigation_horizon_min=self.config.ship.navigation_horizon_min,
            integration_dt_min=self.config.ship.integration_dt_min,
            navigation_clearance_cells=self.config.ship.navigation_clearance_cells,
            radar_emitter=emitter,
            vessel_class=command.vessel_class,
            ais_enabled=True,
        )
        self._next_scenario_vessel_number += 1
        return vessel

    def _set_surveillance_fact(
        self,
        vessel_id: str,
        source: str,
        active: bool,
        now_min: float,
        cause_id: str,
    ) -> None:
        """Publish a source fact and its derived stage transition."""
        try:
            previous = self.surveillance_stages.snapshot(vessel_id)
        except KeyError:
            return
        state = self.surveillance_stages.set_fact(
            vessel_id, source, active, now_min, cause_id,
        )
        if state is None:
            return
        payload = {
            "vessel_id": vessel_id,
            "previous_stage": previous.stage,
            "stage": state.stage,
            "revision": state.revision,
            "cause_id": state.cause_id,
        }
        self.allocator.sm.add_event("surveillance_stage_changed", payload)
        self.allocator.trigger_manager.notify_event(
            "surveillance_stage_changed", time=now_min, **payload,
        )

    def _vessel_ids_for_contact(self, contact_id: str) -> tuple[str, ...]:
        """Resolve observation-only contact aliases back to environment IDs."""
        sm = self.allocator.sm
        try:
            canonical = sm.resolve_contact_id(contact_id)
        except (KeyError, TypeError):
            canonical = contact_id
        vessel_ids = set()
        for vessel_id, contact_ids in self._vessel_contact_ids.items():
            if canonical in {sm.resolve_contact_id(item) for item in contact_ids}:
                vessel_ids.add(vessel_id)
        for observed_id, vessel_id in self._evaluation_contact_links.items():
            if sm.resolve_contact_id(observed_id) == canonical:
                vessel_ids.add(vessel_id)
        for ship in self.ships:
            if ship.id == contact_id or ship.id == canonical:
                vessel_ids.add(ship.id)
        return tuple(sorted(vessel_ids))

    def _clear_surveillance_contact(
        self, contact_id: str, now_min: float, cause_id: str,
    ) -> None:
        for vessel_id in self._vessel_ids_for_contact(contact_id):
            for source in ("eo_lock", "probe", "passive", "sar"):
                self._set_surveillance_fact(
                    vessel_id, source, False, now_min, cause_id,
                )

    @property
    def blocked_role(self) -> str | None:
        """Read-only model role that currently blocks the simulation."""
        return self._blocked_role

    def _publish_runtime_state(self) -> None:
        """Copy lifecycle metadata into the immutable frame source."""
        self._validate_mission_state_invariants()
        self.allocator.sm.runtime_status = self._runtime_status
        self.allocator.sm.blocked_role = self._blocked_role
        self.allocator.sm.vessel_mutation_allowed = self.vessel_mutation_allowed
        self.allocator.sm.editing_allowed = self.editing_allowed
        self.allocator.sm.initial_vessel_count = self.config.ship.population.total_count
        self.allocator.sm.actual_vessel_count = len(getattr(self, "ships", ()))
        if hasattr(self, "surveillance_stages"):
            self._publish_vessel_inventory()
        self.allocator.sm.memory_version = self.allocator.memory_version
        self._publish_control_routes()
        set_context = getattr(self.allocator.llm_client.gateway, "set_context", None)
        if callable(set_context):
            set_context(
                self.episode_id,
                self.allocator.memory_version,
                float(self.clock.time),
            )

    def _set_search_task_projection(
        self,
        task_id: str,
        *,
        state: str,
        uav_id: str | None,
        current_time: float,
        reason: str | None,
        allow_missing_region: bool = False,
    ) -> None:
        """Apply one ordinary-search state to both authoritative projections."""
        valid_states = {"pending", "executing", "completed", "stale"}
        if state not in valid_states:
            raise ValueError(f"unknown search projection state: {state}")
        if state == "executing" and not isinstance(uav_id, str):
            raise ValueError("executing search must have a UAV")
        if state != "executing" and uav_id is not None:
            raise ValueError(f"{state} search must be unassigned")
        if uav_id is not None and not self.allocator.sm.is_uav_operational(uav_id):
            raise ValueError("executing search must use an operational UAV")

        regions = [
            region
            for region in self.allocator.sm.get_search_regions()
            if region.id == task_id and region.type == "search"
        ]
        if len(regions) > 1:
            raise ValueError(f"duplicate search region: {task_id}")
        record = self._mission_task_records.get(task_id)
        if record is not None and record.kind not in _SEARCH_TASK_KINDS:
            raise ValueError(f"non-search task has search region: {task_id}")
        if not regions and record is None:
            return
        if not regions:
            if not (
                allow_missing_region
                and state == "completed"
                and record is not None
            ):
                raise ValueError(f"search region missing: {task_id}")
            self._mission_task_records[task_id] = replace(
                record,
                status="completed",
                assigned_uav_id=None,
                finished_at_min=(
                    record.finished_at_min
                    if record.status in {"completed", "cancelled", "blocked"}
                    else current_time
                ),
                release_reason=(
                    record.release_reason
                    if record.status in {"completed", "cancelled", "blocked"}
                    else reason
                ),
            )
            return

        region = regions[0]
        if state in {"pending", "executing"}:
            region.status = "active"
        elif state == "completed":
            region.status = "completed"
        else:
            region.status = "stale"
        region.assigned_uav_id = uav_id

        if record is None:
            return
        if state == "pending":
            desired = replace(
                record,
                status="approved",
                assigned_uav_id=None,
                finished_at_min=None,
                release_reason=reason,
            )
        elif state == "executing":
            desired = replace(
                record,
                status="executing",
                assigned_uav_id=uav_id,
                started_at_min=(
                    record.started_at_min
                    if record.started_at_min is not None
                    else current_time
                ),
                finished_at_min=None,
                release_reason=None,
            )
        elif state == "completed":
            desired = replace(
                record,
                status="completed",
                assigned_uav_id=None,
                finished_at_min=(
                    record.finished_at_min
                    if record.status in {"completed", "cancelled", "blocked"}
                    else current_time
                ),
                release_reason=(
                    record.release_reason
                    if record.status in {"completed", "cancelled", "blocked"}
                    else reason
                ),
            )
        else:
            desired = replace(
                record,
                status=(
                    record.status
                    if record.status in {"completed", "cancelled", "blocked"}
                    else "blocked"
                ),
                assigned_uav_id=None,
                finished_at_min=(
                    record.finished_at_min
                    if record.status in {"completed", "cancelled", "blocked"}
                    else current_time
                ),
                release_reason=(
                    record.release_reason
                    if record.status in {"completed", "cancelled", "blocked"}
                    else reason
                ),
            )
        self._mission_task_records[task_id] = desired

    def _mission_state_invariant_errors(self) -> tuple[str, ...]:
        """Return deterministic ordinary-search projection violations."""
        sm = self.allocator.sm
        regions = [
            region for region in sm.get_search_regions()
            if region.type == "search"
        ]
        region_by_id: dict[str, Region] = {}
        errors: list[str] = []
        for region in regions:
            if region.id in region_by_id:
                errors.append(f"duplicate_search_region:{region.id}")
            else:
                region_by_id[region.id] = region

        records = {
            record.task_id: record
            for record in self._mission_task_records.values()
            if record.kind == "search"
        }
        assigned_records: dict[str, list[str]] = defaultdict(list)
        for record in records.values():
            if record.assigned_uav_id is not None:
                assigned_records[record.assigned_uav_id].append(record.task_id)
            region = region_by_id.get(record.task_id)
            if record.status == "executing":
                if record.assigned_uav_id is None:
                    errors.append(f"executing_search_without_uav:{record.task_id}")
                else:
                    if not sm.is_uav_operational(record.assigned_uav_id):
                        errors.append(
                            f"executing_search_nonoperational_uav:{record.task_id}"
                        )
                    if region is None or region.assigned_uav_id != record.assigned_uav_id:
                        errors.append(
                            f"region_record_assignee_mismatch:{record.task_id}"
                        )
            elif record.status == "approved" and record.assigned_uav_id is None:
                if region is None or region.status != "active" or region.assigned_uav_id is not None:
                    errors.append(f"pending_search_projection_mismatch:{record.task_id}")
            elif record.status in {"completed", "cancelled", "blocked"} and record.assigned_uav_id is not None:
                errors.append(f"terminal_search_has_uav:{record.task_id}")

        if not self.allocator.uses_legacy_scheduler():
            for region in regions:
                if region.status not in {"active"} or region.assigned_uav_id is None:
                    continue
                record = records.get(region.id)
                if record is None or record.assigned_uav_id != region.assigned_uav_id:
                    errors.append(f"region_record_assignee_mismatch:{region.id}")

        for uav_id, task_ids in sorted(assigned_records.items()):
            if len(task_ids) > 1:
                errors.append(f"uav_bound_to_multiple_searches:{uav_id}")

        unfinished = [
            region for region in regions
            if region.status == "active"
        ]
        for index, left in enumerate(unfinished):
            for right in unfinished[index + 1:]:
                if not (
                    left.bbox.col_end <= right.bbox.col_start
                    or right.bbox.col_end <= left.bbox.col_start
                    or left.bbox.row_end <= right.bbox.row_start
                    or right.bbox.row_end <= left.bbox.row_start
                ):
                    errors.append(
                        f"overlapping_unfinished_search_regions:{left.id}/{right.id}"
                    )
        return tuple(dict.fromkeys(errors))

    def _validate_mission_state_invariants(
        self, *, strict: bool = False,
    ) -> tuple[str, ...]:
        """Validate projections; production pauses once per violation signature."""
        errors = self._mission_state_invariant_errors()
        if not errors:
            return ()
        if strict:
            raise ValueError("; ".join(errors))
        signature = tuple(errors)
        if signature not in self._mission_invariant_failure_signatures:
            self._mission_invariant_failure_signatures.add(signature)
            self.allocator.sm.add_event("mission_state_invariant_failed", {
                "violations": list(errors),
            })
        self._runtime_status = "paused_safety"
        self._blocked_role = "mission_state_invariant"
        return errors

    def _publish_control_routes(self) -> None:
        """Publish immutable controller route envelopes for frame readers."""
        coordinator = getattr(self, "control_coordinator", None)
        if coordinator is None:
            return
        state = self.allocator.sm
        for uav in getattr(self, "uavs", ()):
            state.set_control_route(
                uav.id,
                coordinator.route_snapshot(uav.id),
            )

    def _publish_vessel_inventory(self) -> None:
        items = tuple({
            "scenario_entity_id": ship.id,
            "revision": self._vessel_revisions.get(ship.id, 1),
            "position": [float(ship.float_position[0]), float(ship.float_position[1])],
            "vessel_class": ship.vessel_class,
            "ais_enabled": ship.ais_enabled,
            "ais_controllable": ship.vessel_class == "type_ii",
            "surveillance_stage": self.surveillance_stages.snapshot(ship.id).stage,
        } for ship in self.ships)
        self.allocator.sm.publish_vessel_inventory(items)

    def _set_runtime_state(self, status: str, blocked_role: str | None = None) -> None:
        self._runtime_status = status
        self._blocked_role = blocked_role
        self._publish_runtime_state()

    def _record_decision_maker_failure(self, result: dict) -> None:
        """Count one completed heavy decision and pause on bounded failure."""
        interaction = result.get("llm_cycle") or {}
        category = (
            interaction.get("failure_category")
            or self.allocator.mission_scheduler.last_selection_failure_category
            or "unknown"
        )
        stage = interaction.get("failure_stage") or "transport"
        self._decision_failure_streak += 1
        self.allocator.sm.add_event("mission_model_failure", {
            "failure_category": category,
            "failure_stage": stage,
            "consecutive_failures": self._decision_failure_streak,
            "snapshot_id": result.get("snapshot_id"),
        })
        threshold = self.config.mission.coverage.max_consecutive_decision_failures
        immediate_pause = category in {
            "http_401", "http_402", "http_403", "configuration",
        }
        if immediate_pause or self._decision_failure_streak >= threshold:
            self.allocator.trigger_manager.clear_heavy_retry()
            self._set_runtime_state("paused_model", "decision_maker")
            self.allocator.sm.add_event("mission_model_paused", {
                "blocked_role": "decision_maker",
                "failure_category": category,
                "failure_stage": stage,
                "consecutive_failures": self._decision_failure_streak,
            })

    def _create_ships(self) -> list[Ship]:
        return create_ship_population(
            self.config,
            self.seed,
            self.ship_land_mask,
            self.ship_navigator,
        )

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
        strategy_memory_store = self.allocator.strategy_memory_store
        strategy_memory_version = self.allocator.memory_version
        retired = dict(self._retired_command_results)
        for queue in (self.intent_commands, self.runtime_commands):
            for command in queue.drain():
                result = CommandResult(
                    command.command_id, "rejected", None, "episode_reset",
                )
                queue.complete(result)
                retired[command.command_id] = result
        self.__init__(
            self.config,
            next_seed,
            control_providers=self._control_providers,
            llm_gateway=self._llm_gateway,
            strategy_memory_store=strategy_memory_store,
            strategy_memory_version=strategy_memory_version,
        )
        self._retired_command_results = retired
        self.reset_generation = generation
        self.allocator.sm.scenario_generation = generation
        self.allocator.sm.add_event("environment_reset", {
            "previous_seed": previous_seed,
            "seed": next_seed,
            "generation": generation,
            "base_ids": [base.id for base in self.bases],
        })
        return self

    @property
    def retired_command_results(self) -> dict[str, CommandResult]:
        return dict(self._retired_command_results)

    def published_intent_snapshot(self) -> dict:
        """Return the read model used by the operator API and later frames."""
        now = float(self.clock.time)
        statuses = self._evaluate_intent_statuses(now)
        return {
            "episode_id": self.episode_id,
            "intents": self.intents.intents(),
            "statuses": statuses,
            "pending_commands": self.intent_commands.pending(),
            "intent_events": self.allocator.sm.get_intent_events(),
        }

    def _evaluate_intent_statuses(self, now_min: float):
        sm = self.allocator.sm
        statuses = self.intents.evaluate(
            sm.get_info_matrix(),
            sm.get_last_scan_matrix(),
            sm.get_searchable_mask(),
            tuple(self._mission_task_records.values()),
            now_min,
        )
        sm.publish_intent_snapshot(self.intents.intents(), statuses)
        return statuses

    def _record_intent_result(self, result: CommandResult) -> None:
        self.allocator.sm.record_intent_event(result)

    @staticmethod
    def _intent_error_code(error: Exception) -> str:
        message = str(error).lower()
        if "revision" in message:
            return "revision_conflict"
        if "maximum active" in message:
            return "intent_limit"
        if "unknown intent" in message:
            return "intent_not_found"
        if "not active" in message:
            return "intent_not_active"
        return "invalid_intent"

    def _expire_intents(self, current_time: float) -> tuple:
        expired = self.intents.expire(current_time)
        for intent in expired:
            self.allocator.sm.add_event("intent_expired", {
                "intent_id": intent.intent_id,
                "revision": intent.revision,
            })
            self.allocator.trigger_manager.notify_event(
                "intent_expired", time=current_time, intent_id=intent.intent_id,
            )
        return expired

    def apply_pending_intent_commands(self) -> tuple[CommandResult, ...]:
        """Apply queued intent mutations at a simulation-thread boundary."""
        now = float(self.clock.time)
        self.allocator.sm.current_time = now
        self._expire_intents(now)
        results: list[CommandResult] = []
        for command in self.intent_commands.drain():
            if command.episode_id != self.episode_id:
                result = CommandResult(
                    command.command_id, "rejected", None, "episode_conflict",
                )
            elif self.runtime_status == "finished":
                result = CommandResult(
                    command.command_id, "rejected", None, "episode_finished",
                )
            else:
                try:
                    if command.operation == "create":
                        intent = self.intents.create(dict(command.payload), now)
                    elif command.operation == "update":
                        intent = self.intents.update(
                            command.intent_id,
                            command.expected_revision,
                            dict(command.payload),
                            now,
                        )
                    else:
                        intent = self.intents.cancel(
                            command.intent_id, command.expected_revision, now,
                        )
                    result = CommandResult(
                        command.command_id, "applied", intent, None,
                    )
                    self.allocator.sm.add_event("intent_changed", {
                        "command_id": command.command_id,
                        "intent_id": intent.intent_id,
                        "operation": command.operation,
                        "revision": intent.revision,
                    })
                    self.allocator.trigger_manager.notify_event(
                        "intent_changed", time=now,
                        intent_id=intent.intent_id,
                    )
                except (TypeError, ValueError, KeyError) as exc:
                    result = CommandResult(
                        command.command_id, "rejected", None,
                        self._intent_error_code(exc),
                    )
            self.intent_commands.complete(result)
            self._record_intent_result(result)
            results.append(result)
        # Keep the public read model coherent even when commands are applied
        # while the clock is paused or directly at an API/test boundary.
        self._evaluate_intent_statuses(now)
        return tuple(results)

    def apply_pending_runtime_commands(self) -> tuple[CommandResult, ...]:
        """Apply retry/abort commands without advancing the simulation clock."""
        results: list[CommandResult] = []
        for command in self.runtime_commands.drain():
            if command.episode_id != self.episode_id:
                result = CommandResult(
                    command.command_id, "rejected", None, "episode_conflict",
                )
            elif command.operation == "retry":
                if self.runtime_status != "paused_model":
                    result = CommandResult(
                        command.command_id, "rejected", None, "runtime_not_paused",
                    )
                else:
                    self.retry_blocked_decision()
                    result = CommandResult(
                        command.command_id,
                        "applied" if self.runtime_status == "running" else "rejected",
                        None,
                        None if self.runtime_status == "running" else "model_blocked",
                    )
            elif self.runtime_status == "finished":
                result = CommandResult(
                    command.command_id, "rejected", None, "episode_finished",
                )
            else:
                self._outcome_evaluator.invalidate("runtime_aborted")
                self._set_runtime_state("finished")
                self.allocator.sm.add_event("runtime_aborted", {
                    "command_id": command.command_id,
                })
                result = CommandResult(command.command_id, "applied", None, None)
            self.runtime_commands.complete(result)
            results.append(result)
        return tuple(results)

    def step(self) -> dict:
        self.apply_pending_runtime_commands()
        self.apply_pending_intent_commands()
        if self.runtime_status == "running":
            self.opponent_population.tick(self)
        self.apply_pending_vessel_commands()
        self._editing_allowed = False
        if self.runtime_status != "running":
            return self.last_result
        try:
            self._prepare_red_decision(self.clock.time)
        except RedDecisionBlocked:
            self._publish_runtime_state()
            self.last_result = {
                "trigger_type": "model_blocked",
                "action": None,
                "blocked_role": self.blocked_role,
            }
            return self.last_result
        t = self.clock.tick()
        sm = self.allocator.sm
        sm.current_time = t
        for failed_handoff in self.handoff_manager.advance(t):
            sm.add_event("handoff_failed", {
                "handoff_id": failed_handoff.handoff_id,
                "contact_id": failed_handoff.contact_id,
                "failure_reason": failed_handoff.failure_reason,
            })
        self._publish_runtime_state()
        self._expire_intents(t)

        self._update_obstacles()
        self._update_ships(t)
        self._refresh_ais_signals(t)
        self._expire_contacts(t)

        for uav in self.uavs:
            self._publish_fuel_warning(uav, t)
            fuel_low = self._step_controlled_uav(uav, t)
            self._record_storm_avoidance(uav, t)
            self._publish_fuel_warning(uav, t)
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

        self._record_uav_position_history(t)
        self._update_passive_sensors(t)
        self._update_sensors_and_detections(t)
        self._finalize_coverage_completions(t)
        self._advance_probe_sessions(t)
        self._update_lifecycle_mode(t)
        self._process_refuelling(t)
        self._publish_information_delta(
            sm.information_policy.advance_time(t), t,
        )
        self._sync_state_from_entities()

        if self.allocator.uses_legacy_scheduler():
            result = self.allocator.step(t)
            self._sync_assignments()
        else:
            pending_reassigned = self._apply_pending_search_reassignments(t)
            if pending_reassigned:
                # Refresh the immutable snapshot after the atomic handoff so
                # the following trigger sees the new owner and generation.
                self.allocator.build_mission_snapshot(
                    t,
                    active_tasks=tuple(self._mission_task_records.values()),
                    intents=self.intents.intents(),
                    intent_statuses=self._evaluate_intent_statuses(t),
                )
            result, batch = self.allocator.mission_step(
                t,
                active_tasks=tuple(self._mission_task_records.values()),
                intents=self.intents.intents(),
                intent_statuses=self._evaluate_intent_statuses(t),
            )
            assignment_applied = batch is not None
            if batch is not None:
                assignment_applied = self.apply_assignment_batch(batch)
                if not assignment_applied:
                    result = {
                        **result,
                        "action": "mission_assignment_rejected",
                    }
            if pending_reassigned:
                result = {
                    **result,
                    "pending_search_reassignments": pending_reassigned,
                }
                if result.get("trigger_type") == "none":
                    result = {
                        **result,
                        "trigger_type": "light",
                        "action": "pending_searches_reassigned",
                        "assignments": pending_reassigned,
                    }
            timing = getattr(self.allocator, "last_decision_timing", None)
            skipped_model_selection = (
                result.get("action") == "mission_selection_skipped"
            )
            if skipped_model_selection:
                self._decision_failure_streak = 0
            if timing is not None:
                decision_succeeded = assignment_applied or (
                    result.get("trigger_type") == "light"
                    and result.get("action") == "approved_tasks_deferred"
                ) or skipped_model_selection
                if result.get("trigger_type") == "heavy":
                    if decision_succeeded:
                        self._decision_failure_streak = 0
                    else:
                        self._record_decision_maker_failure(result)
                self._outcome_evaluator.record_decision_latency(
                    snapshot_frozen_wall=timing["snapshot_frozen_wall"],
                    decision_finished_wall=(
                        timing["decision_finished_wall"]
                        if decision_succeeded else time.perf_counter()
                    ),
                    llm_seconds=timing["llm_seconds"],
                    validation_seconds=timing["validation_seconds"],
                    matching_seconds=timing["matching_seconds"],
                    success=decision_succeeded,
                    failure_reason=None if decision_succeeded else "assignment_rejected",
                )
        self.last_result = result
        if result["trigger_type"] == "heavy":
            self.heavy_triggers += 1
            interaction = result.get("llm_cycle") or {}
            self.llm_successes += int(bool(interaction.get("success")))
            if interaction and not interaction.get("success", False):
                self._outcome_evaluator.invalidate("decision_maker_failed")
            signature = tuple(
                (region["id"], tuple(region["bbox"]))
                for region in result.get("search_regions", [])
            )
            if signature and (not self.region_signatures or signature != self.region_signatures[-1]):
                self.region_signatures.append(signature)
        elif result["trigger_type"] == "light":
            self.light_triggers += 1
        self._detect_and_resolve_path_conflicts(t)
        self._observe_evaluation(t)
        self._record_statuses()
        self._publish_runtime_state()
        return result

    def retry_blocked_decision(self) -> None:
        """Retry a model decision without advancing simulation time."""
        if self.runtime_status != "paused_model":
            return
        if self.blocked_role == "decision_maker":
            result, batch = self.allocator.mission_step(
                self.clock.time,
                active_tasks=tuple(self._mission_task_records.values()),
                intents=self.intents.intents(),
                intent_statuses=self._evaluate_intent_statuses(self.clock.time),
                force_heavy=True,
            )
            skipped = result.get("action") == "mission_selection_skipped"
            if skipped or (batch is not None and self.apply_assignment_batch(batch)):
                self.allocator.trigger_manager.clear_heavy_retry()
                self._decision_failure_streak = 0
                self._set_runtime_state("running")
                self.last_result = result
                self.allocator.sm.add_event("mission_model_retry_succeeded", {
                    "snapshot_id": result.get("snapshot_id"),
                    "selection_call_id": (
                        None if skipped else batch.selection_call_id
                    ),
                })
            else:
                self._set_runtime_state("paused_model", "decision_maker")
                self.allocator.sm.add_event("mission_model_retry_failed", {
                    "snapshot_id": result.get("snapshot_id"),
                    "failure_category": (
                        self.allocator.mission_scheduler.last_selection_failure_category
                        or "apply"
                    ),
                    "errors": list(
                        self.allocator.mission_scheduler.last_selection_errors
                    ),
                })
            return
        try:
            self._prepare_red_decision(self.clock.time)
        except RedDecisionBlocked:
            return
        self.last_result = {
            "trigger_type": "model_resumed",
            "action": "red_decision_retry_succeeded",
        }
        self.allocator.sm.add_event("red_decision_retry_succeeded", {
            "sim_time_min": self.clock.time,
        })

    def apply_assignment_batch(self, batch: AssignmentBatch) -> bool:
        """Validate and install one scheduler batch at the simulation boundary."""
        if not isinstance(batch, AssignmentBatch):
            return False
        snapshot = self.allocator.last_mission_snapshot
        if snapshot is None or batch.snapshot_id != snapshot.snapshot_id:
            return False
        if self.allocator.sm.obstacle_version != snapshot.planning_map_version:
            return False

        candidates = {
            candidate.task_id: candidate for candidate in snapshot.candidates
        }
        resources = {resource.uav_id: resource for resource in snapshot.resources}
        edges = {
            (edge.task_id, edge.uav_id): edge
            for edge in snapshot.feasible_edges
        }
        candidates.update(
            {
                record.task_id: replace(
                    self.allocator._active_task_candidate(
                        record, snapshot.resources
                    ),
                    feasible_uav_ids=tuple(sorted(
                        edge.uav_id
                        for (task_id, _), edge in edges.items()
                        if task_id == record.task_id
                    )),
                )
                for record in snapshot.active_tasks
            }
        )
        if len({assignment.task_id for assignment in batch.assignments}) != len(batch.assignments):
            return False
        if len({assignment.uav_id for assignment in batch.assignments}) != len(batch.assignments):
            return False

        prepared: list[
            tuple[
                object,
                ControlTask,
                TaskCandidate,
                object,
                ControlTask | None,
                Region | None,
            ]
        ] = []
        route_plans: dict[str, SearchRoutePlan] = {}
        reservations: list[tuple[str, str, str | None]] = []
        reserved_contacts: set[str] = set()
        handoff_commits: list[tuple[object, str, str]] = []
        next_probe_number = self._next_probe_number
        for assignment in batch.assignments:
            candidate = candidates.get(assignment.task_id)
            resource = resources.get(assignment.uav_id)
            if candidate is None or resource is None:
                return False
            if not self.allocator.sm.is_uav_operational(assignment.uav_id):
                return False
            if assignment.uav_id not in candidate.feasible_uav_ids:
                return False
            edge = edges.get((assignment.task_id, assignment.uav_id))
            if edge is None:
                return False
            try:
                lease = self.control_coordinator.current_lease(assignment.uav_id)
            except KeyError:
                return False
            if lease.generation != assignment.expected_generation:
                return False
            if resource.generation != assignment.expected_generation:
                return False
            active = self.control_coordinator.active_task(assignment.uav_id)
            previous_task_id = active.task_id if active is not None else None
            if (
                active is not None
                and active.task_type is OperationMode.HOLDING
                and lease.owner is ControlOwner.SYSTEM
                and resource.current_task_id is None
            ):
                # System holding is not a mission assignment in the snapshot.
                previous_task_id = None
            if assignment.previous_task_id != previous_task_id:
                return False
            uav = next(
                (entity for entity in self.uavs if entity.id == assignment.uav_id),
                None,
            )
            if uav is None:
                return False

            if candidate.contact_id is not None:
                handoff = self.handoff_manager.latest_for_contact(candidate.contact_id)
                if handoff is not None and handoff.state != "succeeded":
                    contact = self.allocator.sm.contacts.snapshot(candidate.contact_id)
                    if assignment.uav_id == handoff.source_uav_id:
                        return False
                    if candidate.kind == "track" and assignment.uav_id not in (
                        self.handoff_manager.observed_successors(contact, self.clock.time)
                    ):
                        return False
                    if handoff.state == "pending" and handoff.successor_uav_id != assignment.uav_id:
                        return False
                    if candidate.kind in _SEARCH_TASK_KINDS:
                        if handoff.state != "required" or self.clock.time > handoff.assignment_deadline_min:
                            return False
                        handoff_commits.append((handoff, assignment.uav_id, candidate.contact_id))

            if candidate.kind in _SEARCH_TASK_KINDS:
                if candidate.bbox is None:
                    return False
                existing_region = next(
                    (
                        item
                        for item in self.allocator.sm.get_search_regions()
                        if item.id == candidate.task_id
                    ),
                    None,
                )
                if existing_region is not None and candidate.kind == "search":
                    region = replace(
                        existing_region,
                        status="active",
                        priority=candidate.priority,
                        assigned_uav_id=assignment.uav_id,
                    )
                else:
                    region = Region(
                        candidate.task_id,
                        BBox(*candidate.bbox),
                        "search",
                        priority=candidate.priority,
                        created_cycle=self.allocator.sm.cycle,
                        assigned_uav_id=assignment.uav_id,
                    )
                if self.allocator.sm.coverage_service is not None:
                    try:
                        self.allocator.sm.coverage_service.validate_start(
                            candidate.task_id,
                            assignment.expected_generation + 1,
                            assignment.uav_id,
                            tuple(candidate.bbox),
                            self.clock.time,
                        )
                    except ValueError:
                        return False
                try:
                    route_plan = plan_search_route(
                        self._search_route_request(uav, region)
                    )
                except Exception as exc:
                    self.allocator.sm.add_event("mission_assignment_rejected", {
                        "reason": "search_route_planning_failed",
                        "error_type": type(exc).__name__,
                        "task_id": candidate.task_id,
                        "snapshot_id": snapshot.snapshot_id,
                    })
                    return False
                if not route_plan.scanned_swath_count:
                    return False
                route_plans[assignment.task_id] = route_plan
                control_task = ControlTask(
                    candidate.task_id,
                    OperationMode.COVERAGE,
                    region_bbox=region.bbox,
                )
            elif candidate.contact_id is not None:
                try:
                    contact_id = self.allocator.sm.resolve_contact_id(
                        candidate.contact_id
                    )
                    contact = self.allocator.sm.contacts.snapshot(contact_id)
                except KeyError:
                    return False
                if contact_id in reserved_contacts:
                    return False
                if contact.assigned_uav_id is not None and not (
                    contact.assigned_uav_id == assignment.uav_id
                    and active is not None
                    and active.task_id == assignment.task_id
                ):
                    return False
                if contact.state in {"cleared", "lost", "departed"}:
                    return False
                if candidate.kind == "track" and not (
                    contact.vessel_class == "type_ii"
                    or getattr(contact, "activity", "unknown")
                    in {"suspected_violation", "confirmed_violation"}
                ):
                    return False
                if candidate.kind == "probe" and contact.vessel_class != "unknown":
                    return False
                handoff = next(
                    (
                        item
                        for item in self.handoff_manager.attempts()
                        if item.contact_id == contact_id
                        and item.state == "required"
                        and item.source_uav_id != assignment.uav_id
                    ),
                    None,
                )
                if handoff is not None:
                    if self.clock.time > handoff.assignment_deadline_min:
                        return False
                    handoff_commits.append((handoff, assignment.uav_id, contact_id))
                target = contact.estimated_position_cells
                radius = (
                    self.config.mission.contact.baseline_standoff_cells
                    if candidate.kind == "probe"
                    else self.config.mission.contact.near_standoff_cells
                )
                try:
                    route = self.allocator._mission_navigator.plan_to_standoff(
                        (*uav.float_position, uav.heading_rad),
                        target,
                        radius,
                        self.allocator.sm.obstacle_mask,
                        uav.R_min,
                        snapshot.planning_map_version,
                    )
                except Exception as exc:
                    self.allocator.sm.add_event("mission_assignment_rejected", {
                        "reason": "standoff_route_planning_failed",
                        "error_type": type(exc).__name__,
                        "task_id": candidate.task_id,
                        "snapshot_id": snapshot.snapshot_id,
                    })
                    return False
                if not route:
                    return False
                reuse_existing_probe = (
                    candidate.kind == "probe"
                    and active is not None
                    and active.task_id == assignment.task_id
                    and active.target_contact_id == contact_id
                    and active.probe_id is not None
                    and (
                        session := self.allocator.sm.get_probe_session(active.probe_id)
                    ) is not None
                    and session.contact_id == contact_id
                    and session.uav_id == assignment.uav_id
                )
                if reuse_existing_probe:
                    probe_id = active.probe_id
                elif candidate.kind == "probe":
                    probe_id = f"P{next_probe_number:04d}"
                    next_probe_number += 1
                else:
                    probe_id = None
                control_task = ControlTask(
                    candidate.task_id,
                    OperationMode.PROBE if candidate.kind == "probe" else OperationMode.TRACK,
                    target_contact_id=contact_id,
                    probe_id=probe_id,
                )
                reservations.append((contact_id, assignment.uav_id, probe_id))
                reserved_contacts.add(contact_id)
            else:
                return False
            prepared.append(
                (
                    uav,
                    control_task,
                    candidate,
                    assignment,
                    active,
                    region if candidate.kind in _SEARCH_TASK_KINDS else None,
                )
            )

        reservation_state = self.allocator.sm.contacts.capture_reservation_state(
            contact_id for contact_id, _, _ in reservations
        )
        try:
            for contact_id, uav_id, probe_id in reservations:
                self.allocator.sm.contacts.reserve(contact_id, uav_id, probe_id)
            leases = self.control_coordinator.assign_tasks_atomically(
                tuple(
                    (assignment.uav_id, task, assignment.expected_generation)
                    for _, task, _, assignment, _, _ in prepared
                ),
                current_time=self.clock.time,
                dt_min=self.clock.dt_min,
            )
        except Exception as exc:
            self.allocator.sm.contacts.restore_reservation_state(reservation_state)
            self.allocator.sm.add_event("mission_assignment_rejected", {
                "reason": str(exc),
                "error_type": type(exc).__name__,
                "snapshot_id": snapshot.snapshot_id,
            })
            return False

        for attempt, successor_uav_id, contact_id in handoff_commits:
            try:
                self.handoff_manager.commit_assignment(
                    attempt.handoff_id,
                    successor_uav_id,
                    self.clock.time,
                )
            except (KeyError, ValueError) as exc:
                raise RuntimeError(
                    "handoff changed after assignment commit"
                ) from exc
            self._outcome_evaluator.record_handoff_assignment(
                attempt.handoff_id,
                at_min=self.clock.time,
            )
            self.allocator.sm.add_event("handoff_assignment_committed", {
                "handoff_id": attempt.handoff_id,
                "contact_id": contact_id,
                "successor_uav_id": successor_uav_id,
            })

        by_uav = {
            assignment.uav_id: (
                uav, task, candidate, assignment, previous_task, region
            )
            for (
                uav, task, candidate, assignment, previous_task, region
            ) in prepared
        }
        for lease, assignment in zip(leases, batch.assignments):
            uav, task, candidate, _, old_task, prepared_region = by_uav[assignment.uav_id]
            if old_task is not None and old_task.task_id != task.task_id:
                old_record = self._mission_task_records.get(old_task.task_id)
                if old_record is not None:
                    self._close_mission_task(
                        assignment.uav_id,
                        old_task,
                        status=(
                            "approved"
                            if old_record.kind in _SEARCH_TASK_KINDS
                            else "cancelled"
                        ),
                        reason="preempted",
                        current_time=self.clock.time,
                        preserve_search=old_record.kind in _SEARCH_TASK_KINDS,
                        coverage_generation=(
                            assignment.expected_generation
                            if old_task.task_type is OperationMode.COVERAGE
                            else None
                        ),
                    )
                self.allocator.sm.mark_uav_reassigned(
                    assignment.uav_id, self.clock.time,
                )
            if candidate.kind in _SEARCH_TASK_KINDS:
                region = prepared_region
                if region is None:
                    return False
                regions = [
                    item for item in self.allocator.sm.get_search_regions()
                    if item.id != region.id
                ]
                self.allocator.sm.set_search_regions([*regions, region])
                self.allocator.ivt.add_row(region.id, region.bbox, "search")
                self._apply_search_route_plan(
                    uav, region, route_plans[assignment.task_id]
                )
                self.allocator.sm.update_uav_status(
                    uav.id,
                    uav.status,
                    uav.position,
                    assigned_region_id=region.id,
                    fuel_remaining_pct=uav.fuel_remaining_pct,
                )
            elif task.task_type is OperationMode.TRACK:
                contact = self.allocator.sm.contacts.snapshot(task.target_contact_id)
                uav.start_tracking(task.target_contact_id, contact.estimated_position_cells)
                self.allocator.sm.update_uav_status(
                    uav.id,
                    "transit",
                    uav.position,
                    assigned_region_id=task.task_id,
                    target_group_id=task.target_contact_id,
                    fuel_remaining_pct=uav.fuel_remaining_pct,
                )
            else:
                uav._mission_kind = "probe"
                uav.status = "transit"
                uav.sensor_mode = "off"
                self.allocator.sm.update_uav_status(
                    uav.id,
                    "transit",
                    uav.position,
                    assigned_region_id=task.task_id,
                    fuel_remaining_pct=uav.fuel_remaining_pct,
                )
                assert task.probe_id is not None
                if self.allocator.sm.get_probe_session(task.probe_id) is None:
                    self.allocator.sm.set_probe_session(
                        ProbeSession(
                            task.probe_id,
                            task.target_contact_id,
                            uav.id,
                            "baseline",
                            self.clock.time,
                            None,
                            self.clock.time,
                            (),
                            (),
                            0.0,
                            None,
                        )
                    )
                for vessel_id in self._vessel_ids_for_contact(task.target_contact_id):
                    self._set_surveillance_fact(
                        vessel_id, "probe", True, self.clock.time, task.probe_id,
                    )
            self._coordinator_tasks[uav.id] = task
            existing_record = self._mission_task_records.get(task.task_id)
            if existing_record is None:
                existing_record = TaskRecord(
                    task.task_id,
                    candidate.kind,
                    "candidate",
                    candidate.bbox,
                    task.target_contact_id,
                    candidate.intent_ids,
                    None,
                    None,
                    candidate.eligible_since_min,
                    None,
                    None,
                    None,
                )
            self._mission_task_records[task.task_id] = replace(
                existing_record,
                kind=candidate.kind,
                status=("executing" if candidate.kind == "search" else "approved"),
                bbox=candidate.bbox,
                contact_id=task.target_contact_id,
                intent_ids=candidate.intent_ids,
                assigned_uav_id=uav.id,
                approved_call_id=batch.selection_call_id,
                started_at_min=(
                    existing_record.started_at_min
                    if existing_record.started_at_min is not None
                    else self.clock.time
                ),
                finished_at_min=None,
                release_reason=None,
            )
            if candidate.kind == "search":
                self._set_search_task_projection(
                    task.task_id,
                    state="executing",
                    uav_id=uav.id,
                    current_time=self.clock.time,
                    reason=None,
                )
            self.allocator.sm.update_uav_control(
                uav.id,
                self.control_coordinator.configured_mode(uav.id).value,
                lease.owner.value,
                self.control_coordinator.operation_mode(uav.id).value,
                lease.generation,
                self.control_coordinator.safety_intervened(uav.id),
            )
            if task.task_type is OperationMode.COVERAGE:
                self._start_coverage_service_task(
                    uav,
                    task,
                    self.clock.time,
                    generation=lease.generation,
                )
        self._next_probe_number = next_probe_number
        self.allocator.sm.add_event("mission_assignment_committed", {
            "snapshot_id": snapshot.snapshot_id,
            "selection_call_id": batch.selection_call_id,
            "task_ids": [assignment.task_id for assignment in batch.assignments],
        })
        self._publish_control_routes()
        return True

    def _apply_pending_search_reassignments(self, current_time: float) -> int:
        """Install deterministic pending-search handoffs at one engine boundary."""
        batch = self.allocator.build_pending_search_batch(
            current_time,
            active_tasks=tuple(self._mission_task_records.values()),
        )
        if batch is None:
            return 0
        if not self.apply_assignment_batch(batch):
            return 0
        self.allocator.sm.add_event("pending_search_reassigned", {
            "snapshot_id": batch.snapshot_id,
            "selection_call_id": batch.selection_call_id,
            "assignments": [
                {"task_id": item.task_id, "uav_id": item.uav_id}
                for item in batch.assignments
            ],
        })
        return len(batch.assignments)

    def _prepare_red_decision(self, current_time: float) -> None:
        """Build one red-only fleet snapshot and install its validated plan."""
        self._red_snapshot_sequence += 1
        uavs = tuple(
            (
                uav.id,
                (float(uav.float_position[0]), float(uav.float_position[1])),
                (
                    float(
                        uav.cruise_speed_kmh / uav.cell_size_km / 60.0
                        * math.cos(uav.heading_rad)
                    ),
                    float(
                        uav.cruise_speed_kmh / uav.cell_size_km / 60.0
                        * math.sin(uav.heading_rad)
                    ),
                ),
            )
            for uav in self.uavs
        )
        ships = []
        active_signature = []
        for ship in self.ships:
            if ship.departed:
                continue
            minimum_distance = min(
                (math.dist(ship.float_position, uav.float_position)
                 for uav in self.uavs), default=math.inf,
            )
            self.red_commander.threat_gate.update(
                ship.id, ship.vessel_class, minimum_distance, current_time
            )
            stage = self.surveillance_stages.snapshot(ship.id).stage
            ships.append(
                RedShipSnapshot(
                    ship_id=ship.id,
                    vessel_class=ship.vessel_class,
                    surveillance_stage=stage,
                    position_cells=(float(ship.float_position[0]), float(ship.float_position[1])),
                    heading_deg=float(math.degrees(ship.heading_rad)),
                    speed_kn=float(ship.speed_kn),
                    normal_tangent_deg=float(math.degrees(ship.normal_tangent_rad())),
                    ais_enabled=ship.ais_enabled,
                )
            )
            if (
                not ship.departed
                and ship.vessel_class == "type_ii"
                and stage in ("detected", "probing", "tracking")
            ):
                active_signature.append((ship.id, stage))
        snapshot = RedSnapshot(
            snapshot_id=f"red-{self.reset_generation}-{self._red_snapshot_sequence}",
            sim_time_min=float(current_time),
            ships=tuple(ships),
            uavs=uavs,
            active_signature=tuple(sorted(active_signature)),
            land_mask_version=int(max((ship.navigator.map_version for ship in self.ships), default=0)),
        )
        try:
            plan = self.red_commander.decide(snapshot)
        except RedDecisionBlocked as exc:
            self._outcome_evaluator.invalidate("red_decision_blocked")
            self._set_runtime_state("paused_model", "red_commander")
            self.allocator.sm.add_event("red_decision_blocked", {"reason": str(exc)})
            raise
        self._set_runtime_state("running")
        commands = {} if plan is None else {command.ship_id: command for command in plan.commands}
        plan_id = None if plan is None else plan.snapshot_id
        new_plan = plan_id != self._installed_red_plan_id
        for ship in self.ships:
            params = (
                commands.get(ship.id)
                if ship.vessel_class == "type_ii"
                and any(item[0] == ship.id for item in snapshot.active_signature)
                else None
            )
            if params != ship._navigation_params or (params is not None and new_plan):
                ship.navigator.install(params, current_time)
                if params is not None:
                    self.allocator.sm.add_event("opponent_maneuver_installed", {
                        "side": "blue", "vessel_id": ship.id,
                        "plan_id": plan_id, "parameters": asdict(params),
                    })
        self._installed_red_plan_id = plan_id

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
            if (
                lease.owner is ControlOwner.SYSTEM
                and self.control_coordinator.operation_mode(uav.id) is OperationMode.RETURN
            ):
                self._divert_blocked_return(uav, current_time)
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
        self._publish_control_routes()
        fuel_low = (
            uav.fuel_remaining_pct <= 0.08
            and uav.status not in ("returning", "idle", "refueling")
            and not getattr(uav, "_fuel_low_reported", False)
        )
        if fuel_low:
            uav._fuel_low_reported = True
        return fuel_low

    def _publish_fuel_warning(self, uav: UAVEntity, current_time: float) -> None:
        """Publish the proactive fuel edge before or after one control tick."""
        if (
            uav.fuel_remaining_pct <= 0.25
            and not uav.fuel_warning_sent
            and uav.status in ("searching", "tracking", "transit")
        ):
            uav.fuel_warning_sent = True
            self.allocator.trigger_manager.notify_event(
                "uav_fuel_low_warning",
                time=current_time,
                uav_id=uav.id,
                fuel_pct=round(uav.fuel_remaining_pct, 3),
                status=uav.status,
            )
            self.allocator.sm.add_event("uav_fuel_low_warning", {
                "uav_id": uav.id,
                "fuel_pct": round(uav.fuel_remaining_pct, 3),
                "status": uav.status,
            })

    def _record_control_tick(self, uav: UAVEntity, tick) -> None:
        """Bridge immutable control output into legacy entity diagnostics."""
        previous_task = self._coordinator_tasks.get(uav.id)
        active_task = self.control_coordinator.active_task(uav.id)
        if active_task is not None:
            self._coordinator_tasks[uav.id] = active_task
            record = self._mission_task_records.get(active_task.task_id)
            if record is not None and record.status == "approved":
                self._mission_task_records[active_task.task_id] = replace(
                    record,
                    status="executing",
                    started_at_min=tick.observation.timestamp_min,
                )
        command = tick.execution.applied_command
        previous_target = uav.target_group_id
        if command.operation_mode is OperationMode.TRACK:
            uav.target_group_id = self.allocator.sm.merged_contact_aliases.get(
                command.target_contact_id, command.target_contact_id)
            if command.target_contact_id:
                uav._mission_kind = "track_entry"
                if (
                    previous_target != uav.target_group_id
                    or uav.id not in self._tracking_started_at
                ):
                    self._tracking_started_at[uav.id] = tick.observation.timestamp_min
        elif command.operation_mode is OperationMode.PROBE:
            uav.target_group_id = self.allocator.sm.merged_contact_aliases.get(
                command.target_contact_id, command.target_contact_id)
            if command.target_contact_id:
                uav._mission_kind = "probe"
        elif command.operation_mode not in (OperationMode.TRACK,):
            uav.target_group_id = None
        if command.operation_mode is OperationMode.HOLDING:
            self._promote_work_controller_to_holding(
                uav, tick.observation.timestamp_min,
            )
        for event in tick.emitted_events:
            event_generation = event.payload.get("generation")
            if (
                event_generation is not None
                and event_generation != tick.lease.generation
            ):
                continue
            event_task_id = event.payload.get("task_id")
            current_task = self.control_coordinator.active_task(uav.id)
            if (
                event_task_id is not None
                and (current_task is None or event_task_id != current_task.task_id)
                and (previous_task is None or event_task_id != previous_task.task_id)
            ):
                continue
            if event.event_type == "coverage_route_finished":
                self._queue_pending_coverage_completion(
                    uav,
                    event,
                    previous_task,
                    tick.lease.generation,
                )
            elif event.event_type == "search_complete":
                self._record_search_completion_event(uav, event, previous_task)
            elif event.event_type == "task_failed":
                failed_task = previous_task
                if (
                    failed_task is None
                    or failed_task.task_id != event.payload.get("task_id")
                ):
                    failed_task = self.control_coordinator.active_task(uav.id)
                if failed_task is not None and failed_task.task_id == event.payload.get("task_id"):
                    self._close_mission_task(
                        uav.id,
                        failed_task,
                        status="blocked",
                        reason=str(event.payload.get("reason", "task_failed")),
                        current_time=event.timestamp_min,
                        preserve_search=failed_task.task_type is OperationMode.COVERAGE,
                    )
                self.allocator.sm.add_event("task_failed", {
                    "uav_id": uav.id,
                    **dict(event.payload),
                })

        if command.operation_mode is OperationMode.RETURN and uav.id in self._return_base_by_uav:
            base = self._return_base_by_uav[uav.id]
            # A fixed-wing return controller may still be turning when it
            # crosses the exact base coordinate. Capture the final approach
            # before the next safety tick can carry it beyond the map edge.
            capture_radius = max(
                0.05,
                uav.R_min
                + self._control_action_spec().max_speed_cells_min * self.clock.dt_min,
            )
            if math.dist(
                uav.float_position,
                (base.position.col, base.position.row),
            ) <= capture_radius:
                uav.position = base.position
                uav.status = "refueling"
                uav.sensor_mode = "off"
                self._land_for_refuelling(uav)

    def _queue_pending_coverage_completion(
        self,
        uav: UAVEntity,
        event: ControlEvent,
        previous_task: ControlTask | None,
        lease_generation: int,
    ) -> None:
        """Defer route completion until this tick's real SAR footprint is recorded."""
        task_id = event.payload.get("task_id")
        # Completion events are asynchronous and must carry the generation
        # they were emitted for. Falling back to the current lease can turn an
        # old generation's event into a completion for a reassigned task.
        generation = event.payload.get("generation")
        if not isinstance(task_id, str) or not task_id:
            return
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation != lease_generation
        ):
            return
        task = self.control_coordinator.active_task(uav.id)
        if (
            task is None
            or task.task_type is not OperationMode.COVERAGE
            or task.task_id != task_id
        ):
            task = previous_task
        if (
            task is None
            or task.task_type is not OperationMode.COVERAGE
            or task.task_id != task_id
        ):
            return
        if any(
            item["uav_id"] == uav.id
            and item["task_id"] == task_id
            and item["generation"] == generation
            for item in self._pending_coverage_completions
        ):
            return
        self._pending_coverage_completions.append({
            "uav_id": uav.id,
            "task_id": task_id,
            "generation": generation,
            "event": event,
            "previous_task": task,
        })

    def _finalize_coverage_completions(self, current_time: float) -> None:
        """Validate queued route finishes against the actual SAR task ledger."""
        pending = self._pending_coverage_completions
        self._pending_coverage_completions = []
        service = self.allocator.sm.coverage_service
        if service is None:
            return
        for item in pending:
            uav_id = item["uav_id"]
            task_id = item["task_id"]
            generation = item["generation"]
            if not isinstance(uav_id, str) or not isinstance(task_id, str):
                continue
            if isinstance(generation, bool) or not isinstance(generation, int):
                continue
            uav = next((entity for entity in self.uavs if entity.id == uav_id), None)
            if uav is None:
                continue
            active = self.control_coordinator.active_task(uav_id)
            lease = self.control_coordinator.current_lease(uav_id)
            if (
                active is None
                or active.task_type is not OperationMode.COVERAGE
                or active.task_id != task_id
                or lease.generation != generation
            ):
                self.allocator.sm.add_event("coverage_completion_ignored", {
                    "uav_id": uav_id,
                    "task_id": task_id,
                    "generation": generation,
                    "reason": "stale_generation",
                })
                continue
            try:
                completion = service.finish(
                    task_id,
                    generation,
                    current_time,
                    uav_id=uav_id,
                )
            except ValueError as exc:
                self.allocator.sm.add_event("coverage_completion_ignored", {
                    "uav_id": uav_id,
                    "task_id": task_id,
                    "generation": generation,
                    "reason": "unknown_task",
                    "error": str(exc),
                })
                continue
            event = item["event"]
            if not isinstance(event, ControlEvent):
                continue
            if completion.complete:
                self._record_search_completion_event(
                    uav,
                    event,
                    item.get("previous_task"),
                    completion=completion,
                    coverage_generation=generation,
                )
                self._queue_control_event(
                    "search_complete",
                    uav_id,
                    current_time,
                    {
                        "task_id": task_id,
                        "generation": generation,
                    },
                )
                continue

            task = active
            self._close_mission_task(
                uav_id,
                task,
                status="blocked",
                reason="coverage_incomplete",
                current_time=current_time,
                preserve_search=True,
                coverage_generation=generation,
            )
            region = next(
                (
                    item_region
                    for item_region in self.allocator.sm.get_search_regions()
                    if item_region.id == task_id
                ),
                None,
            )
            if region is not None:
                region.completion_pct = completion.completion_pct
                region.completion_basis = "task_sar"
            self.allocator.sm.add_event("coverage_incomplete", {
                "uav_id": uav_id,
                "task_id": task_id,
                "generation": generation,
                "scanned_cells": completion.scanned_cells,
                "required_cells": completion.required_cells,
                "completion_pct": completion.completion_pct,
                "missing_cells": [list(cell) for cell in completion.missing_cells],
            })
            self._queue_control_event(
                "task_failed",
                uav_id,
                current_time,
                {
                    "task_id": task_id,
                    "generation": generation,
                    "reason": "coverage_incomplete",
                    "missing_cells": [list(cell) for cell in completion.missing_cells],
                },
            )

    def _close_mission_task(
        self,
        uav_id: str,
        task: ControlTask,
        *,
        status: str,
        reason: str,
        current_time: float,
        preserve_search: bool = False,
        preserve_contact: bool = False,
        coverage_generation: int | None = None,
        allow_missing_search_region: bool = False,
    ) -> None:
        """Close one mission binding without touching a replacement task."""
        sm = self.allocator.sm
        active = self.control_coordinator.active_task(uav_id)
        owns_active = active is not None and active.task_id == task.task_id
        replacement_targets_contact = (
            active is not None
            and not owns_active
            and active.target_contact_id is not None
            and task.target_contact_id is not None
            and sm.resolve_contact_id(active.target_contact_id)
            == sm.resolve_contact_id(task.target_contact_id)
        )
        record_before = self._mission_task_records.get(task.task_id)

        if task.task_type is OperationMode.COVERAGE:
            service = sm.coverage_service
            if coverage_generation is None:
                coverage_generation = self._coverage_assignment_generations.get(
                    (uav_id, task.task_id)
                )
            if service is not None and coverage_generation is not None:
                service.close(
                    task.task_id,
                    coverage_generation,
                    reason,
                    uav_id=uav_id,
                )
            projection_state = (
                "pending"
                if preserve_search
                else "completed"
                if status == "completed"
                else "stale"
            )
            self._set_search_task_projection(
                task.task_id,
                state=projection_state,
                uav_id=None,
                current_time=current_time,
                reason=reason,
                allow_missing_region=allow_missing_search_region,
            )

        if task.target_contact_id is not None:
            try:
                contact_id = sm.resolve_contact_id(task.target_contact_id)
                contact = sm.contacts.snapshot(contact_id)
            except KeyError:
                contact_id = None
                contact = None
            if contact_id is not None:
                session = (
                    sm.get_probe_session(task.probe_id)
                    if task.probe_id is not None else None
                )
                if (
                    session is not None
                    and session.uav_id == uav_id
                    and sm.resolve_contact_id(session.contact_id) == contact_id
                ):
                    sm.clear_probe_session(session.probe_id)
                if not preserve_contact and not replacement_targets_contact:
                    expected_probe = (
                        task.probe_id
                        if task.task_type is OperationMode.PROBE else None
                    )
                    if (
                        contact.assigned_uav_id == uav_id
                        and contact.active_probe_id == expected_probe
                    ):
                        sm.contacts.release(contact_id, current_time, reason)
                track = sm.get_track_region_for_group(contact_id)
                if (
                    track is not None
                    and track.assigned_uav_id == uav_id
                    and not replacement_targets_contact
                ):
                    sm.release_track_region(
                        track.id, source_uav_id=uav_id, create_marker=False
                    )

        if owns_active:
            sm.clear_uav_assignment(uav_id)
            if self._coordinator_tasks.get(uav_id) == task:
                self._coordinator_tasks.pop(uav_id, None)

        record = self._mission_task_records.get(task.task_id)
        if record is None:
            return
        if task.task_type is OperationMode.COVERAGE:
            desired = record
            if record_before == desired:
                return
        elif preserve_search:
            desired = replace(
                record,
                status="approved",
                assigned_uav_id=None,
                finished_at_min=None,
                release_reason=reason,
            )
        elif record.status in {"completed", "cancelled", "blocked"}:
            desired = record
        else:
            desired = replace(
                record,
                status=status,
                assigned_uav_id=None,
                finished_at_min=current_time,
                release_reason=reason,
            )
        if desired == record and task.task_type is not OperationMode.COVERAGE:
            return
        self._mission_task_records[task.task_id] = desired
        sm.current_time = max(float(sm.current_time), float(current_time))
        # Publishing the audit event alone does not reach TriggerManager.
        # Reuse its existing heavy event so one completed search (not only
        # three batched light events) refreshes rolling coverage decisions.
        self.allocator.trigger_manager.notify_event(
            "mission_task_released", time=current_time, uav_id=uav_id,
            task_id=task.task_id,
        )
        sm.add_event("mission_task_released", {
            "task_id": task.task_id,
            "uav_id": uav_id,
            "status": desired.status,
            "reason": reason,
            "contact_id": task.target_contact_id,
            "probe_id": task.probe_id,
        })

    def _start_coverage_service_task(
        self,
        uav: UAVEntity,
        task: ControlTask,
        current_time: float,
        *,
        generation: int | None = None,
    ) -> None:
        """Start task SAR accounting only after a lease assignment commits."""
        if task.task_type is not OperationMode.COVERAGE or task.region_bbox is None:
            return
        service = self.allocator.sm.coverage_service
        if service is None:
            return
        if generation is None:
            generation = self.control_coordinator.current_lease(uav.id).generation
        key = (uav.id, task.task_id)
        previous_generation = self._coverage_assignment_generations.get(key)
        if previous_generation == generation:
            return
        service.validate_start(
            task.task_id,
            generation,
            uav.id,
            tuple(task.region_bbox),
            current_time,
        )
        previous_progress = None
        latest_generation = service.latest_generation(task.task_id)
        if latest_generation is not None:
            previous_progress = service.progress(
                task.task_id,
                latest_generation,
            )
        if previous_generation is not None:
            service.close(
                task.task_id,
                previous_generation,
                "replaced",
                uav_id=uav.id,
            )
        service.start(
            task.task_id,
            generation,
            uav.id,
            tuple(task.region_bbox),
            current_time,
            initial_scanned_cells=(
                previous_progress.scanned_cells
                if previous_progress is not None else ()
            ),
        )
        self._coverage_assignment_generations[key] = generation
        self.allocator.sm.register_coverage_task_generation(
            task.task_id, generation, uav.id,
        )

    def _promote_work_controller_to_holding(
        self, uav: UAVEntity, current_time: float
    ) -> None:
        """Keep physical holding/refuelling aligned with SYSTEM ownership."""
        if not self.control_coordinator.has_controller(uav.id):
            return
        lease = self.control_coordinator.current_lease(uav.id)
        if lease.owner not in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
            return
        previous_task = self.control_coordinator.active_task(uav.id)
        if previous_task is not None and previous_task.task_type not in {
            OperationMode.HOLDING,
            OperationMode.RETURN,
        }:
            self._close_mission_task(
                uav.id,
                previous_task,
                status="blocked",
                reason="holding",
                current_time=current_time,
                preserve_search=previous_task.task_type is OperationMode.COVERAGE,
            )
        task = ControlTask(
            f"holding:{uav.id}:{current_time}", OperationMode.HOLDING,
        )
        self.control_coordinator.promote_to_system_holding(
            uav.id,
            current_time=current_time,
            task_id=task.task_id,
        )
        self._coordinator_tasks[uav.id] = task

    def _record_search_completion_event(
        self,
        uav: UAVEntity,
        event: ControlEvent,
        previous_task: ControlTask | None = None,
        *,
        completion: CoverageCompletion | None = None,
        coverage_generation: int | None = None,
    ) -> None:
        task = self.control_coordinator.active_task(uav.id)
        if (
            (task is None or task.task_type is not OperationMode.COVERAGE)
            and previous_task is not None
            and previous_task.task_type is OperationMode.COVERAGE
            and previous_task.task_id == event.payload.get("task_id")
        ):
            task = previous_task
        region_id = task.task_id if task and task.task_type is OperationMode.COVERAGE else None
        if region_id is None:
            region_id = event.payload.get("task_id")
        has_search_region = any(
            item.id == region_id and item.type == "search"
            for item in self.allocator.sm.get_search_regions()
        )
        if task is not None and task.task_type is OperationMode.COVERAGE:
            self._close_mission_task(
                uav.id,
                task,
                status="completed",
                reason="search_complete",
                current_time=event.timestamp_min,
                coverage_generation=coverage_generation,
                allow_missing_search_region=not has_search_region,
            )
        region = next(
            (
                item
                for item in self.allocator.sm.get_search_regions()
                if item.id == region_id
            ),
            None,
        )
        if region is not None:
            self._set_search_task_projection(
                region.id,
                state="completed",
                uav_id=None,
                current_time=event.timestamp_min,
                reason="search_complete",
            )
            region.completion_pct = (
                completion.completion_pct if completion is not None else 100.0
            )
            region.completion_basis = (
                "task_sar" if completion is not None else "legacy_observation"
            )
        uav.completed_searches_since_refuel += 1
        self._sortie_searched[uav.id] = True
        self.allocator.sm.clear_uav_assignment(uav.id)
        self.allocator.trigger_manager.notify_event(
            "search_complete",
            time=event.timestamp_min,
            uav_id=uav.id,
            region_id=region_id,
        )
        payload = {
            "uav_id": uav.id,
            "region_id": region_id,
        }
        if completion is not None:
            payload.update({
                "completion_basis": "task_sar",
                "scanned_cells": completion.scanned_cells,
                "required_cells": completion.required_cells,
                "completion_pct": completion.completion_pct,
            })
        self.allocator.sm.add_event("search_complete", payload)

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
        previous_task = self.control_coordinator.active_task(uav.id)
        reserve_cells = self.config.control.safety.reserve_range_cells
        max_speed = self._control_action_spec().max_speed_cells_min
        base_capacity_available = any(
            self._base_maintenance_load(base) < base.capacity
            for base in self.bases
        )
        if not force and base_capacity_available:
            # Recovery planning can be temporarily impossible while a
            # fixed-wing controller is completing an edge turn.  Do not turn
            # that transient heading into a failure while the airframe still
            # has a conservative direct-to-base fuel margin.
            available_bases = [
                base for base in self.bases
                if self._base_maintenance_load(base) < base.capacity
            ]
            nearest_base_distance = min(
                math.dist(
                    uav.float_position,
                    (base.position.col, base.position.row),
                )
                for base in available_bases
            )
            conservative_margin = (
                nearest_base_distance
                + 2.0 * math.pi * uav.R_min
                + reserve_cells
                + max_speed * self.clock.dt_min
            )
            if uav.remaining_range_cells > conservative_margin:
                return False
        planner = RecoveryPlanner()
        base_observations = self._control_base_observations()
        allow_reserved_bases = False
        try:
            candidates = planner.evaluate(
                uav.pose,
                uav.remaining_range_cells,
                base_observations,
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
            # A full base rejects a new refuelling slot, not a safe inbound
            # flight. Let the airframe reserve the route and enter holding
            # on arrival when every open slot is infeasible.
            allow_reserved_bases = True
            try:
                candidates = planner.evaluate(
                    uav.pose,
                    uav.remaining_range_cells,
                    base_observations,
                    self.allocator.sm.obstacle_mask,
                    self.allocator.sm.obstacle_version,
                    uav.R_min,
                    reserve_cells,
                    allow_reserved_bases=True,
                )
            except (RuntimeError, ValueError) as exc:
                candidates = ()
                planning_error = str(exc)
        if not candidates:
            error = NoSafeRecoveryPath(
                "none",
                self.allocator.sm.obstacle_version,
                planning_error,
            )
            self._emit_no_safe_recovery_path(uav, current_time, error)
            raise error

        candidate = candidates[0]
        threshold = (
            candidate.path_length_cells
            + candidate.reserve_cells
            + max_speed * self.clock.dt_min
        )
        if (
            not force
            and not allow_reserved_bases
            and uav.remaining_range_cells > threshold
        ):
            return False

        base = next(
            (item for item in self.bases if item.id == candidate.base.base_id),
            None,
        )
        if base is None or (
            not allow_reserved_bases
            and self._base_maintenance_load(base) >= base.capacity
        ):
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
        if previous_task is not None and previous_task.task_type not in {
            OperationMode.RETURN,
            OperationMode.HOLDING,
        }:
            self._close_mission_task(
                uav.id,
                previous_task,
                status="blocked",
                reason="fuel_return" if force else "range_reserve",
                current_time=current_time,
                preserve_search=previous_task.task_type is OperationMode.COVERAGE,
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

    def _divert_blocked_return(self, uav: UAVEntity, current_time: float) -> bool:
        """Replace a blocked inbound plan and its capacity reservation together."""
        task = self.control_coordinator.active_task(uav.id)
        if task is None or task.recovery_plan is None:
            return False
        exported = self.control_coordinator.route_snapshot(uav.id).route
        suffix = (exported.route[exported.next_index:]
                  if exported.status == "ready" else task.recovery_plan.path[1:])
        if not recovery_route_blocked((uav.pose, *suffix), self.allocator.sm.obstacle_mask):
            return False
        bases = tuple(
            BaseObservation(
                base.id, tuple(map(float, base.position)), base.capacity,
                self._base_maintenance_load(base, exclude_uav_id=uav.id),
            ) for base in self.bases
        )
        planner = RecoveryPlanner()
        args = (uav.pose, uav.remaining_range_cells, bases,
                self.allocator.sm.obstacle_mask, self.allocator.sm.obstacle_version,
                uav.R_min, task.recovery_plan.reserve_cells)
        candidates = planner.evaluate(*args)
        if not candidates:
            # Full bases still permit a safe inbound flight followed by holding.
            candidates = planner.evaluate(*args, allow_reserved_bases=True)
        if not candidates:
            raise NoSafeRecoveryPath(task.recovery_plan.base_id,
                                     self.allocator.sm.obstacle_version,
                                     "no base has a safe route within fuel reserve")
        candidate = candidates[0]
        base = next(base for base in self.bases if base.id == candidate.base.base_id)
        reservation_id = f"{uav.id}:return:{self._return_reservation_sequence}"
        plan = RecoveryPlan(base.id, candidate.base.position, reservation_id,
                            candidate.path, candidate.path_length_cells,
                            candidate.reserve_cells, candidate.planning_map_version)
        replacement = ControlTask(reservation_id, OperationMode.RETURN, recovery_plan=plan)
        previous = self._return_base_by_uav.get(uav.id)
        self._return_base_by_uav[uav.id] = base
        try:
            self.control_coordinator.assign_system_task(
                uav.id, replacement, current_time=current_time)
        except Exception:
            if previous is None:
                self._return_base_by_uav.pop(uav.id, None)
            else:
                self._return_base_by_uav[uav.id] = previous
            raise
        self._return_reservation_sequence += 1
        self._coordinator_tasks[uav.id] = replacement
        uav.plan_return(plan.path)
        self.allocator.sm.add_event("return_diverted", {
            "uav_id": uav.id, "previous_base_id": task.recovery_plan.base_id,
            "base_id": base.id, "reservation_id": reservation_id,
            "path_length_cells": plan.path_length_cells,
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
            else "probe_validation_error"
            if isinstance(error, ProbeValidationError)
            else "unsafe_control_state"
            if isinstance(error, UnsafeControlState)
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

    def _require_handoff(self, uav: UAVEntity, report, current_time: float):
        """Create one projected handoff fact before releasing a tracker."""
        if report is None or not uav.target_group_id:
            return None
        sm = self.allocator.sm
        successors = tuple(
            candidate.id for candidate in sm.get_available_uavs()
            if candidate.id != uav.id
        )
        existing_ids = {
            attempt.handoff_id for attempt in self.handoff_manager.attempts()
        }
        attempt = self.handoff_manager.require(
            report.contact_id,
            source_uav_id=uav.id,
            required_at_min=current_time,
            last_position=(float(report.position.col), float(report.position.row)),
            velocity_cells_min=report.velocity_cells_per_min,
            last_observed_at_min=report.observed_at,
        )
        if attempt.handoff_id not in existing_ids:
            evidence = self.handoff_manager.evidence(attempt.handoff_id)
            if evidence.mean is not None and evidence.covariance_cells2 is not None:
                fact = EvidenceRecord(
                    evidence_id=attempt.evidence_id,
                    kind="handoff",
                    source_id=report.contact_id,
                    contact_id=report.contact_id,
                    observed_at_min=current_time,
                    expires_at_min=current_time + 10.0,
                    strength=1.0,
                    spatial=CovarianceKernel(
                        mean_cells=evidence.mean,
                        covariance_cells2=evidence.covariance_cells2,
                    ),
                )
                delta = sm.apply_information_facts([fact], current_time)
                self._publish_information_delta(delta, current_time)
            self._outcome_evaluator.register_handoff(
                attempt.handoff_id,
                at_min=current_time,
                successor_uav_ids=successors,
            )
            sm.add_event("handoff_required", {
                "handoff_id": attempt.handoff_id,
                "contact_id": attempt.contact_id,
                "source_uav_id": attempt.source_uav_id,
                "successor_uav_ids": list(successors),
                "evidence_id": attempt.evidence_id,
            })
            self.allocator.trigger_manager.notify_event(
                "handoff_required",
                time=current_time,
                uav_id=uav.id,
                contact_id=report.contact_id,
            )
        return attempt

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
            self._require_handoff(uav, report, current_time)
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
                        "contact_id": report.contact_id,
                        "position": report.position,
                        "observed_at": report.observed_at,
                    })
            if report is not None:
                sm.release_contact_reservation(group_id, uav.id, current_time, "uav_return")
        uav.target_group_id = None
        for region in sm.get_search_regions():
            if region.assigned_uav_id == uav.id:
                record = self._mission_task_records.get(region.id)
                if record is None or record.kind == "search":
                    self._set_search_task_projection(
                        region.id,
                        state="pending",
                        uav_id=None,
                        current_time=current_time,
                        reason="uav_return",
                    )
                else:
                    region.assigned_uav_id = None
        sm.clear_uav_assignment(uav.id)
        uav.status = "returning"
        uav.sensor_mode = "off"

    def _enter_emergency_failure(
        self, uav: UAVEntity, reason: str, error: Exception
    ) -> None:
        """Freeze one airframe and release every mission binding it owned."""
        if uav.id in self._emergency_failures:
            return

        current_time = float(self.clock.time)
        sm = self.allocator.sm
        active_task = self.control_coordinator.active_task(uav.id)
        if active_task is None:
            active_task = self._coordinator_tasks.get(uav.id)
        lease = self.control_coordinator.current_lease(uav.id)

        if active_task is not None and active_task.task_type in {
            OperationMode.COVERAGE,
            OperationMode.PROBE,
            OperationMode.TRACK,
        }:
            self._close_mission_task(
                uav.id,
                active_task,
                status="blocked",
                reason=reason,
                current_time=current_time,
                coverage_generation=(
                    lease.generation
                    if active_task.task_type is OperationMode.COVERAGE
                    else None
                ),
                preserve_search=active_task.task_type is OperationMode.COVERAGE,
            )

        # A stale coordinator mirror or an already detached task record must
        # not keep the failed UAV in the prompt's active-task snapshot.
        for task_id, record in tuple(self._mission_task_records.items()):
            if record.assigned_uav_id != uav.id:
                continue
            if record.status in {"completed", "cancelled", "blocked"}:
                continue
            if record.kind == "search":
                self._set_search_task_projection(
                    task_id,
                    state="pending",
                    uav_id=None,
                    current_time=current_time,
                    reason=reason,
                )
            else:
                self._mission_task_records[task_id] = replace(
                    record,
                    status="blocked",
                    assigned_uav_id=None,
                    finished_at_min=current_time,
                    release_reason=reason,
                )

        # Keep the failed area's responsibility visible and reusable by a
        # healthy UAV while preserving the failure reason in the audit record.
        for region in sm.get_search_regions():
            if region.assigned_uav_id == uav.id:
                record = self._mission_task_records.get(region.id)
                if record is None or record.kind == "search":
                    self._set_search_task_projection(
                        region.id,
                        state="pending",
                        uav_id=None,
                        current_time=current_time,
                        reason=reason,
                    )
                else:
                    region.assigned_uav_id = None
        sm.clear_uav_assignment(uav.id)

        contact_ids = {
            contact.contact_id
            for contact in sm.contacts.list_snapshots()
            if contact.assigned_uav_id == uav.id
        }
        if active_task is not None and active_task.target_contact_id:
            contact_ids.add(active_task.target_contact_id)
        for contact_id in sorted(contact_ids):
            self._clear_surveillance_contact(contact_id, current_time, reason)
        self.control_coordinator.operation_registry.release_uav(
            uav.id, current_time=current_time, reason=reason,
        )
        for region in tuple(sm.get_track_regions()):
            if region.assigned_uav_id == uav.id:
                sm.release_track_region(
                    region.id, source_uav_id=uav.id, create_marker=False,
                )

        service = sm.coverage_service
        if service is not None:
            for (assigned_uav_id, task_id), generation in tuple(
                self._coverage_assignment_generations.items()
            ):
                if assigned_uav_id != uav.id:
                    continue
                try:
                    service.close(
                        task_id, generation, reason, uav_id=uav.id,
                    )
                except ValueError:
                    pass
                self._coverage_assignment_generations.pop(
                    (assigned_uav_id, task_id), None,
                )

        # Remove both in-flight and waiting base reservations. No refuel
        # completion is recorded for an airframe that failed in the queue.
        self._return_base_by_uav.pop(uav.id, None)
        self._holding_base_by_uav.pop(uav.id, None)
        for base in self.bases:
            base.remove_uav(uav.id)

        self._coordinator_tasks.pop(uav.id, None)
        self._pending_coverage_completions = [
            item for item in self._pending_coverage_completions
            if item.get("uav_id") != uav.id
        ]
        self._search_started_at.pop(uav.id, None)
        self._tracking_started_at.pop(uav.id, None)
        self._ais_tracking_started_at.pop(uav.id, None)
        self._ais_measurements.pop(uav.id, None)
        uav.assigned_region = None
        uav.target_group_id = None
        uav.waypoints = []
        uav.planned_path = []
        uav.mission_route = []
        uav._wp_index = 0
        uav._transit_end_index = 0
        uav._scan_ranges = []
        uav._mission_kind = ""
        uav.avoidance_path = []
        uav._avoidance_index = 0
        uav.eo_fov = None
        uav.sar_look_direction = None
        uav.sar_footprint = []
        uav.sar_aperture_track = []
        uav._clear_sar_acquisition()
        uav.search_complete_pending = False
        uav.last_requested_command = None
        uav.last_applied_command = None
        uav.last_safety_interventions = ()
        uav.request_active_mode("standby")
        uav.sensor_mode = "off"

        self._emergency_failures[uav.id] = reason
        self._outcome_evaluator.invalidate(reason)
        lease = self.control_coordinator.quarantine_uav(
            uav.id, current_time=current_time, reason=reason,
        )
        self._publish_control_routes()
        uav.status = "failed"
        state = sm.get_uav(uav.id)
        if state is not None:
            state.operational_status = "failed"
            state.failure_reason = reason
            sm.update_uav_status(
                uav.id,
                "failed",
                uav.position,
                fuel_remaining_pct=uav.fuel_remaining_pct,
                heading_deg=uav.heading_deg,
                sensor_mode="off",
            )
            sm.update_uav_control(
                uav.id,
                self.control_coordinator.configured_mode(uav.id).value,
                lease.owner.value,
                OperationMode.IDLE.value,
                lease.generation,
                False,
            )

        self.allocator.trigger_manager.notify_event(
            "emergency_failure",
            time=current_time,
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
        if event_type == "duplicate_task_cancelled":
            task = self.control_coordinator.active_task(uav_id)
            lease = self.control_coordinator.current_lease(uav_id)
            payload = {**(payload or {}),
                       "task_id": task.task_id if task else None,
                       "lease_generation": lease.generation,
                       "controller_id": lease.controller_id}
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
            previous_time = self.clock.time
            result = self.step()
            if on_step is not None:
                on_step(self, result)
            if self.clock.time == previous_time or self.runtime_status != "running":
                break
        return self.summary()

    def summary(self) -> dict:
        coverage = self.allocator.sm.get_coverage_stats()
        outcome = self._outcome_evaluator.snapshot()
        return {
            "episode_id": self.episode_id,
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
            "episode_outcome": asdict(outcome),
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

    def _coverage_execution_config(self) -> CoverageExecutionConfig:
        """Publish one immutable geometry contract for the homogeneous fleet."""
        first = self.uavs[0]
        expected = (
            first.sar_sensor.swath_width_cells,
            first.sar_sensor.near_range_cells,
            first.R_min,
            first.sar_along_track_cells,
            first.sar_heading_tolerance_rad,
        )
        for uav in self.uavs[1:]:
            actual = (
                uav.sar_sensor.swath_width_cells,
                uav.sar_sensor.near_range_cells,
                uav.R_min,
                uav.sar_along_track_cells,
                uav.sar_heading_tolerance_rad,
            )
            if actual != expected:
                raise ValueError("heterogeneous UAV SAR geometry requires per-UAV factory mapping")
        return CoverageExecutionConfig(
            swath_width_cells=expected[0],
            near_range_cells=expected[1],
            min_turn_radius_cells=expected[2],
            along_track_cells=expected[3],
            heading_tolerance_rad=expected[4],
            cross_track_tolerance_cells=0.2,
        )

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
        for ship in self.ships:
            try:
                ship.step(self.clock.dt_min, islands, current_time=current_time)
            except TypeError as exc:
                # Keep legacy replay/test adapters with the original two-argument
                # step signature working while the production Ship receives time.
                if "current_time" not in str(exc):
                    raise
                ship.step(self.clock.dt_min, islands)
            history = self._ship_position_history.setdefault(ship.id, [])
            history.append((float(current_time), tuple(ship.float_position)))
            del history[:-600]
            if ship.departed and ship.contact_id not in self._departed_contacts:
                self._departed_contacts.add(ship.contact_id)
                ship.set_tracked(False)
                self.departed_ship_count += 1

    def _publish_information_delta(self, delta, at_min: float) -> None:
        if delta is not None:
            self.allocator.trigger_manager.notify_information_delta(
                delta, time=at_min,
            )

    def _refresh_ais_signals(self, current_time: float) -> None:
        """Ingest satellite AIS globally, including forced runtime refreshes."""
        interval = self.config.ship.ais_update_interval_min
        last_update = getattr(self, "_last_ais_update", float("-inf"))
        regular_due = current_time - last_update >= interval
        forced_ids = set(self._ais_force_refresh_ids)
        if not regular_due and not forced_ids:
            return
        ais_facts: list[EvidenceRecord] = []
        for ship in self.ships:
            if not regular_due and ship.id not in forced_ids:
                continue
            if not ship.departed:
                signal = generate_ais_signal(ship, current_time)
                ship.set_ais_signal(signal)
                if signal is not None:
                    self._ais_force_refresh_ids.discard(ship.id)
                    contact_id = self.allocator.sm.contacts.ingest_ais(signal, current_time)
                    self._vessel_contact_ids[ship.id].add(contact_id)
                    self._ais_history[signal.mmsi].append((
                        float(signal.timestamp),
                        tuple(signal.reported_position),
                        signal.mmsi,
                    ))
                    self._ais_history[signal.mmsi] = self._ais_history[signal.mmsi][-64:]
                    # Initialization keeps legacy AIS contacts available to
                    # the operator, but does not create planning evidence
                    # before the edit window closes.
                    if current_time > 0.0 or not self.editing_allowed:
                        if self.allocator.sm.ais_updates.is_enabled(signal.mmsi):
                            ais_facts.append(EvidenceRecord(
                                evidence_id=(
                                    f"AIS-EVIDENCE:{signal.mmsi}:"
                                    f"{float(signal.timestamp).hex()}"
                                ),
                                kind="ais_position",
                                source_id=signal.mmsi,
                                contact_id=contact_id,
                                observed_at_min=float(signal.timestamp),
                                expires_at_min=float(signal.timestamp) + 15.0,
                                strength=0.35,
                                spatial=PointKernel(
                                    mean_cells=tuple(signal.reported_position),
                                    sigma_cells=max(
                                        0.05,
                                        self.config.ship.ais_position_noise_cells,
                                    ),
                                ),
                            ))
        if regular_due:
            self._last_ais_update = current_time
        if ais_facts:
            delta = self.allocator.sm.apply_information_facts(ais_facts, current_time)
            self._publish_information_delta(delta, current_time)
        self._publish_contact_events(current_time)

    @staticmethod
    def _position_from_history(
        history: list[tuple[float, tuple[float, float]]],
        at_min: float,
    ) -> tuple[float, float] | None:
        if not history:
            return None
        if at_min <= history[0][0]:
            return history[0][1]
        for (left_time, left), (right_time, right) in zip(history, history[1:]):
            if left_time <= at_min <= right_time:
                span = right_time - left_time
                if span <= 1e-12:
                    return right
                fraction = (at_min - left_time) / span
                return (
                    left[0] + fraction * (right[0] - left[0]),
                    left[1] + fraction * (right[1] - left[1]),
                )
        return history[-1][1]

    def _record_uav_position_history(self, current_time: float) -> None:
        for uav in self.uavs:
            history = self._uav_position_history.setdefault(uav.id, [])
            position = tuple(uav.float_position)
            if history and abs(history[-1][0] - current_time) <= 1e-9:
                history[-1] = (float(current_time), position)
            else:
                history.append((float(current_time), position))
            del history[:-600]

    def _update_passive_sensors(self, current_time: float) -> None:
        """Sample active emitters on one absolute clock and publish facts."""
        sm = self.allocator.sm
        interval = float(self.config.sensor.passive.measurement_interval_min)
        for ship in self.ships:
            if ship.radar_emitter is not None:
                ship.radar_emitter.advance(current_time)

        while self._next_passive_sample_min <= current_time + 1e-9:
            sample_time = self._next_passive_sample_min
            self._passive_sample_index += 1
            sample_id = f"{self.episode_id}:passive:{self._passive_sample_index:08d}"
            observations: list[PassiveBearingObservation] = []
            grouped: dict[tuple[str, str, str], list[PassiveBearingObservation]] = defaultdict(list)
            for ship in self.ships:
                emitter = ship.radar_emitter
                track_id = self._emitter_track_ids.get(ship.id)
                if emitter is None or track_id is None or ship.departed:
                    continue
                burst = emitter.current_burst_at(sample_time)
                if burst is None or burst.burst_id is None:
                    continue
                emitter_position = self._position_from_history(
                    self._ship_position_history.get(ship.id, []), sample_time,
                )
                if emitter_position is None:
                    emitter_position = ship.float_position
                for uav in self.uavs:
                    if uav.id in self._emergency_failures:
                        continue
                    sensor = self.passive_sensors[uav.id]
                    observer_position = self._position_from_history(
                        self._uav_position_history.get(uav.id, []), sample_time,
                    ) or tuple(uav.float_position)
                    observation = sensor.observe(
                        sample_id,
                        uav.id,
                        observer_position,
                        track_id,
                        burst.burst_id,
                        emitter_position,
                        self.config.sensor.emitter.source_power_at_reference_db,
                        sample_time,
                        burst_active=True,
                    )
                    if observation is None:
                        continue
                    observations.append(observation)
                    grouped[(
                        observation.sample_id,
                        observation.emitter_track_id,
                        observation.burst_id,
                    )].append(observation)

            positions: list[PassivePosition] = []
            for key, group in sorted(grouped.items()):
                track_id = key[1]
                ship = next(
                    (item for item in self.ships
                     if self._emitter_track_ids.get(item.id) == track_id),
                    None,
                )
                if ship is None:
                    continue
                position = self._passive_position_resolver.release(
                    group,
                )
                if position is not None:
                    positions.append(position)

            activity_facts = []
            for position in positions:
                sm.register_passive_position(position)
                associated_contact = sm.passive_position_contact_id(
                    position.emitter_track_id,
                )
                emitter_ship = next(
                    (
                        item for item in self.ships
                        if self._emitter_track_ids.get(item.id)
                        == position.emitter_track_id
                    ),
                    None,
                )
                if emitter_ship is not None and associated_contact is not None:
                    self._set_surveillance_fact(
                        emitter_ship.id,
                        "passive",
                        True,
                        sample_time,
                        position.position_id,
                    )
                activity_facts.extend(sm.drain_radiation_activity_evidence())
            facts = [*observations, *positions, *activity_facts]
            if facts:
                sm.record_passive_observations(tuple(observations))
                delta = sm.apply_information_facts(facts, sample_time)
                self._publish_information_delta(delta, sample_time)
                for activity in activity_facts:
                    sm.add_event("radiation_activity_evidence", {
                        "evidence_id": activity.evidence_id,
                        "contact_id": activity.contact_id,
                        "source_id": activity.source_id,
                        "observed_at_min": activity.observed_at_min,
                    })
                for observation in observations:
                    sm.add_event("passive_bearing_observed", {
                        "observation_id": observation.observation_id,
                        "sample_id": observation.sample_id,
                        "emitter_track_id": observation.emitter_track_id,
                        "burst_id": observation.burst_id,
                        "observer_uav_id": observation.observer_uav_id,
                        "observer_position": list(observation.observer_position_cells),
                        "bearing_deg": observation.bearing_deg,
                    })
                for position in positions:
                    sm.add_event("passive_position_released", {
                        "position_id": position.position_id,
                        "sample_id": position.sample_id,
                        "emitter_track_id": position.emitter_track_id,
                        "burst_id": position.burst_id,
                        "position": list(position.position_cells),
                        "source_observation_ids": list(position.source_observation_ids),
                    })
            self._next_passive_sample_min += interval

    def _publish_contact_events(self, current_time: float) -> None:
        sm = self.allocator.sm
        events = list(sm.publish_contact_events())
        cancelled = {(e["contact_id"], e["uav_id"]) for e in events
                     if e["type"] == "duplicate_task_cancelled"}
        for event in events:
            if event["type"] == "duplicate_task_cancelled":
                uav = next(u for u in self.uavs if u.id == event["uav_id"])
                uav.target_group_id = None
                sm.clear_uav_assignment(uav.id)
                self._tracking_started_at.pop(uav.id, None)
                self._coordinator_tasks.pop(uav.id, None)
                if self.control_coordinator.has_controller(uav.id):
                    self._queue_control_event("duplicate_task_cancelled", uav.id, current_time, event)
            elif (event["type"] == "contact_merged"
                  and sm.contacts.snapshot(event["contact_id"]).state == "cleared"):
                self._release_target_group(event["contact_id"], current_time, "type_i_released")
            elif event["type"] == "type_i_released":
                self._release_target_group(event["contact_id"], current_time, "type_i_released")
            elif event["type"] == "contact_merged":
                cid = sm.resolve_contact_id(event["contact_id"])
                tasks = [(uav.id, self.control_coordinator.active_task(uav.id))
                         for uav in self.uavs]
                tasks = [(uid, task) for uid, task in tasks if task is not None
                         and task.target_contact_id
                         and sm.resolve_contact_id(task.target_contact_id) == cid]
                owner = event["assigned_uav_id"]
                if owner is None and tasks:
                    # Pending controllers have no operation binding yet. Prefer
                    # the canonical task, as the stores do for active tracks.
                    owner, _ = min(tasks, key=lambda item: (item[1].target_contact_id != cid, item[0]))
                    sm.contacts.reserve(cid, owner, None)
                for uav in self.uavs:
                    if uav.target_group_id and sm.resolve_contact_id(uav.target_group_id) == cid:
                        uav.target_group_id = cid
                for uid, task in tasks:
                    if uid != owner:
                        if (cid, uid) not in cancelled:
                            cancellation = {"type": "duplicate_task_cancelled",
                                            "contact_id": cid,
                                            "alias_contact_id": event["alias_contact_id"],
                                            "uav_id": uid, "probe_id": None}
                            events.append(cancellation)
                            cancelled.add((cid, uid))
                            sm.add_event(cancellation["type"], {
                                k: v for k, v in cancellation.items() if k != "type"})
                    elif task.target_contact_id != cid:
                        task = replace(task, target_contact_id=cid)
                        self.control_coordinator.assign_task(uid, task, current_time=current_time)
                        self._coordinator_tasks[uid] = task
            elif event["type"] == "contact_lost":
                self._clear_surveillance_contact(
                    event["contact_id"], current_time, "contact_lost",
                )
                self._release_target_group(event["contact_id"], current_time, "target_lost")
            if event["type"] in (
                "contact_created", "contact_merged", "contact_lost",
                "type_i_assessed", "type_ii_assessed",
                "type_i_released", "type_ii_confirmed",
            ):
                self.allocator.trigger_manager.notify_event(
                    event["type"], time=current_time,
                    **{key: value for key, value in event.items() if key != "type"})

    def _evaluate_evasion(self, current_time: float) -> None:
        """Derive AIS evasion facts from observed tracks, never from truth."""
        sm = self.allocator.sm
        for uav in self.uavs:
            mission_kind = str(getattr(uav, "mission_kind", "")).lower()
            if uav.status == "tracking" or mission_kind in {"probe", "track_entry"}:
                operation = "track" if uav.status == "tracking" else "probe"
            else:
                operation = "idle"
            self._evasion_observer_history.append({
                "uav_id": uav.id,
                "timestamp": float(current_time),
                "position": tuple(uav.float_position),
                "operation": operation,
            })
        self._evasion_observer_history = self._evasion_observer_history[-640:]
        cols, rows = self.config.grid.resolution
        boundary = (0.0, 0.0, float(cols), float(rows))
        for mmsi, history in tuple(self._ais_history.items()):
            if len(history) < 2:
                continue
            contact_id = next(
                (
                    contact.contact_id
                    for contact in sm.contacts.list_snapshots()
                    if contact.ais_mmsi == mmsi
                ),
                mmsi,
            )
            facts = self.evasion_detector.evaluate(
                history,
                self._evasion_observer_history,
                now_min=current_time,
                mission_boundary=boundary,
                obstacles=self.obstacles,
                contact_id=contact_id,
            )
            for fact in facts:
                delta = sm.apply_information_facts([fact], current_time)
                self._publish_information_delta(delta, current_time)
                sm.add_event("evasive_maneuver_detected", {
                    "fact_id": fact.fact_id,
                    "evasion_episode_id": fact.evasion_episode_id,
                    "episode_started": fact.episode_started,
                    "mmsi": fact.mmsi,
                    "contact_id": fact.contact_id,
                    "position": list(fact.position_cells),
                })

    def _expire_contacts(self, current_time: float) -> None:
        self.allocator.sm.contacts.expire(current_time)
        self._publish_contact_events(current_time)

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
                    self._set_search_task_projection(
                        region.id,
                        state="stale",
                        uav_id=None,
                        current_time=sm.current_time,
                        reason="route_blocked",
                    )
                    sm.clear_uav_assignment(uav.id)
                    sm.add_event("route_plan_failed", {
                        "uav_id": uav.id,
                        "region_id": region.id,
                        "error": str(exc),
                    })
                    self._begin_return(uav, sm.current_time)
                    continue
            elif uav.mission_kind == "track_entry" and uav.target_group_id:
                center = self._contact_center(uav.target_group_id)
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
        if sm.get_target_report(group_id) is not None:
            sm.contacts.release(sm.resolve_contact_id(group_id), current_time, "sensor_blocked")
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
                cells = tuple(sorted({(cell.col, cell.row) for cell in footprint}))
                footprint = [GridCoord(col, row) for col, row in cells]
                uav.sar_footprint = footprint
                for cell in footprint:
                    self._publish_information_delta(
                        sm.scan_cell(cell, current_time, is_track=False),
                        current_time,
                    )
                if cells:
                    sm.coverage_metrics.record_sar(cells, at_min=current_time)
                    task = self.control_coordinator.active_task(uav.id)
                    lease = self.control_coordinator.current_lease(uav.id)
                    if (
                        task is not None
                        and task.task_type is OperationMode.COVERAGE
                        and self.allocator.sm.coverage_service is not None
                        and self.allocator.sm.coverage_service.progress(
                            task.task_id,
                            lease.generation,
                            uav_id=uav.id,
                        ) is not None
                    ):
                        self.allocator.sm.coverage_service.record(
                            task.task_id,
                            lease.generation,
                            cells,
                            current_time,
                            uav_id=uav.id,
                        )
                    execution = self.dynamics_executor.coverage_execution
                    sm.add_event("sar_scan", {
                        "episode_id": self.episode_id,
                        "uav_id": uav.id,
                        "task_id": task.task_id if task is not None else None,
                        "generation": lease.generation,
                        "cells": [list(cell) for cell in cells],
                        "position": [float(uav.float_position[0]), float(uav.float_position[1])],
                        "heading_rad": float(uav.heading_rad),
                        "look_direction": uav.sar_look_direction,
                        "sar_scan_heading_rad": (
                            float(uav.sar_scan_heading_rad)
                            if uav.sar_scan_heading_rad is not None else None
                        ),
                        "sar_scan_origin": (
                            list(uav.sar_scan_origin)
                            if uav.sar_scan_origin is not None else None
                        ),
                        "swath_width_cells": float(getattr(
                            uav.sar_sensor,
                            "swath_width_cells",
                            execution.swath_width_cells,
                        )),
                        "near_range_cells": float(getattr(
                            uav.sar_sensor,
                            "near_range_cells",
                            execution.near_range_cells,
                        )),
                        "along_track_cells": float(uav.sar_along_track_cells),
                    })
                footprint_set = set(footprint)
                for ship in self.ships:
                    if ship.departed or ship.position not in footprint_set:
                        continue
                    if self.rng.random() <= uav.sar_sensor.detection_probability:
                        self._handle_detection(uav, ship, current_time)
            elif uav.status == "searching":
                # A search route may be in its entry/exit settling segment.
                # It moves normally, but invalid SAR samples never improve
                # the information field or produce a target detection.
                uav.sar_footprint = []
            elif (
                uav.status == "tracking"
                and uav.sensor_mode == "eo"
                and uav.target_group_id
            ):
                center = sm.contact_position(uav.target_group_id, current_time)
                if center is not None:
                    self._process_ais_tracking(uav, center, current_time)
            else:
                uav.sar_footprint = []
                if uav.status != "tracking":
                    uav.eo_fov = None
        self._evaluate_evasion(current_time)
        self._publish_contact_events(current_time)

    def _advance_probe_sessions(self, current_time: float) -> None:
        """Advance probes and assess only after the frozen evidence gate opens."""
        sm = self.allocator.sm
        for probe in sm.get_probe_sessions():
            try:
                contact = sm.contacts.snapshot(probe.contact_id)
            except KeyError:
                sm.clear_probe_session(probe.probe_id)
                continue
            advanced = advance_probe(
                probe,
                contact.samples,
                current_time,
                self.config.mission.contact,
            )
            if advanced != probe:
                sm.set_probe_session(advanced)
                if advanced.phase != probe.phase:
                    sm.add_event("probe_phase_changed", {
                        "probe_id": advanced.probe_id,
                        "contact_id": advanced.contact_id,
                        "phase": advanced.phase,
                    })

            if advanced.phase == "finished":
                self._finish_probe_session(contact, advanced, current_time)
                continue
            if advanced.phase != "awaiting_assessment":
                continue
            call_count = len(
                getattr(self.allocator.llm_client.gateway, "call_log", ())
            )
            try:
                features = build_features(
                    contact,
                    advanced,
                    current_time,
                    self.config.mission.contact,
                )
                assessment = self.contact_assessor.assess(
                    contact, advanced, features, current_time
                )
            except (TypeError, ValueError):
                assessment = None
            calls = getattr(self.allocator.llm_client.gateway, "call_log", ())
            if assessment is None and call_count < len(calls):
                if any(
                    call.get("role") == "contact_assessor"
                    and not call.get("success", False)
                    for call in calls[call_count:]
                ):
                    self._outcome_evaluator.invalidate("contact_assessor_failed")
            if assessment is None:
                continue
            try:
                sm.contacts.apply_assessment(assessment)
            except (TypeError, ValueError):
                continue
            if assessment.vessel_class == "type_i":
                assessed_contact = sm.contacts.snapshot(assessment.contact_id)
                if assessed_contact.ais_mmsi:
                    ais_state = sm.ais_updates.disable(
                        assessed_contact.ais_mmsi,
                        now_min=current_time,
                    )
                    sm.add_event("ais_value_updates_disabled", {
                        "mmsi": assessed_contact.ais_mmsi,
                        "revision": ais_state.revision,
                        "contact_id": assessment.contact_id,
                    })
            self._apply_probe_assessment(assessment, advanced, current_time)
        self._publish_contact_events(current_time)

    def _finish_probe_session(
        self,
        contact: ContactSnapshot,
        probe: ProbeSession,
        current_time: float,
    ) -> None:
        sm = self.allocator.sm
        task = self.control_coordinator.active_task(probe.uav_id)
        if task is not None and task.probe_id == probe.probe_id:
            self._close_mission_task(
                probe.uav_id,
                task,
                status="blocked",
                reason=probe.completed_reason or "probe_timeout",
                current_time=current_time,
            )
            if self.control_coordinator.has_controller(probe.uav_id):
                self._queue_control_event(
                    "task_failed",
                    probe.uav_id,
                    current_time,
                    {
                        "task_id": task.task_id,
                        "contact_id": probe.contact_id,
                        "probe_id": probe.probe_id,
                        "reason": probe.completed_reason or "probe_timeout",
                    },
                )
        if sm.get_probe_session(probe.probe_id) is not None:
            sm.clear_probe_session(probe.probe_id)
        for vessel_id in self._vessel_ids_for_contact(probe.contact_id):
            self._set_surveillance_fact(
                vessel_id, "probe", False, current_time, probe.probe_id,
            )
        sm.add_event("probe_timed_out", {
            "probe_id": probe.probe_id,
            "contact_id": probe.contact_id,
            "uav_id": probe.uav_id,
            "reason": probe.completed_reason or "probe_timeout",
        })

    def _apply_probe_assessment(
        self,
        assessment,
        probe: ProbeSession,
        current_time: float,
    ) -> None:
        sm = self.allocator.sm
        for vessel_id in self._vessel_ids_for_contact(assessment.contact_id):
            self._set_surveillance_fact(
                vessel_id, "probe", False, current_time, assessment.probe_id,
            )
        task = self.control_coordinator.active_task(probe.uav_id)
        if task is not None and task.probe_id == probe.probe_id:
            self._close_mission_task(
                probe.uav_id,
                task,
                status="completed",
                reason=f"assessment:{assessment.vessel_class}",
                current_time=current_time,
                preserve_contact=assessment.vessel_class == "type_ii",
            )
            if assessment.vessel_class == "type_ii":
                contact = sm.contacts.snapshot(assessment.contact_id)
                if (
                    contact.assigned_uav_id == probe.uav_id
                    and contact.active_probe_id == probe.probe_id
                ):
                    sm.contacts.transition_reservation(
                        assessment.contact_id,
                        probe.uav_id,
                        probe.probe_id,
                        None,
                    )
        sm.add_event("assessment_applied", {
            "assessment_id": assessment.assessment_id,
            "contact_id": assessment.contact_id,
            "probe_id": assessment.probe_id,
            "vessel_class": assessment.vessel_class,
            "history_revision": assessment.history_revision,
        })
        if assessment.vessel_class == "type_ii":
            track_task = ControlTask(
                f"track:{assessment.contact_id}",
                OperationMode.TRACK,
                target_contact_id=assessment.contact_id,
            )
            self._mission_task_records[track_task.task_id] = TaskRecord(
                track_task.task_id,
                "track",
                "approved",
                None,
                assessment.contact_id,
                (),
                probe.uav_id,
                assessment.model_call_id,
                current_time,
                None,
                None,
                None,
            )
            if self.control_coordinator.has_controller(probe.uav_id):
                self._queue_control_event(
                    "type_ii_confirmed",
                    probe.uav_id,
                    current_time,
                    {
                        "contact_id": assessment.contact_id,
                        "probe_id": assessment.probe_id,
                        "vessel_class": "type_ii",
                    },
                )
        elif assessment.vessel_class == "type_i":
            if self.control_coordinator.has_controller(probe.uav_id):
                self._queue_control_event(
                    "type_i_released",
                    probe.uav_id,
                    current_time,
                    {
                        "contact_id": assessment.contact_id,
                        "probe_id": assessment.probe_id,
                        "vessel_class": "type_i",
                    },
                )
        sm.clear_probe_session(probe.probe_id)

    def _process_ais_tracking(
        self,
        uav: UAVEntity,
        target_position: tuple[float, float],
        current_time: float,
    ) -> None:
        """Point EO at an observed estimate; emit fixes only for visible returns.

        Truth is consulted inside the sensor model for bearing/range generation,
        never to find a vessel by a scheduler contact ID or to update its motion.
        """
        if uav.target_group_id is None:
            return
        storms = [item for item in self.obstacles if isinstance(item, Thunderstorm)]
        pointing = math.atan2(target_position[1] - uav.float_position[1],
                              target_position[0] - uav.float_position[0])
        for ship in self.ships:
            if ship.departed:
                continue
            measurement = uav.measure_target(ship.float_position, storms)
            if measurement is None:
                continue
            bearing = uav.heading_rad + measurement.relative_bearing_rad
            offset = math.atan2(math.sin(bearing - pointing), math.cos(bearing - pointing))
            if abs(offset) > math.radians(uav.eo_sensor.fov_deg) / 2:
                continue
            estimate = (
                uav.float_position[0] + measurement.distance_cells * math.cos(bearing),
                uav.float_position[1] + measurement.distance_cells * math.sin(bearing))
            self.allocator.sm.contacts.ingest_visual(
                self._visual_detection(uav, estimate, current_time, "eo"),
                association_contact_id=uav.target_group_id,
            )
            self._publish_information_delta(
                self.allocator.sm.scan_cell(
                    GridCoord(*(int(round(v)) for v in estimate)),
                    current_time,
                    True,
                ),
                current_time,
            )
            contact_id = self.allocator.sm.resolve_contact_id(uav.target_group_id)
            for attempt in self.handoff_manager.attempts():
                if (
                    attempt.state == "pending"
                    and attempt.successor_uav_id == uav.id
                    and self.allocator.sm.resolve_contact_id(attempt.contact_id) == contact_id
                ):
                    try:
                        locked = self.handoff_manager.acquire_eo_lock(
                            attempt.handoff_id,
                            uav.id,
                            current_time,
                        )
                    except (KeyError, ValueError):
                        continue
                    self._outcome_evaluator.record_handoff_lock(
                        locked.handoff_id,
                        at_min=current_time,
                    )
                    self.allocator.sm.add_event("handoff_eo_lock_acquired", {
                        "handoff_id": locked.handoff_id,
                        "contact_id": contact_id,
                        "successor_uav_id": uav.id,
                    })

    def _visual_detection(self, uav, position, current_time, source) -> VisualDetection:
        self._visual_sample_counter = getattr(self, "_visual_sample_counter", 0) + 1
        col, row = (int(round(v)) for v in position)
        mask = self.ship_land_mask
        nearby = mask[max(0, col - 1):col + 2, max(0, row - 1):row + 2]
        return VisualDetection(
            sample_id=f"O{self._visual_sample_counter:07d}", observed_at_min=current_time,
            source=source, source_id=uav.id, position_cells=tuple(position),
            velocity_cells_min=None, position_uncertainty_cells=0.05,
            observer_position_cells=tuple(uav.float_position),
            measured_range_cells=math.dist(uav.float_position, position),
            navigation_context="near_land" if nearby.any() else "open_water")

    def _release_target_group(
        self,
        group_id: str,
        current_time: float,
        event_type: str,
    ) -> None:
        """Release contact bindings and queue the ordinary lifecycle transition."""
        sm = self.allocator.sm
        group_id = sm.resolve_contact_id(group_id)
        self._clear_surveillance_contact(group_id, current_time, event_type)
        sm.clear_target_report(group_id)
        track = sm.get_track_region_for_group(group_id)
        if track is not None:
            sm.release_track_region(track.id, create_marker=event_type == "target_lost")
        for uav in self.uavs:
            task = self.control_coordinator.active_task(uav.id)
            targets = (uav.target_group_id, task.target_contact_id if task else None)
            if not any(target and sm.resolve_contact_id(target) == group_id for target in targets):
                continue
            self._tracking_started_at.pop(uav.id, None)
            self._ais_tracking_started_at.pop(uav.id, None)
            self._ais_measurements.pop(uav.id, None)
            if task is not None:
                self._close_mission_task(
                    uav.id,
                    task,
                    status=(
                        "completed"
                        if event_type == "type_i_released"
                        else "blocked"
                    ),
                    reason=event_type,
                    current_time=current_time,
                )
            uav.target_group_id = None
            sm.clear_uav_assignment(uav.id)
            has_controller = self.control_coordinator.has_controller(uav.id)
            if not has_controller:
                sm.update_uav_status(
                    uav.id, "idle", uav.position,
                    fuel_remaining_pct=uav.fuel_remaining_pct,
                )
            elif self.control_coordinator.current_lease(uav.id).owner is ControlOwner.HEURISTIC:
                self._queue_control_event(
                    event_type,
                    uav.id,
                    current_time,
                    {
                        "group_id": group_id,
                        "contact_id": group_id,
                        "vessel_class": "type_i",
                        "probe_id": None,
                    },
                )
        self.allocator.trigger_manager.notify_event(
            event_type, time=current_time, group_id=group_id,
        )
        sm.add_event(event_type, {"group_id": group_id})

    def _handle_detection(self, uav: UAVEntity, ship: Ship, current_time: float) -> str:
        """SAR sensor adapter: a fix creates evidence, never a control assignment."""
        ship.mark_detected()  # evaluation/legacy visualization only
        detection = self._visual_detection(
            uav, ship.float_position, current_time, "sar",
        )
        cid = self.allocator.sm.contacts.ingest_visual(
            detection,
        )
        # This association is retained only by the evaluation side.  It lets
        # outcome metrics count wrong aliases as wrong instead of correcting
        # them with the physical vessel ID in the blue observation stream.
        self._evaluation_contact_links[cid] = ship.id
        self._set_surveillance_fact(
            ship.id, "sar", True, current_time, detection.sample_id,
        )
        self._publish_contact_events(current_time)
        handoff = self.handoff_manager.latest_for_contact(cid)
        if (
            handoff is not None
            and handoff.state == "pending"
            and handoff.successor_uav_id == uav.id
        ):
            task = self.control_coordinator.active_task(uav.id)
            if task is not None and task.task_id == f"handoff-search:{handoff.handoff_id}":
                # Reacquisition ends this investigation, not whole-area SAR
                # coverage. Retire its remaining swaths without crediting them.
                self._close_mission_task(
                    uav.id, task, status="cancelled", reason="handoff_redetected",
                    current_time=current_time,
                )
                holding = ControlTask(f"holding:{uav.id}:{current_time}", OperationMode.HOLDING)
                self.control_coordinator.promote_to_system_holding(
                    uav.id, current_time=current_time, task_id=holding.task_id,
                )
                self._coordinator_tasks[uav.id] = holding
                uav.start_holding(uav.position)
            # Re-observing an existing contact does not emit contact_created.
            # Wake the scheduler so this SAR investigation can become EO work.
            self.allocator.trigger_manager.notify_event(
                "target_found", time=current_time, uav_id=uav.id, contact_id=cid,
            )
            self.allocator.sm.add_event("handoff_redetected", {
                "handoff_id": handoff.handoff_id, "contact_id": cid,
                "successor_uav_id": uav.id, "sample_id": detection.sample_id,
            })
        return cid

    def _observe_evaluation(self, current_time: float) -> None:
        """Publish an evaluator-only tick after all current-step effects settle."""
        sm = self.allocator.sm
        contacts = tuple(sm.contacts.list_snapshots())
        canonical_links = {
            sm.resolve_contact_id(contact_id): physical_id
            for contact_id, physical_id in self._evaluation_contact_links.items()
        }
        contact_by_id = {contact.contact_id: contact for contact in contacts}
        links = []
        for uav in self.uavs:
            if (
                uav.status != "tracking"
                or uav.sensor_mode != "eo"
                or not uav.target_group_id
            ):
                continue
            contact_id = sm.resolve_contact_id(uav.target_group_id)
            physical_id = canonical_links.get(contact_id)
            contact = contact_by_id.get(contact_id)
            if physical_id is None or contact is None:
                continue
            try:
                physical = next(
                    ship for ship in self.ships if ship.id == physical_id
                )
            except StopIteration:
                continue
            if physical.departed or not uav.eo_sensor.is_target_visible(
                uav.float_position, physical.float_position,
            ):
                continue
            links.append((uav.id, contact_id, physical_id))

        linked_ship_ids = {physical_id for _, _, physical_id in links}
        for ship in self.ships:
            self._set_surveillance_fact(
                ship.id,
                "eo_lock",
                ship.id in linked_ship_ids,
                current_time,
                f"eo-lock:{ship.id}" if ship.id in linked_ship_ids else "eo-lock-lost",
            )

        vessel_samples = tuple(
            VesselTruthSample(
                ship.id,
                ship.vessel_class,
                tuple(ship.float_position),
                bool(ship.departed),
                ship.ais_enabled,
                (
                    "departed" if ship.departed
                    else "survey" if ship.activity_state_at(current_time) == "survey"
                    else getattr(ship, "navigation_status", "ready")
                ),
            )
            for ship in self.ships
        )
        operation_samples = tuple((uav.id, uav.status) for uav in self.uavs)
        status_samples = self._evaluate_intent_statuses(current_time)
        coverage = sm.get_coverage_stats()["coverage_pct"] / 100.0
        self._outcome_evaluator.observe(EvaluationTick(
            sim_time_min=float(current_time),
            dt_min=float(self.clock.dt_min),
            vessels=vessel_samples,
            uav_operations=operation_samples,
            task_records=tuple(self._mission_task_records.values()),
            contacts=contacts,
            valid_eo_links=tuple(links),
            intent_statuses=tuple(status_samples),
            unique_coverage_ratio=float(coverage),
            persistent_coverage=sm.get_persistent_coverage_stats(),
        ))

    def _resume_search(self, uav: UAVEntity) -> bool:
        """Retain the legacy hook without bypassing the global scheduler.

        Older controller tests and integrations patched this hook while
        releasing a track.  Search reassignment is now owned by the mission
        scheduler, so an implicit handoff is deliberately never performed.
        """
        del uav
        return False

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
            # A retired search is not an implicit handoff.  Leave the UAV
            # available only after its current route has been safely ended;
            # the next mission assignment must come from the global scheduler.
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
            self._require_handoff(uav, report, current_time)
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
                        "contact_id": report.contact_id,
                        "position": report.position,
                        "observed_at": report.observed_at,
                    })
            if report is not None:
                sm.release_contact_reservation(uav.target_group_id, uav.id, current_time, "uav_return")
        for region in sm.get_search_regions():
            if region.assigned_uav_id == uav.id:
                record = self._mission_task_records.get(region.id)
                if record is None or record.kind == "search":
                    self._set_search_task_projection(
                        region.id,
                        state="pending",
                        uav_id=None,
                        current_time=current_time,
                        reason="uav_return",
                    )
                else:
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

            if assigned_region is not None:
                self._set_search_task_projection(
                    assigned_region.id,
                    state="completed",
                    uav_id=None,
                    current_time=current_time,
                    reason="search_complete",
                )
                assigned_region.completion_pct = 100.0
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
            self._set_search_task_projection(
                region.id,
                state="stale",
                uav_id=None,
                current_time=current_time,
                reason="lifecycle_rotation",
            )
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
            self._promote_work_controller_to_holding(uav, current_time)
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
        self._expire_contacts(sm.current_time)
        for track in list(sm.get_track_regions()):
            center = self._contact_center(track.target_group_id)
            if center:
                sm.update_track_region_center(
                    track.id, GridCoord(int(round(center[0])), int(round(center[1])))
                )
            else:
                sm.release_track_region(track.id, track.assigned_uav_id, create_marker=True)
                for uav in self.uavs:
                    if uav.target_group_id == track.target_group_id:
                        uav.target_group_id = None
                        sm.clear_uav_assignment(uav.id)
                        if self.control_coordinator.has_controller(uav.id):
                            self._queue_control_event("target_lost", uav.id, sm.current_time,
                                                      {"contact_id": track.target_group_id})
        self._publish_contact_events(sm.current_time)
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
                    task = self.control_coordinator.active_task(entity.id)
                    if task is not None:
                        self._start_coverage_service_task(
                            entity, task, sm.current_time,
                        )
            except Exception as exc:
                self._set_search_task_projection(
                    region.id,
                    state="stale",
                    uav_id=None,
                    current_time=sm.current_time,
                    reason="route_plan_failed",
                )
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
        scan_times = self.allocator.sm.get_last_scan_matrix()
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
            self._set_search_task_projection(
                region.id,
                state="completed",
                uav_id=None,
                current_time=self.allocator.sm.current_time,
                reason="empty_search_route",
            )
            region.completion_pct = 100.0
            self.allocator.sm.clear_uav_assignment(uav.id)
            uav.status = "idle"
            return
        uav.assign_mission(
            region.bbox,
            plan.path,
            transit_end_index=plan.transit_end_index,
            scan_ranges=plan.scan_ranges,
        )

    def _contact_center(self, contact_id: str | None):
        if contact_id is None:
            return None
        return self.allocator.sm.contact_position(contact_id, self.allocator.sm.current_time)

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
            center = self._contact_center(group_id)
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
                "planned_path": self._planned_path_for_conflict(uav),
            }
            for uav in self.uavs
        ]
        conflicts = detect_conflicts(
            uav_dicts,
            cell_size_km=self.config.grid.cell_size_km,
            time_horizon_steps=30,
            min_separation_cells=0.5,
            ignore_common_prefix=True,
        )
        conflict_pairs = {
            tuple(sorted((conflict.uav_a, conflict.uav_b)))
            for conflict in conflicts
        }
        if not conflict_pairs:
            self._active_path_conflicts.clear()
            return
        new_conflicts = [
            conflict for conflict in conflicts
            if tuple(sorted((conflict.uav_a, conflict.uav_b)))
            not in self._active_path_conflicts
        ]
        self._active_path_conflicts = conflict_pairs
        if not new_conflicts:
            return

        entities = {uav.id: uav for uav in self.uavs}
        to_replan = set(resolve_conflicts(new_conflicts, entities))

        # Contact work is already carrying an evidence/lifecycle contract.
        # When it shares a departure corridor with ordinary coverage, make the
        # coverage airframe yield instead of repeatedly resetting a probe.
        for conflict in new_conflicts:
            left = entities.get(conflict.uav_a)
            right = entities.get(conflict.uav_b)
            if left is None or right is None:
                continue
            pair = (left, right)
            coverage = [
                item for item in pair
                if self.control_coordinator.active_task(item.id) is not None
                and self.control_coordinator.active_task(item.id).task_type
                is OperationMode.COVERAGE
            ]
            protected = [
                item for item in pair
                if item not in coverage
                and str(getattr(item, "mission_kind", "")).lower()
                in {"probe", "track_entry"}
            ]
            if coverage and protected:
                to_replan.difference_update(item.id for item in protected)
                to_replan.add(min(coverage, key=lambda item: item.id).id)

        sm = self.allocator.sm
        search_regions = {
            region.id: region for region in sm.get_active_search_regions()
        }
        for uav_id in sorted(to_replan):
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
                for c in new_conflicts
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
                    for c in new_conflicts
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
                center = self._contact_center(uav.target_group_id)
                if center is not None:
                    uav.start_tracking(uav.target_group_id, center)
            else:
                # Returning airframes keep their fuel-safe route; other
                # unsupported mission states are recorded but never mutated
                # with a zero-length waypoint that would not delay motion.
                sm.add_event("conflict_replan_deferred", {
                    "uav_id": uav_id,
                })

    def _planned_path_for_conflict(
        self, uav: UAVEntity,
    ) -> list[tuple[float, float, float]]:
        """Return the live, unconsumed route used by global conflict checks."""
        if self.control_coordinator.has_controller(uav.id):
            snapshot = self.control_coordinator.route_snapshot(uav.id).route
            if snapshot.status != "ready" or not snapshot.route:
                return []
            return [
                uav.pose,
                *snapshot.route[snapshot.next_index:],
            ][:60]
        return [list(pose) for pose in uav.remaining_path[:60]]

    def _record_statuses(self) -> None:
        for uav in self.uavs:
            history = self.status_history[uav.id]
            if history[-1] != uav.status:
                history.append(uav.status)


__all__ = ["SimulationEngine"]
