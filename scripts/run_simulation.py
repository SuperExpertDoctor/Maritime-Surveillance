"""UAV 海上侦察动态任务重分配 --- 主仿真入口。"""
import sys
import os
# scripts/ 位于项目根目录，需要将根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.schedule.config_loader import ConfigLoader
from src.schedule.task_allocator import TaskAllocator
from src.env.ship import Ship
from src.env.uav_entity import UAVEntity
from src.env.base_station import BaseStation
from src.env.sim_clock import SimClock
from src.schedule.datatypes import GridCoord, BBox
from src.sensor import SensorSuite
import random
import threading
import asyncio
import uvicorn
from src.vis.backend.server import create_app, broadcast_frame_sync


def _generate_sweep_waypoints(bbox: BBox) -> list[GridCoord]:
    """为 bbox 生成简单的往复扫描航路点。"""
    wpts: list[GridCoord] = []
    rows = range(bbox.row_start, bbox.row_end)
    for i, r in enumerate(rows):
        col_range = range(bbox.col_start, bbox.col_end)
        if i % 2 == 0:
            for c in col_range:
                wpts.append(GridCoord(c, r))
        else:
            for c in reversed(col_range):
                wpts.append(GridCoord(c, r))
    return wpts


def _sync_uav_assignments(uavs: list[UAVEntity], allocator) -> None:
    """将 StateManager 中的 UAV 分配同步到 UAVEntity 物理实体。"""
    sm = allocator.sm
    search_regions = {r.id: r for r in sm.get_search_regions()}
    uav_by_id = {u.id: u for u in uavs}

    for suav in sm.get_all_uavs():
        entity = uav_by_id.get(suav.id)
        if entity is None:
            continue

        # 如果 StateManager 已分配区域但实体仍处于 idle，下达任务
        if (suav.status == "transit"
                and suav.assigned_region_id
                and entity.status == "idle"):
            region = search_regions.get(suav.assigned_region_id)
            if region is None:
                continue
            waypoints = _generate_sweep_waypoints(region.bbox)
            entity.assign_mission(region.bbox, waypoints)

        # 实体已进入搜索/跟踪，但 StateManager 仍在 transit/idle
        if entity.status in ("searching", "tracking") and suav.status in ("idle", "transit"):
            sm.update_uav_status(suav.id, entity.status, entity.position,
                                 assigned_region_id=suav.assigned_region_id,
                                 fuel_remaining_pct=entity.fuel_remaining_pct)

        # 实体已 idle（加油完成），但 StateManager 可能还在 returning/refueling
        if entity.status == "idle" and suav.status in ("returning", "refueling", "transit"):
            sm.update_uav_status(suav.id, "idle", entity.position,
                                 fuel_remaining_pct=1.0)


def main(config_path: str = "configs"):
    config = ConfigLoader.load(config_path)

    # --- 初始化仿真时钟 ---
    clock = SimClock()

    # --- 初始化基地 ---
    base_pos = GridCoord(*config.environment.base_position)
    base = BaseStation(base_pos, config.uav.refuel_time_min)

    # --- 初始化 UAV 实体 ---
    uavs: list[UAVEntity] = []
    for i in range(config.uav.count_max):
        uav = UAVEntity(
            f"UAV-{i + 1}", base_pos,
            config.uav.endurance_h, config.uav.cruise_speed_kmh,
            cell_size_km=config.grid.cell_size_km,
        )
        uavs.append(uav)

    # --- 初始化 Task Allocator ---
    allocator = TaskAllocator(config)

    # --- 初始化传感器套件 ---
    sensor_suite = SensorSuite.from_config(config.sensor)

    # --- 初始化可视化 FastAPI 服务 ---
    app = create_app(config, allocator.sm)
    loop = asyncio.new_event_loop()

    def _run_server():
        asyncio.set_event_loop(loop)
        uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()
    print("可视化服务已启动: http://localhost:8765")

    # --- 初始化目标船舶 ---
    random.seed(42)
    ships: list[Ship] = []
    ship_count = random.randint(config.ship.count_min, config.ship.count_min + 5)
    groups = random.randint(1, min(config.ship.max_groups, ship_count))
    ships_per_group = ship_count // groups

    for g in range(groups):
        group_center = GridCoord(random.randint(5, 24), random.randint(5, 24))
        for s in range(ships_per_group):
            offset_col = random.randint(-2, 2)
            offset_row = random.randint(-2, 2)
            pos = GridCoord(
                max(0, min(29, group_center.col + offset_col)),
                max(0, min(29, group_center.row + offset_row)),
            )
            ship = Ship(
                f"Ship-{g + 1}-{s + 1}", pos,
                config.ship.speed_kn, config.ship.zigzag_amplitude_km,
                config.ship.zigzag_period_min,
            )
            ship.group_id = f"G{g + 1}"
            ships.append(ship)

    # 剩余船舶（不能整除时）
    remaining = ship_count - groups * ships_per_group
    if remaining > 0:
        extra_group = groups - 1 if groups > 0 else 0
        center = GridCoord(random.randint(5, 24), random.randint(5, 24))
        for e in range(remaining):
            pos = GridCoord(
                max(0, min(29, center.col + random.randint(-2, 2))),
                max(0, min(29, center.row + random.randint(-2, 2))),
            )
            ship = Ship(
                f"Ship-{extra_group + 1}-extra-{e + 1}", pos,
                config.ship.speed_kn, config.ship.zigzag_amplitude_km,
                config.ship.zigzag_period_min,
            )
            ship.group_id = f"G{extra_group + 1}"
            ships.append(ship)

    print(f"初始化: {len(uavs)} UAVs, {len(ships)} ships in {groups} groups")
    print(f"基地位置: {base_pos}")

    # ==================================================================
    # 主循环
    # ==================================================================
    max_time_min = 480  # 8 小时仿真
    dt = clock.dt_min

    while clock.time < max_time_min:
        t = clock.tick()

        # 1. 更新船舶位置
        for ship in ships:
            ship.step(dt)

        # 2. 更新 UAV 位置 + 油量
        for uav in uavs:
            fuel_low = uav.step(dt)
            if fuel_low:
                # UAV 自行转入 returning 状态，通知调度层
                allocator.trigger_manager.notify_event(
                    "uav_returned", time=t, uav_id=uav.id,
                    position={"col": uav.position.col, "row": uav.position.row},
                )
                allocator.sm.add_event("uav_returned", {"uav_id": uav.id})
                allocator.sm.update_uav_status(uav.id, "returning", uav.position)

            # 目标检测：基于传感器模型的距离+概率判定
            if uav.status in ("searching", "tracking"):
                detections = sensor_suite.detect(
                    uav.position, ships, config.grid.cell_size_km,
                )
                for ship in detections:
                    ship.mark_detected()
                    allocator.trigger_manager.notify_event(
                        "target_found", time=t,
                        group_id=ship.group_id,
                        position={"col": ship.position.col,
                                  "row": ship.position.row},
                    )
                    allocator.sm.add_event("target_found", {
                        "ship_id": ship.id,
                        "group_id": ship.group_id,
                        "position": ship.position,
                    })
                    print(f"[t={t:.0f}min] {uav.id} 发现 {ship.id} "
                          f"在 {ship.position}")

        # 3. 扫描信息场更新
        for uav in uavs:
            if uav.status == "searching":
                allocator.sm.scan_cell(uav.position, t, is_track=False)
            elif uav.status == "tracking":
                allocator.sm.scan_cell(uav.position, t, is_track=True)

        # 4. 基地加油管理
        #    注意：UAVEntity.step() 到达基地后会自动将状态从
        #    "returning" 转为 "refueling"，因此需要同时检查两种状态。
        for uav in uavs:
            at_base = (uav.position == base_pos)
            if at_base and uav.status in ("returning", "refueling"):
                if not base.is_refueling(uav.id):
                    base.land_uav(uav.id)
                    uav.status = "refueling"
                    uav.fuel_remaining_pct = 0.0

        ready_uavs = base.step(dt)
        for uav_id in ready_uavs:
            for uav in uavs:
                if uav.id == uav_id:
                    uav.fuel_remaining_pct = 1.0
                    uav.status = "idle"
                    uav.position = base_pos
                    allocator.trigger_manager.notify_event(
                        "uav_refueled", time=t, uav_id=uav.id,
                    )
                    break

        # 5. 任务重分配
        result = allocator.step(t)
        if result["trigger_type"] != "none":
            print(f"[t={t:.0f}min] Trigger: {result['trigger_type']} "
                  f"- {result.get('action')}")
            if result["trigger_type"] == "heavy":
                for r in result.get("search_regions", []):
                    print(f"  Region {r['id']}: bbox={r['bbox']}")

        # 5.5. 将调度层分配同步到物理实体
        _sync_uav_assignments(uavs, allocator)

        # 5.6. 广播可视化帧
        app.state.ships = ships
        app.state.uav_entities = uavs
        app.state.current_cycle = allocator.sm.cycle
        app.state.total_steps = int(max_time_min)
        broadcast_frame_sync(app, loop)

        # 6. 整点进度打印
        if int(t) % 60 == 0:
            info_mat = allocator.sm.get_info_matrix()
            coverage = (info_mat > 0.0).sum() / info_mat.size * 100
            tracking = len(allocator.sm.get_track_regions())
            free_uavs = len(allocator.sm.get_available_uavs())
            print(f"[t={t:.0f}min] 覆盖率 {coverage:.1f}% | "
                  f"跟踪 {tracking} 群 | 空闲 UAV {free_uavs}")

    # 仿真结束时广播最后一帧
    app.state.ships = ships
    app.state.uav_entities = uavs
    app.state.current_cycle = allocator.sm.cycle
    app.state.total_steps = int(max_time_min)
    broadcast_frame_sync(app, loop)

    print("仿真结束。")
    print(f"JSONL 日志已保存到: {app.state.frame_logger.path}")
    print("可视化服务将在 30 秒后关闭，或按 Ctrl+C 退出")


if __name__ == "__main__":
    main()
