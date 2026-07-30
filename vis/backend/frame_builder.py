"""从 StateManager 构建 WebSocket/JSONL 帧 JSON。"""
from schedule.state_manager import StateManager
from schedule.config_loader import AppConfig


def build_frame(state: StateManager, cycle: int, config: AppConfig,
                total_steps: int = 480, llm_cycle: dict | None = None,
                ships: list | None = None,
                uav_entities: list | None = None) -> dict:
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
    for u in state.get_all_uavs():
        max_range = config.uav.cruise_speed_kmh * config.uav.endurance_h
        uavs.append({
            "id": u.id,
            "status": u.status,
            "position": [u.position.col, u.position.row],
            "heading_deg": 0,   # wm 未实现，暂用默认值
            "remaining_range_km": round(u.fuel_remaining_pct * max_range),
            "assigned_region_id": u.assigned_region_id,
            "target_group_id": u.target_group_id,
            "time_to_available_min": u.time_to_available,
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

    # 船舶列表（从 wm 实体构建）
    ship_list = []
    if ships:
        for s in ships:
            ship_list.append({
                "id": s.id,
                "position": [s.position.col, s.position.row],
                "group_id": s.group_id or "?",
                "is_detected": s.detected,
                "trail": list(s.trail[-60:]),  # 最近 60 个轨迹点
            })

    step = int(state.current_time)

    frame = {
        "frame_id": step,
        "cycle": cycle,
        "timestamp": _format_time(state.current_time),
        "sim_time_min": state.current_time,
        "total_steps": total_steps,
        "mode": "live",
        "info_matrix": info_mat.tolist() if hasattr(info_mat, "tolist") else info_mat,
        "value_matrix": value_mat.tolist() if hasattr(value_mat, "tolist") else value_mat,
        "uavs": uavs,
        "search_regions": search_regions,
        "track_regions": track_regions,
        "markers": markers,
        "ships": ship_list,
        "events": recent_events,
        "llm_cycle": llm_cycle,
        "base_position": list(config.environment.base_position),
    }
    return frame


def _format_time(minutes: float) -> str:
    """将分钟数转为 HH:MM:SS 字符串。"""
    total_seconds = int(minutes * 60)
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"
