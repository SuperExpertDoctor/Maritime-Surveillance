"""从 StateManager 构建 WebSocket/JSONL 帧 JSON。"""
import math

from src.schedule.state_manager import StateManager
from src.schedule.config_loader import AppConfig


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


def build_frame(state: StateManager, cycle: int, config: AppConfig,
                total_steps: int = 480, llm_cycle: dict | None = None,
                ships: list | None = None,
                uav_entities: list | None = None,
                obstacles: list | None = None,
                bases: list | None = None) -> dict:
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
                "max_range": entity.eo_fov.max_range,
            }
        trail = [list(point) for point in entity.trail[-120:]] if entity else []
        fallback_heading = entity.heading_deg if entity is not None else u.heading_deg
        uavs.append({
            "id": u.id,
            "status": u.status,
            "position": position,
            "heading_deg": _heading_from_motion(trail, fallback_heading),
            "remaining_range_km": round(u.fuel_remaining_pct * max_range),
            "fuel_remaining_pct": u.fuel_remaining_pct,
            "assigned_region_id": u.assigned_region_id,
            "target_group_id": u.target_group_id,
            "time_to_available_min": u.time_to_available,
            "sensor_mode": entity.sensor_mode if entity is not None else u.sensor_mode,
            "planned_path": [list(pose) for pose in entity.planned_path[-500:]] if entity else [],
            "trail": trail,
            "sar_look_direction": entity.sar_look_direction if entity else None,
            "sar_footprint": [[cell.col, cell.row] for cell in entity.sar_footprint] if entity else [],
            "eo_fov": eo_fov,
            "avoidance_level": entity.avoidance_level if entity else 0,
            "avoidance_path": [list(pose) for pose in entity.avoidance_path] if entity else [],
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

    # 近期事件（本帧内新事件）
    recent_events = state.get_recent_events(state.current_time - 1.0)

    # 信息矩阵（直接传 numpy 数组的 list 形式）
    info_mat = state.get_info_matrix()
    value_mat = state.get_value_matrix()
    coverage = state.get_coverage_stats()

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
                "is_military": getattr(s, "is_military", None),
                "departed": bool(getattr(s, "departed", False)),
                "is_evasive": bool(getattr(s, "is_evading", False)),
                "radar_range_cells": float(
                    getattr(s, "surface_search_radar_range_cells", 3.0)
                ),
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
                "discrimination": getattr(s, "discrimination", None),
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

    frame = {
        "frame_id": step,
        "cycle": cycle,
        "timestamp": _format_time(state.current_time),
        "sim_time_min": state.current_time,
        "total_steps": total_steps,
        "mode": "live",
        "scenario_seed": getattr(state, "scenario_seed", None),
        "reset_generation": getattr(state, "scenario_generation", 0),
        "info_matrix": info_mat.tolist() if hasattr(info_mat, "tolist") else info_mat,
        "value_matrix": value_mat.tolist() if hasattr(value_mat, "tolist") else value_mat,
        "task_area": {
            "width_km": config.grid.resolution[1] * config.grid.cell_size_km,
            "height_km": config.grid.resolution[0] * config.grid.cell_size_km,
            "cell_size_km": config.grid.cell_size_km,
        },
        **coverage,
        "uavs": uavs,
        "search_regions": search_regions,
        "track_regions": track_regions,
        "markers": markers,
        "ships": ship_list,
        "events": recent_events,
        "llm_cycle": llm_cycle,
        # Retain V1 fields while appending the richer GOAL2 base model.
        "base_position": base_list[0]["position"],
        "support_base_positions": [base["position"] for base in base_list[1:]],
        "bases": base_list,
        "obstacles": obstacle_list,
    }
    return frame


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
    seed = sum(ord(char) for char in region.id)
    cells = []
    for col in range(bbox.col_start, bbox.col_end):
        for row in range(bbox.row_start, bbox.row_end):
            edge_distance = min(
                col - bbox.col_start,
                bbox.col_end - 1 - col,
                row - bbox.row_start,
                bbox.row_end - 1 - row,
            )
            if edge_distance == 0 and (col * 13 + row * 7 + seed) % 5 == 0:
                continue
            cells.append([col, row])
    return cells
