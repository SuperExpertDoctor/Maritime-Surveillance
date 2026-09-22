"""从 StateManager 构建 WebSocket/JSONL 帧 JSON。"""
import math
from dataclasses import asdict
from collections.abc import Mapping
from functools import lru_cache

from src.control.common.contracts import _immutable_snapshot
from src.schedule.state_manager import StateManager
from src.schedule.config_loader import AppConfig
from src.vis.backend.config_snapshot import configuration_snapshot
from src.vis.backend.public_details import public_frame


def _search_domain(state, config):
    """Publish the actual static denominator, not the decorative chart extent."""
    metrics = getattr(state, "coverage_metrics", None)
    if metrics is None:
        return None
    fixed = metrics.fixed_mask
    cols, rows = fixed.shape
    included, excluded = [], []
    for col in range(cols):
        for row in range(rows):
            (included if fixed[col, row] else excluded).append([col, row])
    return {
        "cols": cols,
        "rows": rows,
        "cell_size_km": config.grid.cell_size_km,
        "searchable_cells": included,
        "excluded_cells": excluded,
        "area_km2": len(included) * config.grid.cell_size_km ** 2,
    }


def sample_route_overview(poses, limit):
    """Sample a complete route while preserving its endpoints."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 2:
        raise ValueError("route overview limit must be at least two")
    poses = tuple(poses)
    if len(poses) <= limit:
        return [list(pose) for pose in poses]
    indices = [
        round(index * (len(poses) - 1) / (limit - 1))
        for index in range(limit)
    ]
    return [list(poses[index]) for index in indices]


def remaining_route(position_pose, poses, next_index, limit):
    """Return the current pose followed by the next unconsumed route points."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("remaining route limit must be positive")
    if isinstance(next_index, bool) or not isinstance(next_index, int):
        raise ValueError("next route index must be an integer")
    poses = tuple(poses)
    if not 0 <= next_index <= len(poses):
        raise ValueError("next route index must be between zero and route length")
    return [
        list(position_pose),
        *[list(pose) for pose in poses[next_index:next_index + limit - 1]],
    ]


def _heading_from_motion(trail, fallback_deg: float) -> float:
    """Prefer the measured direction of travel over a planned heading."""
    points = list(trail or [])
    if len(points) >= 2:
        end = points[-1]
        for start in reversed(points[:-1]):
            dx = float(end[0]) - float(start[0])
            dy = float(end[1]) - float(start[1])
            if math.hypot(dx, dy) > 1e-5:
                return math.degrees(math.atan2(dy, dx)) % 360.0
    return float(fallback_deg) % 360.0


def _transit_progress(entity) -> float | None:
    """Return normalized progress through the departure leg of an active route."""
    if getattr(entity, "status", None) != "transit":
        return None
    waypoints = getattr(entity, "waypoints", ())
    if not waypoints:
        return None
    transit_end = min(int(getattr(entity, "_transit_end_index", 0)), len(waypoints) - 1)
    if transit_end <= 0:
        return None

    total = sum(math.dist(waypoints[index - 1][:2], waypoints[index][:2]) for index in range(1, transit_end + 1))
    if total <= 1e-9:
        return 1.0

    next_index = min(max(1, int(getattr(entity, "_wp_index", 1))), transit_end)
    completed = sum(math.dist(waypoints[index - 1][:2], waypoints[index][:2]) for index in range(1, next_index))
    segment_start = waypoints[next_index - 1][:2]
    segment_end = waypoints[next_index][:2]
    segment_length = math.dist(segment_start, segment_end)
    completed += min(math.dist(segment_start, entity.float_position), segment_length)
    return max(0.0, min(1.0, completed / total))


def _enum_value(value):
    return getattr(value, "value", value)


def _json_snapshot(value):
    """Convert immutable contract payloads into detached JSON containers."""
    value = _immutable_snapshot(value)
    if isinstance(value, Mapping):
        return {key: _json_snapshot(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_snapshot(item) for item in value]
    return value


def _probe_observation_started(state, uav_id: str, route) -> bool | None:
    """Resolve probe observation state from the public session read model."""
    if route.task_type != "probe":
        return None
    sessions = (
        state.get_probe_sessions()
        if hasattr(state, "get_probe_sessions") else ()
    )
    session = next(
        (
            item for item in sessions
            if item.uav_id == uav_id
            and item.contact_id == route.target_contact_id
        ),
        None,
    )
    if session is None:
        return False
    if route.phase == "closing":
        return False
    if route.phase == "baseline":
        return session.baseline_started_at_min is not None
    if route.phase in {"near", "awaiting_assessment", "finished"}:
        return True
    return session.baseline_started_at_min is not None


def _legacy_task_visual(uav, *, source: str, route_status: str) -> dict:
    """Build compatibility metadata when a frame predates route snapshots."""
    operation_mode = _enum_value(getattr(uav, "operation_mode", None)) or "idle"
    return {
        "task_id": None,
        "task_type": operation_mode,
        "phase": _enum_value(getattr(uav, "status", None)) or "idle",
        "contact_id": getattr(uav, "target_group_id", None),
        "generation": int(getattr(uav, "controller_generation", 0)),
        "route_revision": 0,
        "planning_map_version": None,
        "route_status": route_status,
        "route_source": source,
        "observation_started": None,
        "coverage_progress": None,
    }


def _route_visual_data(state, uav, entity, *, planned_limit: int,
                       mission_limit: int) -> tuple[list, list, dict, str | None]:
    """Return bounded route fields from the authoritative or legacy source."""
    snapshot = (
        state.get_control_route(uav.id)
        if hasattr(state, "get_control_route") else None
    )
    expected_episode = getattr(state, "episode_id", "")
    expected_generation = int(getattr(uav, "controller_generation", 0))
    if snapshot is not None:
        if (
            snapshot.episode_id != expected_episode
            or snapshot.generation != expected_generation
        ):
            return (
                [],
                [],
                _legacy_task_visual(
                    uav, source="none", route_status="unavailable",
                ),
                "stale_controller_snapshot",
            )

        route = snapshot.route
        task_visual = {
            "task_id": route.task_id,
            "task_type": route.task_type,
            "phase": route.phase,
            "contact_id": route.target_contact_id,
            "generation": snapshot.generation,
            "route_revision": route.route_revision,
            "planning_map_version": route.planning_map_version,
            "route_status": route.status,
            "route_source": "controller",
            "observation_started": _probe_observation_started(
                state, uav.id, route,
            ),
            "coverage_progress": (
                _json_snapshot(route.coverage_progress)
                if route.coverage_progress is not None
                else None
            ),
        }
        if route.status not in {"ready", "unavailable"}:
            return [], [], task_visual, None
        mission = sample_route_overview(route.route, mission_limit)
        if route.next_index >= len(route.route):
            return [], mission, task_visual, None
        current_pose = (
            [
                float(entity.float_position[0]),
                float(entity.float_position[1]),
                float(entity.heading_rad),
            ]
            if entity is not None
            else [
                float(uav.position.col),
                float(uav.position.row),
                math.radians(float(uav.heading_deg)),
            ]
        )
        return (
            remaining_route(
                current_pose, route.route, route.next_index, planned_limit,
            ),
            mission,
            task_visual,
            None,
        )

    legacy_planned = list(getattr(entity, "planned_path", ()) if entity else ())
    legacy_mission = list(getattr(entity, "mission_route", ()) if entity else ())
    source = "legacy" if legacy_planned or legacy_mission else "none"
    status = "ready" if source == "legacy" else "unavailable"
    return (
        [list(pose) for pose in legacy_planned[-planned_limit:]],
        [list(pose) for pose in legacy_mission[-mission_limit:]],
        _legacy_task_visual(uav, source=source, route_status=status),
        None,
    )


def _sample_snapshot(sample) -> dict:
    """Serialize only an observation key point for the operator read model."""
    return {
        "sample_id": sample.sample_id,
        "observed_at_min": sample.observed_at_min,
        "source": sample.source,
        "source_id": sample.source_id,
        "position": list(sample.position_cells),
        "velocity": list(sample.velocity_cells_min) if sample.velocity_cells_min else None,
        "uncertainty_cells": sample.position_uncertainty_cells,
        "observer_position": (
            list(sample.observer_position_cells)
            if sample.observer_position_cells is not None else None
        ),
        "measured_range_cells": sample.measured_range_cells,
        "navigation_context": sample.navigation_context,
    }


def _assessment_snapshot(assessment) -> dict | None:
    if assessment is None:
        return None
    # model_call_id stays in role-scoped logs; the operator view only needs
    # the conclusion, reasons and the evidence references supporting it.
    return {
        "assessment_id": assessment.assessment_id,
        "contact_id": assessment.contact_id,
        "probe_id": assessment.probe_id,
        "history_revision": assessment.history_revision,
        "assessed_at_min": assessment.assessed_at_min,
        "vessel_class": assessment.vessel_class,
        "confidence": assessment.confidence,
        "evidence_sample_ids": list(assessment.evidence_sample_ids),
        "reasons": list(assessment.reasons),
        "alternative_explanations": list(assessment.alternative_explanations),
    }


def _evidence_snapshot(record) -> dict:
    """Serialize public evidence geometry without exposing environment truth."""
    spatial = record.spatial
    payload = {
        "evidence_id": record.evidence_id,
        "kind": record.kind,
        "source_id": record.source_id,
        "contact_id": record.contact_id,
        "observed_at_min": record.observed_at_min,
        "expires_at_min": record.expires_at_min,
        "strength": record.strength,
    }
    if hasattr(spatial, "origin_cells"):
        payload.update({
            "observer_position": list(spatial.origin_cells),
            "bearing_deg": spatial.bearing_deg,
            "bearing_std_deg": spatial.bearing_std_deg,
        })
    elif hasattr(spatial, "mean_cells"):
        payload["position"] = list(spatial.mean_cells)
    return payload


def _passive_observation_snapshot(observation) -> dict:
    """Expose bearing-only passive facts; environment gates stay server-side."""
    return {
        "observation_id": observation.observation_id,
        "sample_id": observation.sample_id,
        "emitter_track_id": observation.emitter_track_id,
        "burst_id": observation.burst_id,
        "observed_at_min": observation.observed_at_min,
        "observer_uav_id": observation.observer_uav_id,
        "observer_position": list(observation.observer_position_cells),
        "bearing_deg": observation.bearing_deg,
        "bearing_std_deg": observation.bearing_std_deg,
    }


def _passive_position_snapshot(position) -> dict:
    return {
        "position_id": position.position_id,
        "sample_id": position.sample_id,
        "emitter_track_id": position.emitter_track_id,
        "burst_id": position.burst_id,
        "observed_at_min": position.observed_at_min,
        "position": list(position.position_cells),
        "source_observation_ids": list(position.source_observation_ids),
    }


def _contact_snapshot(contact, *, realtime: bool) -> dict:
    sample_limit = 12 if realtime else None
    samples = contact.samples[-sample_limit:] if sample_limit else contact.samples
    return {
        "contact_id": contact.contact_id,
        "revision": contact.revision,
        "state": contact.state,
        "vessel_class": contact.vessel_class,
        "class_confidence": contact.class_confidence,
        "class_evidence_ids": list(contact.class_evidence_ids),
        "activity": contact.activity,
        "activity_confidence": contact.activity_confidence,
        "activity_evidence_ids": list(contact.activity_evidence_ids),
        "ais_mmsi": contact.ais_mmsi,
        "first_seen_min": contact.first_seen_min,
        "last_seen_min": contact.last_seen_min,
        "estimated_position": list(contact.estimated_position_cells),
        "estimated_velocity": (
            list(contact.estimated_velocity_cells_min)
            if contact.estimated_velocity_cells_min is not None else None
        ),
        "uncertainty_cells": contact.uncertainty_cells,
        "assigned_uav_id": contact.assigned_uav_id,
        "active_probe_id": contact.active_probe_id,
        "last_assessment": _assessment_snapshot(contact.last_assessment),
        "cleared_at_min": contact.cleared_at_min,
        "next_probe_not_before_min": contact.next_probe_not_before_min,
        "sample_count": len(contact.samples),
        "latest_ais_sample": next((_sample_snapshot(sample) for sample in reversed(contact.samples) if sample.source == "ais"), None),
        "samples": [_sample_snapshot(sample) for sample in samples],
    }


def build_frame(state: StateManager, cycle: int, config: AppConfig,
                total_steps: int = 480, llm_cycle: dict | None = None,
                model_calls: list | None = None,
                event_history_limit: int = 0,
                ships: list | None = None,
                uav_entities: list | None = None,
                obstacles: list | None = None,
                bases: list | None = None, *, realtime: bool = False,
                include_matrices: bool = True) -> dict:
    """从 StateManager 当前状态构建一帧完整 JSON。

    Args:
        state: 全局状态管理器（单例）
        cycle: 当前 LLM 决策周期编号
        config: 应用配置
        total_steps: 仿真总步数（用于进度显示）
        llm_cycle: LLM 周期信息，None 表示本帧无 LLM 决策
        ships: 船舶实体列表 (wm.ship.Ship)，可选
        uav_entities: UAV 实体列表 (wm.uav_entity.UAVEntity)，可选

    Returns:
        符合设计文档 §7.1 格式的帧 dict
    """
    # UAV 列表
    uavs = []
    entities = {entity.id: entity for entity in (uav_entities or [])}
    for u in state.get_all_uavs():
        entity = entities.get(u.id)
        endurance_h = entity.endurance_h if entity is not None else config.uav.endurance_h
        max_range = config.uav.cruise_speed_kmh * endurance_h
        position = (
            list(entity.float_position) if entity is not None
            else [u.position.col, u.position.row]
        )
        eo_fov = None
        if entity is not None and entity.eo_fov is not None:
            eo_fov = {
                "origin": list(entity.eo_fov.origin),
                "target": list(entity.eo_fov.target),
                "polygon": [list(point) for point in entity.eo_fov.polygon],
                "heading": entity.eo_fov.heading,
                "half_angle": entity.eo_fov.half_angle,
                "max_range": entity.eo_fov.max_range,
            }
        sar_beam = None
        # Preserve replay compatibility for historical frames that only
        # supplied sensor_mode.  Live entities expose sar_imaging and only
        # set sensor_mode to SAR during a stable stripmap acquisition.
        #
        # Also compute the beam during U-turn connectors between scan legs
        # (transit + sar_look_direction still set) so the fan-shaped beam
        # stays visible — the UAV reads as continuously in motion instead
        # of appearing to pause between swaths.
        if entity is not None and entity.sar_look_direction is not None:
            show_beam = (
                entity.sar_imaging
                or entity.sensor_mode == "sar"
                or entity.status in ("transit", "searching")
            )
        else:
            show_beam = entity is not None and (entity.sar_imaging or entity.sensor_mode == "sar")
        if show_beam and entity is not None and entity.sar_look_direction is not None:
            beam = entity.sar_sensor.compute_swath_beam(
                entity.float_position,
                (
                    entity.heading_rad
                    if entity.sar_scan_heading_rad is None
                    else entity.sar_scan_heading_rad
                ),
                entity.sar_look_direction,
                along_track_cells=entity.sar_along_track_cells,
            )
            sar_beam = {
                "origin": list(beam.origin),
                "heading": beam.heading,
                "look_direction": beam.look_direction,
                "near_range": beam.near_range,
                "far_range": beam.far_range,
                "along_track": beam.along_track,
                "polygon": [list(point) for point in beam.polygon],
            }
        # Replay keeps the full sortie.  Live frames trade historical detail
        # for a bounded payload because the client receives frequent updates.
        trail_limit = 120 if realtime else 480
        planned_limit = 100 if realtime else 500
        mission_limit = 200 if realtime else 800
        trail = [list(point) for point in entity.trail[-trail_limit:]] if entity else []
        fallback_heading = entity.heading_deg if entity is not None else u.heading_deg
        planned_path, mission_route, task_visual, route_diagnostic = _route_visual_data(
            state,
            u,
            entity,
            planned_limit=planned_limit,
            mission_limit=mission_limit,
        )
        uav_frame = {
            "id": u.id,
            "status": u.status,
            "operational_status": getattr(u, "operational_status", "available"),
            "failure_reason": getattr(u, "failure_reason", None),
            "position": position,
            "heading_deg": _heading_from_motion(trail, fallback_heading),
            "remaining_range_km": round(u.fuel_remaining_pct * max_range),
            "fuel_remaining_pct": u.fuel_remaining_pct,
            "assigned_region_id": u.assigned_region_id,
            "target_group_id": u.target_group_id,
            "time_to_available_min": u.time_to_available,
            "sensor_mode": entity.sensor_mode if entity is not None else u.sensor_mode,
            "active_mode": getattr(entity, "active_mode", "standby"),
            "transition_remaining_min": getattr(entity, "transition_remaining_min", 0.0),
            "passive_enabled": bool(getattr(entity, "passive_enabled", True)),
            "control_mode": u.control_mode,
            "control_owner": u.control_owner,
            "operation_mode": u.operation_mode,
            "controller_generation": u.controller_generation,
            "safety_intervened": bool(u.safety_intervened),
            "planned_path": planned_path,
            "mission_route": mission_route,
            "task_visual": task_visual,
            "home_base_grid": list(entity.home_base_grid) if entity else [u.position.col, u.position.row],
            "transit_progress": _transit_progress(entity) if entity else None,
            "trail": trail,
            "sar_look_direction": entity.sar_look_direction if entity else None,
            "sar_scan_heading_rad": (
                entity.sar_scan_heading_rad if entity else None
            ),
            "sar_scan_origin": (
                list(entity.sar_scan_origin) if entity and entity.sar_scan_origin else None
            ),
            "sar_scan_position": (
                list(entity.float_position) if entity and entity.sar_imaging else None
            ),
            "sar_actual_heading_rad": (
                float(entity.heading_rad) if entity and entity.sar_imaging else None
            ),
            "sar_footprint": [[cell.col, cell.row] for cell in entity.sar_footprint] if entity else [],
            "sar_beam": sar_beam,
            "sar_imaging": entity.sar_imaging if entity else False,
            "sar_standby": bool(
                entity is not None
                and entity.sar_look_direction is not None
                and not entity.sar_imaging
                and entity.status in ("transit", "searching")
            ),
            "sar_heading_error_deg": entity.sar_heading_error_deg if entity else None,
            "sar_aperture_track": [
                list(position) for position in entity.sar_aperture_track
            ] if entity else [],
            "eo_fov": eo_fov,
            "avoidance_level": entity.avoidance_level if entity else 0,
            "avoidance_path": [list(pose) for pose in entity.avoidance_path] if entity else [],
        }
        if route_diagnostic is not None:
            uav_frame["route_diagnostic"] = route_diagnostic
        uavs.append({
            **uav_frame,
        })

    # 搜索区域
    search_regions = []
    for r in state.get_search_regions():
        search_regions.append({
            "id": r.id,
            "bbox": [r.bbox.col_start, r.bbox.row_start, r.bbox.col_end, r.bbox.row_end],
            "type": r.type,
            "status": r.status,
            "priority": r.priority,
            "info_value": r.info_value,
            "avg_info": r.avg_info,
            "assigned_uav_id": r.assigned_uav_id,
            "completion_pct": r.completion_pct,
            "completion_basis": getattr(r, "completion_basis", "legacy_observation"),
            "created_cycle": r.created_cycle,
            "cells": _task_cells(r),
        })

    # 跟踪区域
    track_regions = []
    for r in state.get_track_regions():
        track_regions.append({
            "id": r.id,
            "bbox": [r.bbox.col_start, r.bbox.row_start, r.bbox.col_end, r.bbox.row_end],
            "type": r.type,
            "status": r.status,
            "priority": r.priority,
            "assigned_uav_id": r.assigned_uav_id,
            "target_group_id": getattr(r, "target_group_id", None),
            "created_cycle": r.created_cycle,
        })

    # 标记点
    markers = []
    for m in state.get_active_markers():
        markers.append({
            "id": m.id,
            "position": [m.position.col, m.position.row],
            "created_time_min": m.created_time,
            "source_uav_id": m.source_uav_id,
        })

    # Production publishers retain bounded history: commands are drained
    # before advancing the clock and can share the previous frame timestamp.
    # A time-exclusive delta would permanently lose those events, and live
    # conflation can skip whole frames. Consumers already deduplicate history.
    recent_events = state.get_recent_events(
        0.0 if event_history_limit else state.current_time - 1.0,
        until_time=state.current_time,
        include_since=bool(event_history_limit),
    )
    if event_history_limit:
        recent_events = recent_events[-event_history_limit:]

    coverage = state.get_coverage_stats()
    published_intents, published_intent_statuses = (
        state.get_published_intent_snapshot()
        if hasattr(state, "get_published_intent_snapshot")
        else ((), ())
    )

    # 船舶列表（从 wm 实体构建）
    ship_list = []
    if ships:
        for s in ships:
            # Contacts exist in the physics engine before discovery, but the
            # UI and decision layer must never receive their hidden state.
            if not getattr(s, "detected", False):
                continue
            trail = [list(point) for point in s.trail[-60:]]
            fallback_heading = math.degrees(getattr(s, "heading_rad", getattr(s, "base_heading", 0.0)))
            ship_list.append({
                "id": s.id,
                "position": list(getattr(s, "float_position", (s.position.col, s.position.row))),
                "group_id": s.group_id or "G0",
                "is_detected": s.detected,
                "ship_type": getattr(getattr(s, "ship_type", None), "value", "destroyer"),
                "heading_deg": _heading_from_motion(trail, fallback_heading),
                "departed": bool(getattr(s, "departed", False)),
                "estimated_position": (
                    list(s.estimated_position)
                    if getattr(s, "estimated_position", None) is not None
                    else None
                ),
                "ais": (
                    getattr(s, "ais_signal", None).to_dict()
                    if getattr(s, "ais_signal", None) is not None
                    else None
                ),
                "trail": trail,  # 最近 60 个轨迹点
            })

    step = int(state.current_time)

    obstacle_list = []
    for obstacle in obstacles or []:
        if hasattr(obstacle, "intensity"):
            obstacle_list.append({
                "id": obstacle.id,
                "type": "thunderstorm",
                "center": list(obstacle.center),
                "size": obstacle.size,
                "move_vector": list(obstacle.move_vector),
                "intensity": obstacle.intensity,
                "lifetime": obstacle.lifetime,
            })
        else:
            obstacle_list.append({
                "id": obstacle.id,
                "type": "island",
                "vertices": [list(point) for point in obstacle.vertices],
                "label": obstacle.label,
            })

    base_list = []
    for index, base in enumerate(bases or []):
        base_list.append({
            "id": base.id,
            "number": index + 1,
            "position": [base.position.col, base.position.row],
            "occupancy": base.occupancy,
            "capacity": base.capacity,
            "busy": base.is_busy,
            "refueling_uav_ids": list(base.hangar),
        })
    if not base_list:
        base_list = [{
            "id": "Base-1", "number": 1,
            "position": list(config.environment.base_position),
            "occupancy": 0, "capacity": config.environment.base_capacity,
            "busy": False, "refueling_uav_ids": [],
        }]

    scenario_vessels = list(
        state.get_vessel_inventory()
        if hasattr(state, "get_vessel_inventory") else ()
    )

    frame = {
        "schema_version": "mission-frame/v2",
        "visual_schema_version": "mission-visual/v1",
        "frame_id": step,
        "cycle": cycle,
        "timestamp": _format_time(state.current_time),
        "sim_time_min": state.current_time,
        "total_steps": total_steps,
        "mode": "live",
        "episode_id": getattr(state, "episode_id", ""),
        "scenario_seed": getattr(state, "scenario_seed", None),
        "reset_generation": getattr(state, "scenario_generation", 0),
        "coverage_metrics": state.get_persistent_coverage_stats(),
        "search_domain": _search_domain(state, config),
        "passive_detection_range_cells": config.sensor.passive.detection_range_cells,
        "task_area": {
            "width_km": config.grid.resolution[0] * config.grid.cell_size_km,
            "height_km": config.grid.resolution[1] * config.grid.cell_size_km,
            "cell_size_km": config.grid.cell_size_km,
        },
        **coverage,
        "uavs": uavs,
        "search_regions": search_regions,
        "track_regions": track_regions,
        "markers": markers,
        "ships": ship_list,
        "contacts": [
            _contact_snapshot(contact, realtime=realtime)
            for contact in (
                state.contacts.list_snapshots()
                if getattr(state, "contacts", None) is not None else ()
            )
        ],
        "events": recent_events,
        "intents": [
            asdict(intent) if hasattr(intent, "__dataclass_fields__") else intent
            for intent in published_intents
        ],
        "intent_statuses": [
            asdict(status) if hasattr(status, "__dataclass_fields__") else status
            for status in published_intent_statuses
        ],
        "intent_events": state.get_intent_events()
        if hasattr(state, "get_intent_events") else [],
        "runtime_status": getattr(state, "runtime_status", "running"),
        "blocked_role": getattr(state, "blocked_role", None),
        "vessel_mutation_allowed": bool(
            getattr(
                state, "vessel_mutation_allowed",
                getattr(state, "editing_allowed", False),
            )
        ),
        "initial_vessel_count": getattr(
            state, "initial_vessel_count", len(scenario_vessels),
        ),
        "actual_vessel_count": getattr(state, "actual_vessel_count", len(ship_list)),
        "information_version": int(getattr(state, "information_version", 0)),
        "evidence": [
            _evidence_snapshot(record)
            for record in (
                state.information_policy.evidence_store.active_records(state.current_time)
                if getattr(state, "information_policy", None) is not None else ()
            )
        ],
        "passive_observations": [
            _passive_observation_snapshot(item)
            for item in (
                state.get_passive_observations()
                if hasattr(state, "get_passive_observations") else ()
            )
        ],
        "passive_positions": [
            _passive_position_snapshot(item)
            for item in (
                state.get_passive_positions()
                if hasattr(state, "get_passive_positions") else ()
            )
        ],
        "handoffs": [
            asdict(item)
            for item in (
                state.handoff_manager.attempts()
                if getattr(state, "handoff_manager", None) is not None else ()
            )
        ],
        "scenario_vessels": scenario_vessels,
        "memory_version": getattr(state, "memory_version", "baseline"),
        "llm_cycle": llm_cycle,
        "model_calls": model_calls or [],
        "config_snapshot": configuration_snapshot(config),
        # Retain V1 fields while appending the richer GOAL2 base model.
        "base_position": base_list[0]["position"],
        "support_base_positions": [base["position"] for base in base_list[1:]],
        "bases": base_list,
        "obstacles": obstacle_list,
    }
    if include_matrices:
        # Live publication includes both matrices on every delivered frame so
        # cell values describe the same simulation instant as the telemetry.
        info_mat = state.get_info_matrix()
        value_mat = state.get_value_matrix()
        frame["info_matrix"] = info_mat.tolist() if hasattr(info_mat, "tolist") else info_mat
        frame["value_matrix"] = value_mat.tolist() if hasattr(value_mat, "tolist") else value_mat
    return public_frame(frame)


def _format_time(minutes: float) -> str:
    """将分钟数转为 HH:MM:SS 字符串。"""
    total_seconds = int(minutes * 60)
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _task_cells(region) -> list[list[int]]:
    """Expose task areas as explicit, slightly irregular grid-cell sets."""
    bbox = region.bbox
    return [list(cell) for cell in _task_cells_cached(
        str(region.id), tuple(bbox),
    )]


@lru_cache(maxsize=2048)
def _task_cells_cached(region_id: str, bbox: tuple[int, int, int, int]) -> tuple[tuple[int, int], ...]:
    """Cache deterministic task geometry while returning immutable cells."""
    sparse_edge_period = 5
    seed = sum(ord(char) for char in region_id)
    cells = []
    for col in range(bbox[0], bbox[2]):
        for row in range(bbox[1], bbox[3]):
            edge_distance = min(
                col - bbox[0],
                bbox[2] - 1 - col,
                row - bbox[1],
                bbox[3] - 1 - row,
            )
            _, sparse_edge_remainder = divmod(
                col * 13 + row * 7 + seed,
                sparse_edge_period,
            )
            if edge_distance == 0 and sparse_edge_remainder == 0:
                continue
            cells.append((col, row))
    return tuple(cells)
