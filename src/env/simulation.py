"""Headless simulation engine shared by the CLI, tests, and web service."""
from __future__ import annotations

import math
import random
from collections import defaultdict

import numpy as np

from src.env.base_station import BaseStation
from src.env.ais_signal import generate_ais_signal
from src.env.obstacle import Island, Thunderstorm, default_obstacles, obstacle_grid_mask
from src.env.sar_sensor import SARSensor
from src.env.ship import Ship, ShipType
from src.env.sim_clock import SimClock
from src.env.uav_entity import UAVEntity
from src.schedule.config_loader import AppConfig
from src.schedule.datatypes import GridCoord
from src.schedule.task_allocator import TaskAllocator
from src.utils.coverage_planner import CoveragePlanner
from src.utils.ais_discriminator import AISDiscriminator
from src.utils.obstacle_avoider import ObstacleAvoider
from src.utils.phase_coordinator import PhaseCoordinator


class SimulationEngine:
    def __init__(self, config: AppConfig, seed: int = 42):
        self.config = config
        self.seed = seed
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
        for uav in self.uavs:
            uav.sar_sensor = SARSensor(
                swath_width_cells=config.sensor.sar.swath_km / config.grid.cell_size_km,
                detection_probability=config.sensor.sar.detection_probability,
                grid_shape=config.grid.resolution,
            )
            uav.eo_sensor.max_range_cells = (
                config.sensor.eoir.detection_range_km / config.grid.cell_size_km
            )
            uav.storm_avoider.eo_detection_range_cells = uav.eo_sensor.max_range_cells
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
            for member in range(size):
                ship_type = (
                    ShipType.AIRCRAFT_CARRIER
                    if group == carrier_group and member == 0
                    else ShipType.DESTROYER
                )
                offset = self._formation_offset(member, size)
                position = GridCoord(
                    int(round(center[0] + offset[0])),
                    int(round(center[1] + offset[1])),
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
                    formation_offset=offset,
                    actual_military=military,
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
        margin = max(0, int(cfg.base_land_margin))
        candidates = [
            (col, row)
            for col in range(cols)
            for row in range(rows)
            if col <= margin or col >= cols - 1 - margin or row <= margin or row >= rows - 1 - margin
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
        for _ in range(200):
            center = (self.rng.uniform(4.0, 25.0), self.rng.uniform(4.0, 25.0))
            if all(not island.contains(center) and island.distance_to_boundary(center) >= 2.0 for island in islands):
                return center
        return 15.0, 15.0

    @staticmethod
    def _formation_offset(member: int, size: int) -> tuple[float, float]:
        if size == 1:
            return 0.0, 0.0
        phase = 2.0 * math.pi * member / size
        return 1.25 * math.cos(phase), 1.25 * math.sin(phase)

    def _inward_heading(self, position: GridCoord) -> float:
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
        self.__init__(self.config, next_seed)
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

        tracking_speeds = self._tracking_speed_commands()
        storms = [item for item in self.obstacles if isinstance(item, Thunderstorm)]
        for uav in self.uavs:
            target = self._group_center(uav.target_group_id) if uav.target_group_id else None
            fuel_low = uav.step(
                self.clock.dt_min,
                target,
                tracking_speed_cells_min=tracking_speeds.get(uav.id),
                storm_zones=storms,
            )
            self._record_storm_avoidance(uav, t)
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
            if (
                fuel_low
                or lifecycle_search_due
                or tracking_due
                or self._needs_reserve_return(uav)
            ):
                self._begin_return(uav, t)

        self._update_sensors_and_detections(t)
        self._process_search_completions(t)
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
        self._record_statuses()
        return result

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
            "markers": len(self.allocator.sm.get_active_markers()),
            "lifecycle_cycles": dict(self.lifecycle_cycles),
            "min_lifecycle_cycles": min(self.lifecycle_cycles.values(), default=0),
            "status_history": dict(self.status_history),
            "scenario_seed": self.seed,
            "reset_generation": self.reset_generation,
        }

    def _update_obstacles(self) -> None:
        previous_mask = self.obstacle_mask
        active = []
        dissipated_storms = []
        for obstacle in self.obstacles:
            if hasattr(obstacle, "step"):
                if obstacle.step(self.clock.dt_min, self.config.grid.resolution):
                    active.append(obstacle)
                elif isinstance(obstacle, Thunderstorm):
                    dissipated_storms.append(obstacle)
            else:
                active.append(obstacle)
        for storm in dissipated_storms:
            self.allocator.sm.add_event("storm_dissipated", {"storm_id": storm.id})
        while sum(isinstance(obstacle, Thunderstorm) for obstacle in active) < self._storm_target_count:
            replacement = self._spawn_thunderstorm(active)
            if replacement is None:
                break
            active.append(replacement)
            self.allocator.sm.add_event("storm_spawned", {"storm_id": replacement.id})
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
            # Carrier groups travel at the carrier's safe formation speed; the
            # members still retain their own declared ship-type performance.
            formation_speed = min(member.speed_cells_per_min for member in members)
            for ship in members:
                original = ship.speed_cells_per_min
                ship.speed_cells_per_min = formation_speed
                ship.step(self.clock.dt_min, islands)
                ship.speed_cells_per_min = original
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
            tracker.cancel_tracking()
            sm.clear_uav_assignment(tracker.id)
            sm.update_uav_status(
                tracker.id, "idle", tracker.position,
                fuel_remaining_pct=tracker.fuel_remaining_pct,
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
            if uav.status == "searching":
                footprint = uav.sar_sensor.compute_swath_footprint(
                    uav.float_position,
                    uav.heading_rad,
                    uav.sar_look_direction,
                    along_track_cells=5.0,
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
        started = self._ais_tracking_started_at.setdefault(uav.id, current_time)
        if current_time - started < self.config.ship.ais_discrimination_delay_min:
            return
        estimate_median = tuple(float(np.median([point[index] for point in samples])) for index in (0, 1))
        result = self.ais_discriminator.discriminate(
            members[0].ais_signal,
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
            uav.cancel_tracking()
            sm.clear_uav_assignment(uav.id)
            if not self._resume_search(uav):
                sm.update_uav_status(
                    uav.id, "idle", uav.position,
                    fuel_remaining_pct=uav.fuel_remaining_pct,
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
        for member in self.ships:
            if member.group_id != ship.group_id or member.detected:
                continue
            member.mark_detected()
            sm.add_event("ship_detected", {
                "ship_id": member.id,
                "group_id": member.group_id,
                "uav_id": uav.id,
                "position": member.position,
            })
        existing = sm.get_track_region_for_group(ship.group_id)
        if existing is not None:
            return

        for region in sm.get_search_regions():
            if region.assigned_uav_id == uav.id:
                region.assigned_uav_id = None
        track = sm.create_track_region(ship.group_id, ship.position)
        track.assigned_uav_id = uav.id
        self.track_creations += 1
        self._tracking_started_at[uav.id] = current_time
        uav.start_tracking(ship.group_id, ship.float_position)
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
        sm.add_event("target_found", {
            "uav_id": uav.id,
            "group_id": ship.group_id,
            "position": ship.position,
        })

    def _begin_return(
        self,
        uav: UAVEntity,
        current_time: float,
        *,
        release_marker: bool = True,
    ) -> None:
        sm = self.allocator.sm
        self._search_started_at.pop(uav.id, None)
        self._tracking_started_at.pop(uav.id, None)
        self._ais_tracking_started_at.pop(uav.id, None)
        self._ais_measurements.pop(uav.id, None)
        if uav.target_group_id:
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
        accepting = [base for base in self.bases if base.can_accept()]
        bases = sorted(
            accepting or self.bases,
            key=lambda base: (
                # Keep recovery work balanced across the independently
                # capacity-limited coastal bases.  Equal-work bases still
                # use geometric distance as their final route tiebreaker.
                self._recovery_load(base),
                base.occupancy,
                math.dist(
                    uav.float_position,
                    (base.position.col, base.position.row),
                ),
            ),
        )
        center = (
            (self.config.grid.resolution[0] - 1) / 2,
            (self.config.grid.resolution[1] - 1) / 2,
        )
        local_mask = self.obstacle_mask.copy()
        col, row = uav.position
        local_mask[max(0, col - 1):col + 2, max(0, row - 1):row + 2] = False
        errors = []
        best_path: tuple[int, int, float, BaseStation, list] | None = None
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
                max_range_cells = (
                    uav.cruise_speed_kmh * uav.endurance_h / uav.cell_size_km
                )
                if length >= uav.fuel_remaining_pct * max_range_cells * 0.98:
                    errors.append(f"{base.id}: insufficient fuel for {length:.2f} cells")
                    break
                candidate_key = (self._recovery_load(base), base.occupancy, length)
                if best_path is None or candidate_key < best_path[:3]:
                    best_path = (*candidate_key, base, path)
                break
        if best_path is not None:
            _, _, _, base, path = best_path
            self._return_base_by_uav[uav.id] = base
            uav.plan_return(path)
            return
        raise RuntimeError(
            f"no land recovery base has a safe return path for {uav.id}: "
            + "; ".join(errors)
        )

    def _recovery_load(self, base: BaseStation) -> int:
        """Completed and already-reserved recovery work for one base."""
        scheduled = sum(
            assigned is base for assigned in self._return_base_by_uav.values()
        )
        return base.refuel_count + scheduled

    def _process_search_completions(self, current_time: float) -> None:
        sm = self.allocator.sm
        for uav in self.uavs:
            if not uav.search_complete_pending:
                continue
            region_id = sm.get_uav(uav.id).assigned_region_id if sm.get_uav(uav.id) else None
            for region in sm.get_search_regions():
                if region.id == region_id:
                    region.status = "completed"
                    region.completion_pct = 100.0
                    region.assigned_uav_id = None
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
        max_range_cells = (
            uav.cruise_speed_kmh * uav.endurance_h / uav.cell_size_km
        )
        remaining_cells = uav.fuel_remaining_pct * max_range_cells
        base = self._nearest_base(uav.float_position)
        direct_home = math.dist(
            uav.float_position,
            (base.position.col, base.position.row),
        )
        reserve_cells = direct_home * 1.25 + 3.0
        return remaining_cells <= reserve_cells

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
        for base in self.bases:
            for uav_id in base.step(self.clock.dt_min):
                uav = next(item for item in self.uavs if item.id == uav_id)
                uav.position = base.position
                uav.base_position = base.position
                uav.refuel()
                self._return_base_by_uav.pop(uav.id, None)
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
        for track in sm.get_track_regions():
            center = self._group_center(track.target_group_id)
            if center:
                sm.update_track_region_center(
                    track.id, GridCoord(int(round(center[0])), int(round(center[1])))
                )

    def _sync_assignments(self) -> None:
        sm = self.allocator.sm
        region_by_id = {region.id: region for region in sm.get_search_regions()}
        entity_by_id = {uav.id: uav for uav in self.uavs}
        for state in sm.get_all_uavs():
            entity = entity_by_id[state.id]
            if state.status != "transit" or not state.assigned_region_id or entity.status != "idle":
                continue
            region = region_by_id.get(state.assigned_region_id)
            if region is None:
                continue
            try:
                self._assign_search_route(entity, region)
            except (RuntimeError, ValueError) as exc:
                region.status = "stale"
                region.assigned_uav_id = None
                sm.clear_uav_assignment(entity.id)
                entity.status = "idle"
                sm.add_event("route_plan_failed", {
                    "uav_id": entity.id,
                    "region_id": region.id,
                    "error": str(exc),
                })

    def _assign_search_route(self, uav: UAVEntity, region) -> None:
        swath_width = self.config.sensor.sar.swath_km / self.config.grid.cell_size_km
        coverage = self.coverage_planner.plan(
            region.bbox, uav.pose, swath_width, uav.R_min
        )
        scan_times = self.allocator.sm.info_field.last_scan_time
        swaths = [
            swath
            for swath in coverage.swaths
            if any(
                not math.isfinite(scan_times[cell.col, cell.row])
                for cell in swath.footprint
            )
        ]
        if not swaths:
            region.status = "completed"
            region.completion_pct = 100.0
            region.assigned_uav_id = None
            self.allocator.sm.clear_uav_assignment(uav.id)
            uav.status = "idle"
            return
        full_path = [uav.pose]
        scan_ranges = []
        transit_end_index = 0
        for index, swath in enumerate(swaths):
            entry = (swath.start[0], swath.start[1], swath.heading)
            try:
                connector = self.obstacle_avoider.plan_path(
                    full_path[-1], entry, self.obstacle_mask, uav.R_min
                )
            except RuntimeError:
                connector = ObstacleAvoider(
                    max_iterations=2400,
                    seed=31 + index * 101,
                ).plan_path(
                    full_path[-1], entry, self.obstacle_mask, uav.R_min
                )
            full_path.extend(connector[1:])
            if index == 0:
                transit_end_index = len(full_path) - 1
            line = self.coverage_planner.sample_scan_line(swath)
            if not self.obstacle_avoider.is_path_safe(line, self.obstacle_mask):
                raise RuntimeError("SAR scan line intersects a no-fly obstacle")
            scan_start = len(full_path) - 1
            full_path.extend(line[1:])
            scan_ranges.append((scan_start, len(full_path) - 1, swath.look_direction))
        uav.assign_mission(
            region.bbox,
            full_path,
            transit_end_index=transit_end_index,
            scan_ranges=scan_ranges,
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

    def _record_statuses(self) -> None:
        for uav in self.uavs:
            history = self.status_history[uav.id]
            if history[-1] != uav.status:
                history.append(uav.status)


__all__ = ["SimulationEngine"]
