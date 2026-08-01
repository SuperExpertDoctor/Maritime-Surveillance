"""FastAPI + WebSocket 服务器。

嵌入仿真进程运行，提供:
  - /ws/live      实时帧推送
  - /api/replay/list   可回放文件列表
  - /api/replay?file=  回放文件内容
  - /api/config        只读配置参数
"""
import json
import os
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from src.schedule.state_manager import StateManager
from src.schedule.config_loader import AppConfig
from src.vis.backend.frame_builder import build_frame
from src.vis.backend.frame_logger import FrameLogger

OUTPUT_DIR = "outputs"


def create_app(config: AppConfig, state_manager: StateManager) -> FastAPI:
    """创建 FastAPI 应用实例。

    仿真主循环通过 app.state 访问共享对象：
      - app.state.state_manager
      - app.state.config
      - app.state.frame_logger
      - app.state.current_cycle
      - app.state.total_steps
      - app.state.llm_cycle   (当前 LLM 周期信息, 可选)
      - app.state._live_clients  活跃 WebSocket 连接集合
    """
    app = FastAPI(title="UAV Surveillance Visualizer")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.state.state_manager = state_manager
    app.state.config = config
    app.state.frame_logger = FrameLogger(output_dir=OUTPUT_DIR)
    app.state.current_cycle = 0
    app.state.total_steps = 480
    app.state.llm_cycle = None
    app.state.ships = None        # wm 船舶实体列表
    app.state.uav_entities = None  # wm UAV 实体列表
    app.state.obstacles = None
    app.state.bases = None
    app.state._live_clients = set()

    @app.websocket("/ws/live")
    async def websocket_live(ws: WebSocket):
        await ws.accept()
        app.state._live_clients.add(ws)
        try:
            # A newly connected dashboard must not wait for the next simulation
            # step, especially when a completed run is being held for review.
            await ws.send_json(_build_frame_inner(app))
            while True:
                # 保持连接，由仿真主循环通过 broadcast_frame() 推送
                # 客户端可发送心跳，服务端回复 pong
                data = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                if data == "ping":
                    await ws.send_text("pong")
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            app.state._live_clients.discard(ws)

    @app.get("/api/replay/list")
    async def replay_list():
        """列出 outputs/ 下所有 JSONL 文件。"""
        if not os.path.isdir(OUTPUT_DIR):
            return JSONResponse({"files": []})
        files = sorted(
            [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".jsonl")],
            reverse=True,
        )[:20]
        return JSONResponse({"files": files})

    @app.get("/api/replay")
    async def replay_file(file: str = Query(...)):
        """返回完整 JSONL 文件内容，前端一次加载。

        通过 realpath 校验防止路径遍历攻击。
        """
        allowed_dir = os.path.realpath(OUTPUT_DIR)
        requested_path = os.path.realpath(os.path.join(OUTPUT_DIR, file))
        if not requested_path.startswith(allowed_dir + os.sep):
            return JSONResponse({"error": "invalid file path"}, status_code=400)
        if not os.path.isfile(requested_path):
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(requested_path, media_type="application/x-ndjson")

    @app.get("/api/config")
    async def get_config():
        """返回只读配置参数（分组格式）。"""
        cfg = app.state.config
        return JSONResponse({
            "environment": {
                "sea_area_km": list(cfg.environment.sea_area_km),
                "base_position": list(cfg.environment.base_position),
                "base_count": cfg.environment.base_count,
                "base_capacity": cfg.environment.base_capacity,
                "base_min_distance_cells": cfg.environment.base_min_distance_cells,
                "base_land_margin": cfg.environment.base_land_margin,
                "mainland_width_cells": cfg.environment.mainland_width_cells,
                "base_task_min_distance_cells": cfg.environment.base_task_min_distance_cells,
                "base_obstacle_clearance_cells": cfg.environment.base_obstacle_clearance_cells,
                "island_count_min": cfg.environment.island_count_min,
                "island_count_max": cfg.environment.island_count_max,
                "thunderstorm_count_min": cfg.environment.thunderstorm_count_min,
                "thunderstorm_count_max": cfg.environment.thunderstorm_count_max,
            },
            "grid": {
                "resolution": list(cfg.grid.resolution),
                "cell_size_km": cfg.grid.cell_size_km,
                "decay_half_life_min": cfg.grid.decay_half_life_min,
                "track_decay_half_life_min": cfg.grid.track_decay_half_life_min,
                "white_threshold": cfg.grid.white_threshold,
                "gray_threshold": cfg.grid.gray_threshold,
                "search_min_cells": cfg.grid.search_min_cells,
                "search_max_cells": cfg.grid.search_max_cells,
                "track_min_cells": cfg.grid.track_min_cells,
                "track_max_cells": cfg.grid.track_max_cells,
                "aspect_ratio_max": cfg.grid.aspect_ratio_max,
                "fragment_threshold_cells": cfg.grid.fragment_threshold_cells,
            },
            "uav": {
                "count_max": cfg.uav.count_max,
                "cruise_speed_kmh": cfg.uav.cruise_speed_kmh,
                "endurance_h": cfg.uav.endurance_h,
                "sortie_endurance_h": cfg.uav.sortie_endurance_h,
                "lifecycle_rotation_start_min": cfg.uav.lifecycle_rotation_start_min,
                "lifecycle_coverage_threshold_pct": cfg.uav.lifecycle_coverage_threshold_pct,
                "lifecycle_search_dwell_min": cfg.uav.lifecycle_search_dwell_min,
                "lifecycle_candidate_max_distance_cells": cfg.uav.lifecycle_candidate_max_distance_cells,
                "lifecycle_required_cycles": cfg.uav.lifecycle_required_cycles,
                "refuel_time_min": cfg.uav.refuel_time_min,
            },
            "ship": {
                "count_min": cfg.ship.count_min,
                "max_groups": cfg.ship.max_groups,
                "speed_kn": cfg.ship.speed_kn,
                "zigzag_amplitude_km": cfg.ship.zigzag_amplitude_km,
                "zigzag_period_min": cfg.ship.zigzag_period_min,
                "zigzag_heading_deg": cfg.ship.zigzag_heading_deg,
                "max_turn_rate_deg_min": cfg.ship.max_turn_rate_deg_min,
                "yaw_time_constant_min": cfg.ship.yaw_time_constant_min,
                "heading_control_gain_per_min": cfg.ship.heading_control_gain_per_min,
                "turn_speed_loss_fraction": cfg.ship.turn_speed_loss_fraction,
            },
            "llm": {
                "heavy_cycle_min": cfg.llm.heavy_cycle_min,
                "reviewer_cycle_min": cfg.llm.reviewer_cycle_min,
                "max_retries": cfg.llm.max_retries,
            },
            "common": {
                "clear_outputs_before_run": cfg.common.clear_outputs_before_run,
            },
        })

    return app


def _build_frame_inner(app: FastAPI) -> dict:
    """构建当前帧（同步，可被 async 或 sync 调用方使用）。"""
    state = app.state.state_manager
    cfg = app.state.config
    return build_frame(
        state,
        app.state.current_cycle,
        cfg,
        total_steps=app.state.total_steps,
        llm_cycle=app.state.llm_cycle,
        ships=getattr(app.state, "ships", None),
        uav_entities=getattr(app.state, "uav_entities", None),
        obstacles=getattr(app.state, "obstacles", None),
        bases=getattr(app.state, "bases", None),
    )


async def broadcast_frame(app: FastAPI) -> None:
    """构建当前帧并通过所有活跃 WebSocket 广播。

    仿真主循环每步调用此函数。同时写入 JSONL 日志。
    """
    frame = _build_frame_inner(app)
    # 写入 JSONL
    app.state.frame_logger.write(frame)
    # 广播给所有直播客户端
    clients = getattr(app.state, "_live_clients", set())
    dead = set()
    payload = json.dumps(frame, ensure_ascii=False)
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    app.state._live_clients -= dead


def broadcast_frame_sync(app: FastAPI, loop: asyncio.AbstractEventLoop) -> None:
    """同步版本的广播：从非 async 上下文中安全调用。

    使用 run_coroutine_threadsafe 将 async broadcast_frame
    调度到 uvicorn 事件循环上执行。
    """
    if loop.is_running():
        return asyncio.run_coroutine_threadsafe(broadcast_frame(app), loop)
    return None
