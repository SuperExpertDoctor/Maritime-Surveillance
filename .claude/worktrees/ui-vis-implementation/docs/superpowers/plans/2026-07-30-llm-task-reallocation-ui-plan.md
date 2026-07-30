# LLM 动态任务重分配 — Web 可视化 UI 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 LLM 动态任务重分配系统的 Web 可视化界面——双层融合热力网格地图 + UAV/区域/船舶实时态势 + 直播/回放双模式。

**Architecture:** React 18 + Vite 前端通过 Canvas 2D 渲染 30×30 网格地图，FastAPI + WebSocket 后端嵌入 Python 仿真进程推送帧数据，JSONL 文件支持离线回放。

**Tech Stack:** React 18, Vite, Canvas 2D, FastAPI, WebSocket, JSON Lines

## Global Constraints

- 可视化代码放在 `vis/` 目录
- 可视化输出（JSONL 日志等）放在 `outputs/` 目录
- Canvas 渲染使用 9 层管线，按 §3.2 定义顺序
- 帧数据格式严格遵循设计文档 §7.1
- 直播模式前端为纯观察者，不控制仿真节奏
- 回放模式支持播放/暂停/调速/拖拽
- 油量以剩余里程 km 显示（满续航 4,800km）
- Hungarian 配对连线闪 3 秒后消失
- 底部抽屉默认 35% 高度，可拖拽调整
- 侧边栏固定 300px 宽

---

## 文件结构

```
vis/
├── backend/
│   ├── __init__.py
│   ├── frame_builder.py   # 从 StateManager 构建帧 JSON
│   ├── frame_logger.py    # JSONL 帧日志写入
│   ├── server.py          # FastAPI + WebSocket
│   └── requirements.txt   # fastapi, uvicorn[standard]
│
├── frontend/
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── main.jsx
│       ├── App.jsx
│       ├── App.css
│       ├── components/
│       │   ├── CanvasMap.jsx        # Canvas 地图 React 包装
│       │   ├── RightSidebar.jsx     # 侧边栏容器
│       │   ├── SimStatus.jsx        # 仿真时间 + LLM 周期
│       │   ├── UavList.jsx          # UAV 卡片列表
│       │   ├── UavCard.jsx          # 单架 UAV 卡片
│       │   ├── LlmSummary.jsx       # LLM 决策摘要
│       │   ├── BottomDrawer.jsx     # 底部抽屉容器
│       │   ├── EventTimeline.jsx    # Tab 1: 事件时间线
│       │   ├── RegionTable.jsx      # Tab 2: 区域详情表
│       │   ├── LlmLog.jsx           # Tab 3: LLM 日志
│       │   ├── ParamView.jsx        # Tab 4: 参数配置
│       │   └── PlaybackBar.jsx      # 回放控制栏
│       ├── hooks/
│       │   ├── useWebSocket.js      # WebSocket 连接管理
│       │   └── useReplay.js         # JSONL 加载 + 回放状态
│       └── renderer/
│           ├── geometry.js          # 网格↔像素坐标映射
│           ├── colors.js            # 色值常量 + HSL 映射
│           └── layers.js            # 9 层渲染函数
│
└── outputs/                         # 运行时创建（.gitkeep）
    └── .gitkeep
```

## Interfaces Between Tasks

| 接口 | 定义位置 | 消费位置 | 签名 |
|------|---------|---------|------|
| `build_frame(state, cycle, config)` | `frame_builder.py` | `server.py` | `(StateManager, int, AppConfig) -> dict` — 返回 §7.1 格式的帧 JSON |
| `FrameLogger.write(frame)` | `frame_logger.py` | `server.py` | `(dict) -> None` — 追加一行 JSONL |
| `coordToPixel(col, row, cellSize, offsetX, offsetY)` | `geometry.js` | `layers.js`, `CanvasMap.jsx` | `(int, int, number, number, number) -> {x, y}` |
| `infoValueToHSL(I, V)` | `colors.js` | `layers.js` | `(number, number) -> {h, s, l}` |
| `renderFrame(ctx, frame, cellSize, offsetX, offsetY, opts)` | `layers.js` | `CanvasMap.jsx` | 主渲染入口，依次调用 9 个 layer 函数 |
| `useWebSocket(url)` | `useWebSocket.js` | `App.jsx` | `(string) -> {frame, connected, error}` |
| `useReplay(frames)` | `useReplay.js` | `App.jsx` | `(dict[]) -> {currentFrame, isPlaying, speed, seek, play, pause, ...}` |

---

### Task 1: 项目脚手架

**Files:**
- Create: `vis/backend/__init__.py`
- Create: `vis/backend/requirements.txt`
- Create: `vis/frontend/package.json`
- Create: `vis/frontend/vite.config.js`
- Create: `vis/frontend/index.html`
- Create: `vis/outputs/.gitkeep`

**Interfaces:**
- Produces: 目录结构就绪，前端可 `npm install && npm run dev`，后端可 `pip install -r requirements.txt`

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p vis/backend vis/frontend/src/components vis/frontend/src/hooks vis/frontend/src/renderer vis/outputs
```

- [ ] **Step 2: 创建 `vis/backend/requirements.txt`**

```
fastapi>=0.100.0
uvicorn[standard]>=0.23.0
```

- [ ] **Step 3: 创建 `vis/backend/__init__.py`**

```python
# vis/backend - FastAPI server for simulation visualization
```

- [ ] **Step 4: 创建 `vis/frontend/package.json`**

```json
{
  "name": "uav-surveillance-vis",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "lucide-react": "^0.400.0"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.1",
    "vite": "^5.4.0"
  }
}
```

- [ ] **Step 5: 创建 `vis/frontend/vite.config.js`**

```js
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/ws": {
        target: "ws://localhost:8765",
        ws: true,
      },
      "/api": {
        target: "http://localhost:8765",
      },
    },
  },
});
```

- [ ] **Step 6: 创建 `vis/frontend/index.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>UAV 侦察态势监控</title>
    <style>
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body, #root { width: 100%; height: 100%; overflow: hidden; }
      body { font-family: "PingFang SC", "Microsoft YaHei", "Helvetica Neue", sans-serif; background: #0D1117; color: #e6edf3; }
    </style>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

- [ ] **Step 7: 创建 `vis/outputs/.gitkeep`**

```bash
echo "" > vis/outputs/.gitkeep
```

- [ ] **Step 8: 验证**

```bash
cd vis/frontend && npm install && cd ../..
cd vis/backend && pip install -r requirements.txt
```

- [ ] **Step 9: Commit**

```bash
git add vis/
git commit -m "feat: scaffold vis/ project structure (frontend + backend)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Python 后端 — 帧构建 + 日志 + 服务器

**Files:**
- Create: `vis/backend/frame_builder.py`
- Create: `vis/backend/frame_logger.py`
- Create: `vis/backend/server.py`

**Interfaces:**
- Produces: `build_frame(state, cycle, config) -> dict` — 帧数据构建
- Produces: `FrameLogger` 类 — JSONL 写入
- Produces: `create_app(config, state_manager) -> FastAPI` — 服务器工厂函数

- [ ] **Step 1: 创建 `vis/backend/frame_builder.py`**

```python
"""从 StateManager 构建 WebSocket/JSONL 帧 JSON。"""
from schedule.state_manager import StateManager
from schedule.config_loader import AppConfig


def build_frame(state: StateManager, cycle: int, config: AppConfig,
                total_steps: int = 480, llm_cycle: dict | None = None) -> dict:
    """从 StateManager 当前状态构建一帧完整 JSON。

    Args:
        state: 全局状态管理器（单例）
        cycle: 当前 LLM 决策周期编号
        config: 应用配置
        total_steps: 仿真总步数（用于进度显示）
        llm_cycle: LLM 周期信息，None 表示本帧无 LLM 决策

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

    step = int(state.current_time)

    frame = {
        "frame_id": step,
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
        "ships": [],       # wm 未实现，预留
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
```

- [ ] **Step 2: 创建 `vis/backend/frame_logger.py`**

```python
"""JSONL 帧日志——仿真每步写入一行完整帧 JSON。"""
import json
import os
from datetime import datetime


class FrameLogger:
    """追加式 JSONL 日志写入器。"""

    def __init__(self, output_dir: str = "outputs"):
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._path = os.path.join(output_dir, f"simulation_{timestamp}.jsonl")
        self._count: int = 0

    @property
    def path(self) -> str:
        return self._path

    @property
    def count(self) -> int:
        return self._count

    def write(self, frame: dict) -> None:
        """追加一帧到 JSONL 文件。"""
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self._count += 1
```

- [ ] **Step 3: 创建 `vis/backend/server.py`**

```python
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
from schedule.state_manager import StateManager
from schedule.config_loader import AppConfig
from vis.backend.frame_builder import build_frame
from vis.backend.frame_logger import FrameLogger


def create_app(config: AppConfig, state_manager: StateManager) -> FastAPI:
    """创建 FastAPI 应用实例。

    仿真主循环通过 app.state 访问共享对象：
      - app.state.state_manager
      - app.state.config
      - app.state.frame_logger
      - app.state.current_cycle
      - app.state.total_steps
      - app.state.llm_cycle   (当前 LLM 周期信息, 可选)
    """
    app = FastAPI(title="UAV Surveillance Visualizer")

    app.state.state_manager = state_manager
    app.state.config = config
    app.state.frame_logger = FrameLogger()
    app.state.current_cycle = 0
    app.state.total_steps = 480
    app.state.llm_cycle = None

    @app.websocket("/ws/live")
    async def websocket_live(ws: WebSocket):
        await ws.accept()
        # 注册到活跃连接集合
        if not hasattr(app.state, "_live_clients"):
            app.state._live_clients = set()
        app.state._live_clients.add(ws)
        try:
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
        output_dir = "outputs"
        if not os.path.isdir(output_dir):
            return JSONResponse({"files": []})
        files = sorted(
            [f for f in os.listdir(output_dir) if f.endswith(".jsonl")],
            reverse=True,
        )[:20]
        return JSONResponse({"files": files})

    @app.get("/api/replay")
    async def replay_file(file: str = Query(...)):
        """返回完整 JSONL 文件内容，前端一次加载。"""
        path = os.path.join("outputs", file)
        if not os.path.isfile(path):
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(path, media_type="application/x-ndjson")

    @app.get("/api/config")
    async def get_config():
        """返回只读配置参数（分组格式）。"""
        cfg = app.state.config
        return JSONResponse({
            "environment": {
                "sea_area_km": list(cfg.environment.sea_area_km),
                "base_position": list(cfg.environment.base_position),
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
                "refuel_time_min": cfg.uav.refuel_time_min,
            },
            "ship": {
                "count_min": cfg.ship.count_min,
                "max_groups": cfg.ship.max_groups,
                "speed_kn": cfg.ship.speed_kn,
            },
            "llm": {
                "heavy_cycle_min": cfg.llm.heavy_cycle_min,
                "reviewer_cycle_min": cfg.llm.reviewer_cycle_min,
                "max_retries": cfg.llm.max_retries,
            },
        })

    return app


async def broadcast_frame(app: FastAPI) -> None:
    """构建当前帧并通过所有活跃 WebSocket 广播。

    仿真主循环每步调用此函数。同时写入 JSONL 日志。
    """
    state = app.state.state_manager
    cfg = app.state.config
    frame = build_frame(
        state,
        app.state.current_cycle,
        cfg,
        total_steps=app.state.total_steps,
        llm_cycle=app.state.llm_cycle,
    )
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
```

- [ ] **Step 4: 验证（服务可启动）**

创建一个 `vis/backend/test_server_startup.py` 快速验证：

```python
"""验证 FastAPI 服务可以创建并启动。"""
import uvicorn
from schedule.config_loader import ConfigLoader
from schedule.state_manager import StateManager
from vis.backend.server import create_app

config = ConfigLoader.load("configs")
state = StateManager(config)
app = create_app(config, state)
# 不实际启动，只验证无导入/构造错误
print("OK: app created")
# 如需手动测试: uvicorn.run(app, host="0.0.0.0", port=8765)
```

运行：`cd vis && python -m backend.test_server_startup`
预期输出：`OK: app created`

- [ ] **Step 5: Commit**

```bash
git add vis/backend/
git commit -m "feat: add FastAPI server with WebSocket, frame builder, and JSONL logger

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 前端脚手架 + 布局框架

**Files:**
- Create: `vis/frontend/src/main.jsx`
- Create: `vis/frontend/src/App.jsx`
- Create: `vis/frontend/src/App.css`
- Create: `vis/frontend/src/components/CanvasMap.jsx` (骨架)
- Create: `vis/frontend/src/components/RightSidebar.jsx` (骨架)
- Create: `vis/frontend/src/components/BottomDrawer.jsx` (骨架)
- Create: `vis/frontend/src/components/PlaybackBar.jsx` (骨架)

**Interfaces:**
- Produces: 可运行的布局骨架——顶部栏、左侧地图占位、右侧边栏占位、底部抽屉占位
- Consumes: 无

- [ ] **Step 1: 创建 `vis/frontend/src/main.jsx`**

```jsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./App.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

- [ ] **Step 2: 创建 `vis/frontend/src/App.css`**

```css
:root {
  --bg-primary: #0D1117;
  --bg-secondary: #161B22;
  --bg-tertiary: #21262D;
  --border: #30363D;
  --text-primary: #E6EDF3;
  --text-secondary: #8B949E;
  --text-muted: #6E7681;
  --sidebar-width: 300px;
  --playback-height: 48px;
  --color-green: #22C55E;
  --color-red: #EF4444;
  --color-orange: #F97316;
  --color-blue: #3B82F6;
  --color-gray: #9CA3AF;
  --color-yellow: #FBBF24;
}

.app-layout {
  display: grid;
  width: 100vw;
  height: 100vh;
  grid-template-columns: 1fr var(--sidebar-width);
  grid-template-rows: 1fr auto;
  overflow: hidden;
}

.app-layout.replay-active {
  grid-template-rows: 1fr auto var(--playback-height);
}

.canvas-area {
  position: relative;
  overflow: hidden;
  background: var(--bg-primary);
  grid-row: 1;
  grid-column: 1;
}

.canvas-area canvas {
  display: block;
  width: 100%;
  height: 100%;
}

.sidebar {
  grid-row: 1 / -1;
  grid-column: 2;
  background: var(--bg-secondary);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: hidden;
}

.bottom-drawer {
  grid-row: 2;
  grid-column: 1;
  background: var(--bg-secondary);
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}

.bottom-drawer .drawer-tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border);
  padding: 0 8px;
}

.bottom-drawer .drawer-tab {
  padding: 8px 16px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
  user-select: none;
}

.bottom-drawer .drawer-tab:hover {
  color: var(--text-primary);
}

.bottom-drawer .drawer-tab.active {
  color: var(--text-primary);
  border-bottom-color: var(--color-blue);
}

.bottom-drawer .drawer-content {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
}

.drawer-resize-handle {
  height: 4px;
  cursor: ns-resize;
  background: transparent;
  transition: background 0.15s;
}

.drawer-resize-handle:hover {
  background: var(--border);
}

.playback-bar {
  grid-row: 3;
  grid-column: 1;
  height: var(--playback-height);
  background: var(--bg-tertiary);
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 12px;
  gap: 8px;
}
```

- [ ] **Step 3: 创建骨架组件**

`vis/frontend/src/components/CanvasMap.jsx` (骨架):

```jsx
import { useRef, useEffect } from "react";

export default function CanvasMap({ frame, selectedUavId, onSelectUav }) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);

  return (
    <div className="canvas-area" ref={containerRef}>
      <canvas ref={canvasRef} />
    </div>
  );
}
```

`vis/frontend/src/components/RightSidebar.jsx` (骨架):

```jsx
export default function RightSidebar({ frame }) {
  return (
    <div className="sidebar">
      <div style={{ padding: 16 }}>侧边栏占位</div>
    </div>
  );
}
```

`vis/frontend/src/components/BottomDrawer.jsx` (骨架):

```jsx
import { useState } from "react";

const TABS = ["时间线", "区域详情", "LLM日志", "参数"];

export default function BottomDrawer({ frame, visible }) {
  const [activeTab, setActiveTab] = useState(0);

  if (!visible) return null;

  return (
    <div className="bottom-drawer" style={{ height: "35vh" }}>
      <div className="drawer-tabs">
        {TABS.map((t, i) => (
          <div
            key={t}
            className={`drawer-tab ${i === activeTab ? "active" : ""}`}
            onClick={() => setActiveTab(i)}
          >
            {t}
          </div>
        ))}
      </div>
      <div className="drawer-content">
        {TABS[activeTab]} 占位内容
      </div>
    </div>
  );
}
```

`vis/frontend/src/components/PlaybackBar.jsx` (骨架):

```jsx
export default function PlaybackBar({ visible }) {
  if (!visible) return null;
  return (
    <div className="playback-bar">
      <span style={{ color: "var(--text-secondary)", fontSize: 13 }}>
        回放控制占位
      </span>
    </div>
  );
}
```

- [ ] **Step 4: 创建 `vis/frontend/src/App.jsx`**

```jsx
import { useState } from "react";
import CanvasMap from "./components/CanvasMap";
import RightSidebar from "./components/RightSidebar";
import BottomDrawer from "./components/BottomDrawer";
import PlaybackBar from "./components/PlaybackBar";

export default function App() {
  const [mode, setMode] = useState("live");   // "live" | "replay"
  const [frame, setFrame] = useState(null);
  const [selectedUavId, setSelectedUavId] = useState(null);
  const [drawerVisible, setDrawerVisible] = useState(false);

  return (
    <div className={`app-layout ${mode === "replay" ? "replay-active" : ""}`}>
      <CanvasMap
        frame={frame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
      />
      <RightSidebar frame={frame} />
      <BottomDrawer frame={frame} visible={drawerVisible} />
      <PlaybackBar visible={mode === "replay"} />
    </div>
  );
}
```

- [ ] **Step 5: 验证 — 启动前端查看骨架布局**

```bash
cd vis/frontend && npm run dev
```

浏览器打开 `http://localhost:5173`，确认：深色背景、左右分栏布局渲染正常、无控制台错误。

- [ ] **Step 6: Commit**

```bash
git add vis/frontend/src/ vis/frontend/index.html vis/frontend/vite.config.js vis/frontend/package.json
git commit -m "feat: add frontend scaffolding with layout skeleton

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Canvas 渲染器 — 坐标 + 颜色 + 全部 9 层

**Files:**
- Create: `vis/frontend/src/renderer/geometry.js`
- Create: `vis/frontend/src/renderer/colors.js`
- Create: `vis/frontend/src/renderer/layers.js`

**Interfaces:**
- Produces: `coordToPixel(col, row, cellSize, ox, oy)` → 坐标映射
- Produces: `infoValueToHSL(I, V)` → HSL 颜色编码
- Produces: `renderFrame(ctx, frame, cellSize, ox, oy, opts)` → 主渲染入口
- Produces: 各 layer 函数：`drawBackground`, `drawHeatmap`, `drawGridLines`, `drawSearchRegions`, `drawTrackRegions`, `drawMarkers`, `drawShips`, `drawUavs`, `drawPairingLines`, `drawHover`

- [ ] **Step 1: 创建 `vis/frontend/src/renderer/geometry.js`**

```js
/**
 * 网格坐标 ↔ Canvas 像素坐标映射。
 *
 * cellSize  = floor(min(canvasW, canvasH) / 32)
 * offsetX   = (canvasW - 30 * cellSize) / 2
 * offsetY   = (canvasH - 30 * cellSize) / 2
 */

export function computeLayout(canvasW, canvasH) {
  const cellSize = Math.floor(Math.min(canvasW, canvasH) / 32);
  const offsetX = (canvasW - 30 * cellSize) / 2;
  const offsetY = (canvasH - 30 * cellSize) / 2;
  return { cellSize, offsetX, offsetY };
}

export function coordToPixel(col, row, cellSize, offsetX, offsetY) {
  return {
    x: offsetX + col * cellSize,
    y: offsetY + row * cellSize,
  };
}

export function pixelToCoord(px, py, cellSize, offsetX, offsetY) {
  const col = Math.floor((px - offsetX) / cellSize);
  const row = Math.floor((py - offsetY) / cellSize);
  if (col < 0 || col >= 30 || row < 0 || row >= 30) return null;
  return { col, row };
}
```

- [ ] **Step 2: 创建 `vis/frontend/src/renderer/colors.js`**

```js
/**
 * 色值常量 + 双层热力颜色映射。
 */

// 搜索区优先级色
export const PRIORITY_COLORS = {
  high: "#F87171",
  medium: "#FBBF24",
  low: "#60A5FA",
};

// UAV 状态色
export const UAV_STATUS_COLORS = {
  searching: "#22C55E",
  tracking: "#EF4444",
  returning: "#F97316",
  refueling: "#3B82F6",
  idle: "#9CA3AF",
  transit: "#60A5FA",
};

// 标记点按年龄着色
export function markerColor(ageMinutes) {
  if (ageMinutes < 15) return { fill: "#F97316", alpha: 1.0 };
  if (ageMinutes < 45) return { fill: "#FBBF24", alpha: 0.8 };
  return { fill: "#9A3412", alpha: 0.5 };  // 45-60 min
}

/**
 * 信息素 I + 信息价值 V → HSL
 *
 * H = 120 × (1 - V)    → 0°(红,高价值) ~ 120°(绿,低价值)
 * S = V × 100%
 * L = 由 I 映射:  ≥0.7→85%, ≥0.2→60%, >0→25%, 0→10%
 */
export function infoValueToHSL(I, V) {
  const h = 120 * (1 - V);
  const s = V * 100;
  let l;
  if (I >= 0.7) l = 85;
  else if (I >= 0.2) l = 60;
  else if (I > 0) l = 25;
  else l = 10;
  return { h, s, l };
}

export function hslToString({ h, s, l }) {
  return `hsl(${h}, ${s}%, ${l}%)`;
}

/** 态势分类 */
export function infoCategory(I) {
  if (I >= 0.7) return "white";
  if (I >= 0.2) return "gray";
  return "black";
}

/** UAV 最大续航 (km) */
export const MAX_RANGE_KM = 4800;
```

- [ ] **Step 3: 创建 `vis/frontend/src/renderer/layers.js`**

```js
import { coordToPixel, pixelToCoord } from "./geometry";
import {
  PRIORITY_COLORS,
  UAV_STATUS_COLORS,
  markerColor,
  infoValueToHSL,
  hslToString,
  MAX_RANGE_KM,
} from "./colors";

// ── Layer 0: 背景 ──
export function drawBackground(ctx, w, h) {
  ctx.fillStyle = "#0D1117";
  ctx.fillRect(0, 0, w, h);
}

// ── Layer 1: 双层融合热力 ──
export function drawHeatmap(ctx, infoMatrix, valueMatrix, cellSize, ox, oy) {
  for (let col = 0; col < 30; col++) {
    for (let row = 0; row < 30; row++) {
      const I = (infoMatrix && infoMatrix[col] && infoMatrix[col][row] != null)
        ? infoMatrix[col][row] : 0;
      const V = (valueMatrix && valueMatrix[col] && valueMatrix[col][row] != null)
        ? valueMatrix[col][row] : 0;
      const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
      const hsl = infoValueToHSL(I, V);
      ctx.fillStyle = hslToString(hsl);
      ctx.fillRect(x, y, cellSize, cellSize);
    }
  }
}

// ── Layer 2: 网格线 ──
export function drawGridLines(ctx, cellSize, ox, oy, showGrid) {
  if (!showGrid) return;
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 30; i++) {
    const px = ox + i * cellSize;
    ctx.beginPath();
    ctx.moveTo(px, oy);
    ctx.lineTo(px, oy + 30 * cellSize);
    ctx.stroke();
    const py = oy + i * cellSize;
    ctx.beginPath();
    ctx.moveTo(ox, py);
    ctx.lineTo(ox + 30 * cellSize, py);
    ctx.stroke();
  }
}

// ── Layer 3: 搜索区矩形 ──
export function drawSearchRegions(ctx, regions, cellSize, ox, oy) {
  if (!regions) return;
  for (const r of regions) {
    const [cs, rs, ce, re] = r.bbox;
    const { x, y } = coordToPixel(cs, rs, cellSize, ox, oy);
    const w = (ce - cs) * cellSize;
    const h = (re - rs) * cellSize;
    const color = PRIORITY_COLORS[r.priority] || PRIORITY_COLORS.medium;

    // 填充
    ctx.fillStyle = color + "18";
    ctx.fillRect(x, y, w, h);

    // 边框
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, w, h);

    // 标签
    const label = `${r.id} ${r.completion_pct != null ? Math.round(r.completion_pct) + "%" : ""}`;
    ctx.fillStyle = "#0D1117";
    const textW = ctx.measureText(label).width + 8;
    ctx.fillRect(x + 2, y + 2, textW, 18);
    ctx.fillStyle = color;
    ctx.font = "11px sans-serif";
    ctx.fillText(label, x + 6, y + 15);
  }
}

// ── Layer 4: 跟踪区矩形 ──
export function drawTrackRegions(ctx, regions, cellSize, ox, oy) {
  if (!regions) return;
  for (const r of regions) {
    const [cs, rs, ce, re] = r.bbox;
    const { x, y } = coordToPixel(cs, rs, cellSize, ox, oy);
    const w = (ce - cs) * cellSize;
    const h = (re - rs) * cellSize;

    ctx.fillStyle = "rgba(239,68,68,0.06)";
    ctx.fillRect(x, y, w, h);

    ctx.strokeStyle = "#EF4444";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);

    const label = r.id;
    ctx.fillStyle = "#EF4444";
    ctx.fillRect(x + 2, y + 2, ctx.measureText(label).width + 8, 18);
    ctx.fillStyle = "#FFF";
    ctx.font = "11px sans-serif";
    ctx.fillText(label, x + 6, y + 15);
  }
}

// ── Layer 5: 标记点 ──
export function drawMarkers(ctx, markers, cellSize, ox, oy, currentTimeMin, frameCount) {
  if (!markers) return;
  for (const m of markers) {
    const [col, row] = m.position;
    const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
    const cx = x + cellSize / 2;
    const cy = y + cellSize / 2;
    const age = currentTimeMin - m.created_time_min;
    if (age > 60) continue;

    const { fill, alpha } = markerColor(age);
    const r = 6 + 4 * Math.sin(frameCount / 50);

    ctx.globalAlpha = alpha;
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(r, 3), 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;

    // 标签
    ctx.fillStyle = "#FFF";
    ctx.font = "10px sans-serif";
    ctx.fillText(m.id, cx + r + 4, cy + 4);
  }
}

// ── Layer 6: 船舶 + 轨迹 ──
export function drawShips(ctx, ships, cellSize, ox, oy) {
  if (!ships) return;
  const groupColors = { G1: "#EF4444", G2: "#3B82F6", G3: "#FBBF24" };
  for (const s of ships) {
    const [col, row] = s.position;
    const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
    const cx = x + cellSize / 2;
    const cy = y + cellSize / 2;
    const color = groupColors[s.group_id] || "#9CA3AF";

    // 轨迹尾迹
    if (s.trail && s.trail.length > 1) {
      ctx.strokeStyle = color + "40";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      const first = s.trail[0];
      const fp = coordToPixel(
        Math.round(first[0]), Math.round(first[1]), cellSize, ox, oy
      );
      ctx.moveTo(fp.x + cellSize / 2, fp.y + cellSize / 2);
      for (let i = 0; i < s.trail.length; i++) {
        const pt = s.trail[i];
        const pp = coordToPixel(
          Math.round(pt[0]), Math.round(pt[1]), cellSize, ox, oy
        );
        ctx.lineTo(pp.x + cellSize / 2, pp.y + cellSize / 2);
      }
      ctx.stroke();
    }

    // 船舶三角
    const size = cellSize * 0.35;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(cx, cy - size);
    ctx.lineTo(cx - size * 0.7, cy + size * 0.5);
    ctx.lineTo(cx + size * 0.7, cy + size * 0.5);
    ctx.closePath();
    ctx.fill();

    // 被跟踪光环
    if (s.is_detected) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, size + 3, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
}

// ── Layer 7: UAV + 基地 ──
export function drawUavs(ctx, uavs, basePos, cellSize, ox, oy, selectedUavId) {
  // 基地
  if (basePos) {
    const [bc, br] = basePos;
    const bp = coordToPixel(bc, br, cellSize, ox, oy);
    const bcx = bp.x + cellSize / 2;
    const bcy = bp.y + cellSize / 2;
    const s = cellSize * 0.45;
    ctx.fillStyle = "#6B7280";
    ctx.beginPath();
    ctx.moveTo(bcx, bcy - s);
    ctx.lineTo(bcx - s, bcy + s * 0.6);
    ctx.lineTo(bcx + s, bcy + s * 0.6);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "#6B7280";
    ctx.font = "10px sans-serif";
    ctx.fillText("基地", bcx - 12, bcy + s + 14);
  }

  if (!uavs) return;
  for (const u of uavs) {
    const [col, row] = u.position;
    const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
    const cx = x + cellSize / 2;
    const cy = y + cellSize / 2;
    const color = UAV_STATUS_COLORS[u.status] || "#9CA3AF";
    const size = cellSize * 0.3;
    const isSelected = u.id === selectedUavId;
    const drawSize = isSelected ? size * 1.4 : size;

    // 油量环形指示
    const rangePct = (u.remaining_range_km || 0) / MAX_RANGE_KM;
    ctx.strokeStyle = color + "40";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cx, cy, drawSize + 5, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2);
    ctx.stroke();
    if (rangePct > 0) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy, drawSize + 5, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * rangePct);
      ctx.stroke();
    }

    // UAV 三角
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(cx, cy - drawSize);
    ctx.lineTo(cx - drawSize * 0.7, cy + drawSize * 0.5);
    ctx.lineTo(cx + drawSize * 0.7, cy + drawSize * 0.5);
    ctx.closePath();
    ctx.fill();

    // 标签
    ctx.fillStyle = "#FFF";
    ctx.font = isSelected ? "bold 11px sans-serif" : "10px sans-serif";
    const label = u.id.replace("UAV-", "U-");
    ctx.fillText(label, cx - 10, cy + drawSize + 14);
  }
}

// ── Layer 8: 配对连线（动画） ──
let pairingAnimations = [];

export function triggerPairing(uavId, regionBbox, cellSize, ox, oy) {
  // 找到 UAV 位置（由外部传入）
  pairingAnimations.push({
    uavId,
    regionBbox,
    startTime: performance.now(),
    duration: 3000,
  });
}

export function drawPairingLines(ctx, uavs, cellSize, ox, oy, now) {
  pairingAnimations = pairingAnimations.filter((a) => now - a.startTime < a.duration);
  for (const anim of pairingAnimations) {
    const elapsed = now - anim.startTime;
    const alpha = 1 - elapsed / anim.duration;

    // UAV 位置
    const uav = uavs?.find((u) => u.id === anim.uavId);
    if (!uav) continue;
    const [uc, ur] = uav.position;
    const up = coordToPixel(uc, ur, cellSize, ox, oy);

    // 区域中心
    const [cs, rs, ce, re] = anim.regionBbox;
    const rcx = ox + ((cs + ce) / 2) * cellSize;
    const rcy = oy + ((rs + re) / 2) * cellSize;

    ctx.strokeStyle = `rgba(96,165,250,${alpha})`;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(up.x + cellSize / 2, up.y + cellSize / 2);
    ctx.lineTo(rcx, rcy);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

// ── Layer 9: Hover 交互（绘制浮窗 tooltip） ──
export function drawHoverTooltip(ctx, hoverInfo, cellSize, ox, oy) {
  if (!hoverInfo) return null;
  const { col, row, I, V, category } = hoverInfo;
  const { x, y } = coordToPixel(col, row, cellSize, ox, oy);

  // Cell 高亮边框
  ctx.strokeStyle = "rgba(255,255,255,0.25)";
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, cellSize, cellSize);

  // Tooltip 定位（避免超出 canvas）
  const tipX = Math.min(x + cellSize + 8, ctx.canvas.width - 170);
  const tipY = Math.min(y, ctx.canvas.height - 70);
  const tipW = 160;
  const tipH = 52;

  ctx.fillStyle = "rgba(22,27,34,0.95)";
  ctx.fillRect(tipX, tipY, tipW, tipH);
  ctx.strokeStyle = "#30363D";
  ctx.lineWidth = 1;
  ctx.strokeRect(tipX, tipY, tipW, tipH);

  ctx.fillStyle = "#E6EDF3";
  ctx.font = "12px sans-serif";
  ctx.fillText(`Cell(${col},${row})`, tipX + 8, tipY + 18);
  ctx.fillText(`信息素: ${I.toFixed(2)}  价值: ${V.toFixed(2)}`, tipX + 8, tipY + 34);
  ctx.fillStyle = category === "black" ? "#EF4444" : category === "gray" ? "#FBBF24" : "#22C55E";
  ctx.fillText(`${category === "black" ? "黑" : category === "gray" ? "灰" : "白"}态势`, tipX + 8, tipY + 48);
}

// ── 主渲染入口 ──
export function renderFrame(ctx, frame, opts = {}) {
  const {
    cellSize, offsetX, offsetY,
    showGrid = false,
    hoverInfo = null,
    selectedUavId = null,
    frameCount = 0,
  } = opts;

  const w = ctx.canvas.width;
  const h = ctx.canvas.height;

  drawBackground(ctx, w, h);
  if (frame) {
    drawHeatmap(ctx, frame.info_matrix, frame.value_matrix, cellSize, offsetX, offsetY);
    drawGridLines(ctx, cellSize, offsetX, offsetY, showGrid);
    drawSearchRegions(ctx, frame.search_regions, cellSize, offsetX, offsetY);
    drawTrackRegions(ctx, frame.track_regions, cellSize, offsetX, offsetY);
    drawMarkers(ctx, frame.markers, cellSize, offsetX, offsetY, frame.sim_time_min, frameCount);
    drawShips(ctx, frame.ships, cellSize, offsetX, offsetY);
    drawUavs(ctx, frame.uavs, frame.base_position, cellSize, offsetX, offsetY, selectedUavId);
    drawPairingLines(ctx, frame.uavs, cellSize, offsetX, offsetY, performance.now());
  }
  drawHoverTooltip(ctx, hoverInfo, cellSize, offsetX, offsetY);
}
```

- [ ] **Step 4: 手动验证**

暂无自动化测试（Canvas 渲染为视觉产物）。在浏览器 console 中验证模块加载正常：

```js
// 在 CanvasMap 组件中 import 后验证无报错即可
import { renderFrame } from "../renderer/layers";
console.log("renderer loaded:", typeof renderFrame);
```

- [ ] **Step 5: Commit**

```bash
git add vis/frontend/src/renderer/
git commit -m "feat: add canvas renderer with 9-layer pipeline (heatmap, entities, interaction)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: CanvasMap 组件完整实现

**Files:**
- Modify: `vis/frontend/src/components/CanvasMap.jsx` (从骨架到完整)

**Interfaces:**
- Consumes: `renderFrame` from `renderer/layers.js`, `computeLayout` from `renderer/geometry.js`
- Produces: `CanvasMap` 完整组件 — 响应式 Canvas、ResizeObserver、hover 检测、UAV 点击

- [ ] **Step 1: 完整 `CanvasMap.jsx`**

```jsx
import { useRef, useEffect, useCallback } from "react";
import { renderFrame } from "../renderer/layers";
import { computeLayout, pixelToCoord } from "../renderer/geometry";

export default function CanvasMap({
  frame,
  selectedUavId,
  onSelectUav,
  showGrid = false,
  mode = "live",
}) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const layoutRef = useRef({ cellSize: 20, offsetX: 0, offsetY: 0 });
  const hoverRef = useRef(null);
  const frameCountRef = useRef(0);
  const rafRef = useRef(null);

  // 响应式尺寸
  const updateSize = useCallback(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const w = container.clientWidth;
    const h = container.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    layoutRef.current = computeLayout(w, h);
  }, []);

  // ResizeObserver
  useEffect(() => {
    updateSize();
    const obs = new ResizeObserver(updateSize);
    if (containerRef.current) obs.observe(containerRef.current);
    return () => obs.disconnect();
  }, [updateSize]);

  // 主渲染循环
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    const render = () => {
      const { cellSize, offsetX, offsetY } = layoutRef.current;
      ctx.save();
      renderFrame(ctx, frame, {
        cellSize,
        offsetX,
        offsetY,
        showGrid,
        hoverInfo: hoverRef.current,
        selectedUavId,
        frameCount: frameCountRef.current,
      });
      ctx.restore();
      frameCountRef.current++;
      rafRef.current = requestAnimationFrame(render);
    };

    rafRef.current = requestAnimationFrame(render);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [frame, showGrid, selectedUavId]);

  // 鼠标 hover → cell tooltip
  const handleMouseMove = useCallback((e) => {
    const canvas = canvasRef.current;
    if (!canvas || !frame) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const { cellSize, offsetX, offsetY } = layoutRef.current;
    const coord = pixelToCoord(mx, my, cellSize, offsetX, offsetY);
    if (coord && frame.info_matrix && frame.value_matrix) {
      const I = (frame.info_matrix[coord.col] || [])[coord.row] || 0;
      const V = (frame.value_matrix[coord.col] || [])[coord.row] || 0;
      let cat = "black";
      if (I >= 0.7) cat = "white";
      else if (I >= 0.2) cat = "gray";
      hoverRef.current = { col: coord.col, row: coord.row, I, V, category: cat };
    } else {
      hoverRef.current = null;
    }
  }, [frame]);

  // 点击 UAV 选中
  const handleClick = useCallback((e) => {
    if (!frame || !frame.uavs) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const { cellSize, offsetX, offsetY } = layoutRef.current;

    for (const u of frame.uavs) {
      const [col, row] = u.position;
      const cx = offsetX + col * cellSize + cellSize / 2;
      const cy = offsetY + row * cellSize + cellSize / 2;
      const dist = Math.sqrt((mx - cx) ** 2 + (my - cy) ** 2);
      if (dist < cellSize * 0.5) {
        onSelectUav?.(u.id === selectedUavId ? null : u.id);
        return;
      }
    }
  }, [frame, selectedUavId, onSelectUav]);

  return (
    <div className="canvas-area" ref={containerRef}>
      <canvas
        ref={canvasRef}
        onMouseMove={handleMouseMove}
        onClick={handleClick}
        style={{ cursor: hoverRef.current ? "crosshair" : "default" }}
      />
    </div>
  );
}
```

- [ ] **Step 2: 验证 — 传入模拟帧测试渲染**

在 `App.jsx` 中传入测试帧数据（见 Task 10），确认 Canvas 正确绘制热力 grid、无报错。

- [ ] **Step 3: Commit**

```bash
git add vis/frontend/src/components/CanvasMap.jsx
git commit -m "feat: implement CanvasMap with responsive sizing, hover tooltip, and UAV click selection

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 右侧边栏 — SimStatus + UavList + LlmSummary

**Files:**
- Modify: `vis/frontend/src/components/RightSidebar.jsx`
- Create: `vis/frontend/src/components/SimStatus.jsx`
- Create: `vis/frontend/src/components/UavList.jsx`
- Create: `vis/frontend/src/components/UavCard.jsx`
- Create: `vis/frontend/src/components/LlmSummary.jsx`

**Interfaces:**
- Produces: 侧边栏完整 UI（三区块）
- Consumes: `frame` 对象（来自 §7.1 帧格式）

- [ ] **Step 1: 创建 `SimStatus.jsx`**

```jsx
import { Clock, Radio, Brain } from "lucide-react";

export default function SimStatus({ frame, mode }) {
  if (!frame) {
    return (
      <div style={{ padding: 16, color: "var(--text-muted)", fontSize: 13 }}>
        等待仿真数据...
      </div>
    );
  }

  const llmCycle = frame.llm_cycle;
  const isLive = mode === "live";

  return (
    <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border)" }}>
      {/* 仿真时间 */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
          <Clock size={14} color="var(--text-secondary)" />
          <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>仿真时间</span>
        </div>
        <div style={{ fontSize: 28, fontWeight: 700, fontVariantNumeric: "tabular-nums" }}>
          {frame.timestamp}
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          第 {frame.frame_id} 步 / {frame.total_steps}
        </div>
      </div>

      {/* 模式指示 */}
      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 6 }}>
        <Radio size={14} color={isLive ? "var(--color-green)" : "var(--color-blue)"} />
        <span style={{ fontSize: 13, color: isLive ? "var(--color-green)" : "var(--color-blue)" }}>
          {isLive ? "直播中" : "回放"}
        </span>
      </div>

      {/* LLM 周期 */}
      {llmCycle && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
            <Brain size={14} color="var(--text-secondary)" />
            <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>LLM 周期</span>
          </div>
          <div style={{ fontSize: 13, color: "var(--text-primary)", lineHeight: 1.6 }}>
            <div>周期: #{llmCycle.cycle}</div>
            <div>触发: {llmCycle.trigger_reason || "—"}</div>
            <div>
              状态:{" "}
              <span style={{
                color: llmCycle.status === "passed"
                  ? "var(--color-green)"
                  : llmCycle.status === "failed"
                    ? "var(--color-red)"
                    : "var(--color-yellow)",
              }}>
                {llmCycle.status === "passed" ? "✅ 通过" : llmCycle.status === "failed" ? "❌ 失败" : "⏳ 待触发"}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: 创建 `UavCard.jsx`**

```jsx
import { MAX_RANGE_KM, UAV_STATUS_COLORS } from "../../renderer/colors";

const STATUS_LABELS = {
  searching: "搜索中",
  tracking: "跟踪中",
  returning: "返航中",
  refueling: "加油中",
  idle: "待命中",
  transit: "转场中",
};

export default function UavCard({ uav, isSelected, onClick }) {
  const color = UAV_STATUS_COLORS[uav.status] || "var(--color-gray)";
  const isAirborne = ["searching", "tracking", "returning", "transit"].includes(uav.status);
  const isGround = ["refueling", "idle"].includes(uav.status);
  const rangePct = (uav.remaining_range_km || 0) / MAX_RANGE_KM;

  // 副标题文本（按状态）
  let subtitle = "";
  if (uav.status === "searching" || uav.status === "tracking") {
    subtitle = uav.assigned_region_id
      ? `${uav.assigned_region_id} 区域`
      : "";
    if (uav.target_group_id) subtitle += ` · ${uav.target_group_id}`;
    subtitle += ` · ${uav.remaining_range_km?.toLocaleString()}km`;
  } else if (uav.status === "returning") {
    subtitle = `返回基地 · ${uav.remaining_range_km?.toLocaleString()}km`;
  } else if (uav.status === "refueling") {
    subtitle = `基地 · 剩余 ${uav.time_to_available_min ?? "?"}min`;
  } else if (uav.status === "idle") {
    subtitle = "基地";
  } else {
    subtitle = `${uav.remaining_range_km?.toLocaleString() ?? "?"}km`;
  }

  return (
    <div
      onClick={onClick}
      style={{
        padding: "8px 12px",
        borderBottom: "1px solid var(--border)",
        cursor: "pointer",
        background: isSelected ? "var(--bg-tertiary)" : "transparent",
        transition: "background 0.15s",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {/* 状态图标 */}
          <span style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: isGround ? 0 : "50%",
            border: isAirborne && !isGround ? `2px solid ${color}` : "none",
            background: isGround ? color : "transparent",
            transform: isGround ? "rotate(45deg)" : "none",
          }} />
          <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
            {uav.id.replace("UAV-", "U-")}
          </span>
          <span style={{ fontSize: 12, color }}>{STATUS_LABELS[uav.status] || uav.status}</span>
        </div>
        <span style={{ fontSize: 12, color: "var(--text-muted)", fontVariantNumeric: "tabular-nums" }}>
          {Math.round(rangePct * 100)}%
        </span>
      </div>

      {/* 油量进度条 */}
      <div style={{
        height: 3,
        background: "var(--bg-tertiary)",
        borderRadius: 2,
        marginBottom: 4,
        overflow: "hidden",
      }}>
        <div style={{
          width: `${rangePct * 100}%`,
          height: "100%",
          background: color,
          borderRadius: 2,
          transition: "width 0.3s",
        }} />
      </div>

      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{subtitle}</div>
    </div>
  );
}
```

- [ ] **Step 3: 创建 `UavList.jsx`**

```jsx
import UavCard from "./UavCard";

export default function UavList({ uavs, selectedUavId, onSelectUav }) {
  if (!uavs) {
    return <div style={{ padding: 12, color: "var(--text-muted)", fontSize: 13 }}>无 UAV 数据</div>;
  }

  const available = uavs.filter((u) => u.status === "idle").length;
  const total = uavs.length;

  return (
    <div style={{ borderBottom: "1px solid var(--border)" }}>
      <div style={{
        padding: "8px 16px",
        fontSize: 12,
        fontWeight: 600,
        color: "var(--text-secondary)",
        display: "flex",
        justifyContent: "space-between",
      }}>
        <span>UAV 编队</span>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>{available}/{total}</span>
      </div>
      <div style={{ maxHeight: 420, overflowY: "auto" }}>
        {uavs.map((u) => (
          <UavCard
            key={u.id}
            uav={u}
            isSelected={u.id === selectedUavId}
            onClick={() => onSelectUav?.(u.id === selectedUavId ? null : u.id)}
          />
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: 创建 `LlmSummary.jsx`**

```jsx
export default function LlmSummary({ llmCycle }) {
  if (!llmCycle || !llmCycle.llm_response) return null;

  const resp = llmCycle.llm_response;
  const regions = resp.search_regions || [];
  const totalCells = regions.reduce(
    (sum, r) => sum + (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]), 0
  );

  return (
    <div style={{ padding: "12px 16px" }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 8 }}>
        🧠 最近决策
      </div>
      <div style={{ fontSize: 13, color: "var(--text-primary)", lineHeight: 1.7 }}>
        <div>周期: #{llmCycle.cycle}</div>
        <div>搜索区: {regions.map((r) => r.id).join(" ") || "—"}</div>
        <div>覆盖: ~{totalCells} 格</div>
        <div>需 UAV: {regions.length} 架</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: 更新 `RightSidebar.jsx`**

```jsx
import SimStatus from "./SimStatus";
import UavList from "./UavList";
import LlmSummary from "./LlmSummary";

export default function RightSidebar({ frame, mode, selectedUavId, onSelectUav }) {
  return (
    <div className="sidebar">
      <SimStatus frame={frame} mode={mode} />
      <UavList
        uavs={frame?.uavs}
        selectedUavId={selectedUavId}
        onSelectUav={onSelectUav}
      />
      <LlmSummary llmCycle={frame?.llm_cycle} />
    </div>
  );
}
```

- [ ] **Step 6: 验证 — 组件在布局中正确渲染**

传入 mock frame 数据，确认三个区块排列正确、UAV 卡片状态色和进度条渲染准确。

- [ ] **Step 7: Commit**

```bash
git add vis/frontend/src/components/RightSidebar.jsx vis/frontend/src/components/SimStatus.jsx vis/frontend/src/components/UavList.jsx vis/frontend/src/components/UavCard.jsx vis/frontend/src/components/LlmSummary.jsx
git commit -m "feat: implement right sidebar with SimStatus, UavList, and LlmSummary

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 底部抽屉 — EventTimeline + RegionTable + LlmLog + ParamView

**Files:**
- Modify: `vis/frontend/src/components/BottomDrawer.jsx`
- Create: `vis/frontend/src/components/EventTimeline.jsx`
- Create: `vis/frontend/src/components/RegionTable.jsx`
- Create: `vis/frontend/src/components/LlmLog.jsx`
- Create: `vis/frontend/src/components/ParamView.jsx`

**Interfaces:**
- Produces: 底部抽屉 4 个 Tab 的完整内容
- Consumes: `frame` 对象

- [ ] **Step 1: 创建 `EventTimeline.jsx`**

```jsx
const EVENT_COLORS = {
  heavy_trigger: "#EF4444",
  light_trigger: "#FBBF24",
  target_found: "#22C55E",
  target_lost: "#EF4444",
  uav_status: "#3B82F6",
  hungarian_pairing: "#FBBF24",
};

const EVENT_LABELS = {
  heavy_trigger: "重量触发",
  light_trigger: "轻量触发",
  target_found: "目标发现",
  target_lost: "目标丢失",
  uav_status: "UAV状态",
  hungarian_pairing: "配对",
};

export default function EventTimeline({ events }) {
  if (!events || events.length === 0) {
    return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>暂无事件</div>;
  }

  // 按时间降序
  const sorted = [...events].sort((a, b) => (b.time_min || 0) - (a.time_min || 0));

  return (
    <div style={{ fontSize: 13 }}>
      {sorted.map((ev, i) => {
        const color = EVENT_COLORS[ev.type] || "#6E7681";
        const label = EVENT_LABELS[ev.type] || ev.type;
        const time = formatTime(ev.time_min);

        return (
          <div key={i} style={{
            padding: "6px 0",
            borderBottom: "1px solid var(--border)",
            lineHeight: 1.6,
          }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span style={{ color: "var(--text-muted)", fontVariantNumeric: "tabular-nums", minWidth: 48 }}>
                {time}
              </span>
              <span style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: color,
              }} />
              <span style={{ fontWeight: 600, color: "var(--text-primary)" }}>{label}</span>
            </div>
            <div style={{ marginLeft: 56, color: "var(--text-secondary)", fontSize: 12 }}>
              {ev.label || ev.details?.label || ""}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function formatTime(minutes) {
  if (minutes == null) return "--:--";
  const totalSec = Math.floor(minutes * 60);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}
```

- [ ] **Step 2: 创建 `RegionTable.jsx`**

```jsx
export default function RegionTable({ searchRegions, trackRegions, onSelectRegion }) {
  const allRegions = [
    ...(searchRegions || []).map((r) => ({ ...r, _kind: "搜索" })),
    ...(trackRegions || []).map((r) => ({ ...r, _kind: "跟踪" })),
  ];

  if (allRegions.length === 0) {
    return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>暂无区域</div>;
  }

  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr style={{ color: "var(--text-secondary)", textAlign: "left" }}>
          <th style={{ padding: "4px 8px" }}>区域</th>
          <th style={{ padding: "4px 8px" }}>类型</th>
          <th style={{ padding: "4px 8px" }}>范围</th>
          <th style={{ padding: "4px 8px" }}>面积</th>
          <th style={{ padding: "4px 8px" }}>价值</th>
          <th style={{ padding: "4px 8px" }}>覆盖率</th>
          <th style={{ padding: "4px 8px" }}>UAV</th>
        </tr>
      </thead>
      <tbody>
        {allRegions.map((r) => (
          <tr
            key={r.id}
            onClick={() => onSelectRegion?.(r)}
            style={{
              cursor: "pointer",
              background: r._kind === "跟踪" ? "rgba(239,68,68,0.05)" : "transparent",
            }}
          >
            <td style={{ padding: "4px 8px", fontWeight: 600 }}>{r.id}</td>
            <td style={{ padding: "4px 8px", color: r._kind === "跟踪" ? "var(--color-red)" : "var(--text-primary)" }}>
              {r._kind}
            </td>
            <td style={{ padding: "4px 8px", fontVariantNumeric: "tabular-nums" }}>
              [{r.bbox.join(",")}]
            </td>
            <td style={{ padding: "4px 8px", fontVariantNumeric: "tabular-nums" }}>
              {(r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1])}
            </td>
            <td style={{ padding: "4px 8px", fontVariantNumeric: "tabular-nums" }}>
              {r.info_value != null ? r.info_value.toFixed(2) : "—"}
            </td>
            <td style={{ padding: "4px 8px", fontVariantNumeric: "tabular-nums" }}>
              {r.completion_pct != null ? `${Math.round(r.completion_pct)}%` : r._kind === "跟踪" ? "跟踪中" : "—"}
            </td>
            <td style={{ padding: "4px 8px" }}>
              {r.assigned_uav_id?.replace("UAV-", "U-") || "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 3: 创建 `LlmLog.jsx`**

```jsx
import { useState } from "react";

const SUB_TABS = ["System Prompt", "User Prompt", "Response", "校验结果"];

export default function LlmLog({ llmCycle }) {
  const [subTab, setSubTab] = useState(0);

  if (!llmCycle) {
    return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>暂无 LLM 决策记录</div>;
  }

  const contents = [
    llmCycle.system_prompt || "（无）",
    llmCycle.user_prompt || "（无）",
    llmCycle.llm_response
      ? JSON.stringify(llmCycle.llm_response, null, 2)
      : "（无）",
    llmCycle.status === "passed"
      ? "✅ 校验通过 (面积✓ 长宽比✓ 不重叠✓ 稳定性✓)"
      : llmCycle.validation_errors?.join("\n") || "（无校验数据）",
  ];

  return (
    <div>
      <div style={{ marginBottom: 12, fontSize: 13, color: "var(--text-primary)" }}>
        周期 #{llmCycle.cycle} · {llmCycle.trigger_reason || "—"}
        {" · "}
        <span style={{ color: llmCycle.status === "passed" ? "var(--color-green)" : "var(--color-red)" }}>
          {llmCycle.status === "passed" ? "✅ 通过" : "❌ 失败"}
        </span>
        {llmCycle.retry_count > 0 && (
          <span style={{ color: "var(--color-yellow)", marginLeft: 8 }}>
            重试 {llmCycle.retry_count}/2
          </span>
        )}
      </div>

      {/* 子 Tab */}
      <div style={{ display: "flex", gap: 0, borderBottom: "1px solid var(--border)", marginBottom: 12 }}>
        {SUB_TABS.map((t, i) => (
          <div
            key={t}
            onClick={() => setSubTab(i)}
            style={{
              padding: "4px 12px",
              fontSize: 12,
              cursor: "pointer",
              color: i === subTab ? "var(--text-primary)" : "var(--text-secondary)",
              borderBottom: i === subTab ? "2px solid var(--color-blue)" : "2px solid transparent",
            }}
          >
            {t}
          </div>
        ))}
      </div>

      {/* 内容 */}
      <pre style={{
        fontSize: 12,
        color: "var(--text-primary)",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        maxHeight: 300,
        overflowY: "auto",
        background: "var(--bg-primary)",
        padding: 12,
        borderRadius: 4,
        margin: 0,
      }}>
        {contents[subTab]}
      </pre>
    </div>
  );
}
```

- [ ] **Step 4: 创建 `ParamView.jsx`**

```jsx
import { useState, useEffect } from "react";

export default function ParamView() {
  const [config, setConfig] = useState(null);
  const [expanded, setExpanded] = useState({});

  useEffect(() => {
    fetch("/api/config")
      .then((r) => r.json())
      .then(setConfig)
      .catch(() => {});  // 静默失败，API 未就绪时显示空
  }, []);

  if (!config) {
    return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>加载配置中...</div>;
  }

  const sections = {
    environment: { label: "环境参数", items: [
      ["海域", `${config.environment?.sea_area_km?.[0]}×${config.environment?.sea_area_km?.[1]} km`],
      ["基地", `(${config.environment?.base_position?.join(",")})`],
    ]},
    grid: { label: "网格参数", items: [
      ["分辨率", config.grid?.resolution?.join("×")],
      ["Cell大小", `${config.grid?.cell_size_km} km`],
      ["衰减半衰期", `${config.grid?.decay_half_life_min} min`],
      ["白/灰阈值", `${config.grid?.white_threshold}/${config.grid?.gray_threshold}`],
      ["搜索区", `${config.grid?.search_min_cells}–${config.grid?.search_max_cells} 格`],
      ["跟踪区", `${config.grid?.track_min_cells}–${config.grid?.track_max_cells} 格`],
      ["长宽比上限", config.grid?.aspect_ratio_max],
    ]},
    uav: { label: "UAV参数", items: [
      ["数量", `${config.uav?.count_max} 架`],
      ["巡航速度", `${config.uav?.cruise_speed_kmh} km/h`],
      ["续航", `${config.uav?.endurance_h} h`],
      ["加油", `${config.uav?.refuel_time_min} min`],
    ]},
    ship: { label: "船舶参数", items: [
      ["最少数量", `${config.ship?.count_min} 艘`],
      ["最多群组", config.ship?.max_groups],
      ["速度", `${config.ship?.speed_kn} 节`],
    ]},
    llm: { label: "LLM参数", items: [
      ["重量周期", `${config.llm?.heavy_cycle_min} min`],
      ["Reviewer周期", `${config.llm?.reviewer_cycle_min} min`],
      ["最大重试", config.llm?.max_retries],
    ]},
  };

  return (
    <div style={{ fontSize: 13 }}>
      {Object.entries(sections).map(([key, sec]) => {
        const isOpen = expanded[key] !== false;  // 默认展开
        return (
          <div key={key} style={{ marginBottom: 8 }}>
            <div
              onClick={() => setExpanded((e) => ({ ...e, [key]: !isOpen }))}
              style={{
                padding: "6px 8px",
                background: "var(--bg-tertiary)",
                borderRadius: 4,
                cursor: "pointer",
                fontWeight: 600,
                color: "var(--text-primary)",
                display: "flex",
                justifyContent: "space-between",
              }}
            >
              <span>{sec.label}</span>
              <span style={{ color: "var(--text-muted)" }}>{isOpen ? "▾" : "▸"}</span>
            </div>
            {isOpen && (
              <div style={{ padding: "4px 8px" }}>
                {sec.items.map(([label, value]) => (
                  <div key={label} style={{ display: "flex", justifyContent: "space-between", padding: "2px 0" }}>
                    <span style={{ color: "var(--text-secondary)" }}>{label}</span>
                    <span style={{ color: "var(--text-primary)", fontVariantNumeric: "tabular-nums" }}>
                      {value ?? "—"}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 5: 更新 `BottomDrawer.jsx`**（合并所有 Tab）

```jsx
import { useState } from "react";
import EventTimeline from "./EventTimeline";
import RegionTable from "./RegionTable";
import LlmLog from "./LlmLog";
import ParamView from "./ParamView";

const TABS = ["时间线", "区域详情", "LLM日志", "参数"];

export default function BottomDrawer({ frame, visible }) {
  const [activeTab, setActiveTab] = useState(0);

  if (!visible) return null;

  const tabContent = () => {
    switch (activeTab) {
      case 0: return <EventTimeline events={frame?.events} />;
      case 1: return (
        <RegionTable
          searchRegions={frame?.search_regions}
          trackRegions={frame?.track_regions}
        />
      );
      case 2: return <LlmLog llmCycle={frame?.llm_cycle} />;
      case 3: return <ParamView />;
      default: return null;
    }
  };

  return (
    <div className="bottom-drawer" style={{ height: "35vh" }}>
      <div className="drawer-tabs">
        {TABS.map((t, i) => (
          <div
            key={t}
            className={`drawer-tab ${i === activeTab ? "active" : ""}`}
            onClick={() => setActiveTab(i)}
          >
            {t}
          </div>
        ))}
      </div>
      <div className="drawer-content">{tabContent()}</div>
    </div>
  );
}
```

- [ ] **Step 6: 验证**

传入 mock frame，确认 4 个 Tab 切换正常，事件列表/区域表/LLM 日志/参数面板内容正确。

- [ ] **Step 7: Commit**

```bash
git add vis/frontend/src/components/BottomDrawer.jsx vis/frontend/src/components/EventTimeline.jsx vis/frontend/src/components/RegionTable.jsx vis/frontend/src/components/LlmLog.jsx vis/frontend/src/components/ParamView.jsx
git commit -m "feat: implement bottom drawer with 4 tabs (timeline, regions, LLM log, params)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: PlaybackBar + useReplay Hook

**Files:**
- Modify: `vis/frontend/src/components/PlaybackBar.jsx`
- Create: `vis/frontend/src/hooks/useReplay.js`

**Interfaces:**
- Produces: `useReplay(frames)` hook — `{ currentFrame, isPlaying, speed, seek, play, pause, stepForward, stepBack, jumpToStart, jumpToEnd }`
- Produces: `PlaybackBar` 完整组件

- [ ] **Step 1: 创建 `useReplay.js`**

```js
import { useState, useCallback, useRef, useEffect } from "react";

/**
 * 回放状态管理。
 * @param {Array|null} frames - 全量帧数组（null 表示未加载）
 * @returns 回放控制接口
 */
export default function useReplay(frames) {
  const [index, setIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const timerRef = useRef(null);

  const total = frames?.length || 0;
  const currentFrame = frames?.[index] || null;

  // 播放驱动
  useEffect(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    if (isPlaying && total > 0) {
      const interval = 1000 / speed;  // 1x = 1 frame/sec
      timerRef.current = setInterval(() => {
        setIndex((i) => {
          if (i + 1 >= total) {
            setIsPlaying(false);
            return i;
          }
          return i + 1;
        });
      }, interval);
    }
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [isPlaying, speed, total]);

  const play = useCallback(() => setIsPlaying(true), []);
  const pause = useCallback(() => setIsPlaying(false), []);
  const togglePlay = useCallback(() => setIsPlaying((p) => !p), []);

  const seek = useCallback((newIndex) => {
    const clamped = Math.max(0, Math.min(total - 1, newIndex));
    setIndex(clamped);
  }, [total]);

  const stepForward = useCallback(() => seek(index + 1), [seek, index]);
  const stepBack = useCallback(() => seek(index - 1), [seek, index]);
  const jumpToStart = useCallback(() => { setIsPlaying(false); setIndex(0); }, []);
  const jumpToEnd = useCallback(() => { setIsPlaying(false); setIndex(total - 1); }, [total]);

  const changeSpeed = useCallback((s) => setSpeed(s), []);

  return {
    currentFrame,
    index,
    total,
    isPlaying,
    speed,
    play,
    pause,
    togglePlay,
    seek,
    stepForward,
    stepBack,
    jumpToStart,
    jumpToEnd,
    changeSpeed,
  };
}
```

- [ ] **Step 2: 创建完整的 `PlaybackBar.jsx`**

```jsx
import {
  SkipBack, Rewind, Play, Pause, FastForward, SkipForward,
} from "lucide-react";

const SPEEDS = [1, 2, 5, 10];

export default function PlaybackBar({
  visible, currentTime, totalTime, isPlaying, speed,
  onPlay, onPause, onStepBack, onStepForward,
  onJumpStart, onJumpEnd, onSeek, onChangeSpeed,
}) {
  if (!visible) return null;

  const togglePlay = isPlaying ? onPause : onPlay;

  return (
    <div className="playback-bar">
      {/* 跳开头 */}
      <button onClick={onJumpStart} style={btnStyle} title="跳到开头">
        <SkipBack size={16} />
      </button>

      {/* 后退 10 步 */}
      <button onClick={onStepBack} style={btnStyle} title="后退 10 秒">
        <Rewind size={16} />
      </button>

      {/* 播放/暂停 */}
      <button onClick={togglePlay} style={btnStyle} title={isPlaying ? "暂停" : "播放"}>
        {isPlaying ? <Pause size={18} /> : <Play size={18} />}
      </button>

      {/* 快进 10 步 */}
      <button onClick={onStepForward} style={btnStyle} title="快进 10 秒">
        <FastForward size={16} />
      </button>

      {/* 跳末尾 */}
      <button onClick={onJumpEnd} style={btnStyle} title="跳到末尾">
        <SkipForward size={16} />
      </button>

      {/* 速度选择 */}
      <select
        value={speed}
        onChange={(e) => onChangeSpeed(Number(e.target.value))}
        style={{
          ...btnStyle,
          width: 56,
          fontSize: 13,
          textAlign: "center",
        }}
      >
        {SPEEDS.map((s) => (
          <option key={s} value={s}>{s}x</option>
        ))}
      </select>

      {/* 时间显示 */}
      <span style={{
        fontSize: 13,
        fontVariantNumeric: "tabular-nums",
        color: "var(--text-primary)",
        minWidth: 64,
        textAlign: "center",
      }}>
        {currentTime || "00:00:00"}
      </span>

      {/* 滑块 */}
      <input
        type="range"
        min={0}
        max={totalTime || 0}
        value={currentTime || 0}
        onChange={(e) => onSeek?.(Number(e.target.value))}
        style={{ flex: 1, accentColor: "var(--color-blue)" }}
      />

      <span style={{
        fontSize: 13,
        fontVariantNumeric: "tabular-nums",
        color: "var(--text-muted)",
        minWidth: 48,
        textAlign: "right",
      }}>
        / {formatTotal(totalTime)}
      </span>
    </div>
  );
}

const btnStyle = {
  background: "none",
  border: "none",
  color: "var(--text-secondary)",
  cursor: "pointer",
  padding: 4,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

function formatTotal(total) {
  if (!total || total === 0) return "00:00";
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}
```

- [ ] **Step 3: 验证**

在 App.jsx 中传入 mock 控制属性，确认按钮交互正常、滑块拖拽响应正确。

- [ ] **Step 4: Commit**

```bash
git add vis/frontend/src/components/PlaybackBar.jsx vis/frontend/src/hooks/useReplay.js
git commit -m "feat: implement PlaybackBar with full controls and useReplay hook

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: useWebSocket Hook + 直播模式

**Files:**
- Create: `vis/frontend/src/hooks/useWebSocket.js`

**Interfaces:**
- Produces: `useWebSocket(url)` → `{ frame, connected, error, sendPing }`

- [ ] **Step 1: 创建 `useWebSocket.js`**

```js
import { useState, useEffect, useRef, useCallback } from "react";

/**
 * WebSocket 连接管理 hook。
 * 自动重连，支持心跳 ping/pong。
 *
 * @param {string} url - WebSocket 地址 (如 "ws://localhost:8765/ws/live")
 * @returns {{ frame: object|null, connected: boolean, error: string|null }}
 */
export default function useWebSocket(url) {
  const [frame, setFrame] = useState(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);
  const wsRef = useRef(null);
  const pingTimerRef = useRef(null);

  useEffect(() => {
    let stopped = false;

    function connect() {
      if (stopped) return;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        if (stopped) { ws.close(); return; }
        setConnected(true);
        setError(null);
        // 心跳：每 25 秒发 ping
        pingTimerRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send("ping");
          }
        }, 25000);
      };

      ws.onmessage = (e) => {
        if (stopped) return;
        try {
          const data = JSON.parse(e.data);
          if (data !== "pong") {
            setFrame(data);
          }
        } catch {
          // 非 JSON 消息忽略（如 "pong"）
        }
      };

      ws.onerror = () => {
        if (!stopped) setError("WebSocket 连接错误");
      };

      ws.onclose = () => {
        if (stopped) return;
        setConnected(false);
        if (pingTimerRef.current) clearInterval(pingTimerRef.current);
        // 3 秒后重连
        setTimeout(connect, 3000);
      };
    }

    connect();

    return () => {
      stopped = true;
      if (pingTimerRef.current) clearInterval(pingTimerRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;  // 避免重连
        wsRef.current.close();
      }
    };
  }, [url]);

  const sendPing = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send("ping");
    }
  }, []);

  return { frame, connected, error, sendPing };
}
```

- [ ] **Step 2: 验证**

单元测试思路（手动确认）：启动后端 `uvicorn vis.backend.server:app`，前端连接 WebSocket 后 `connected` 变为 `true`。

- [ ] **Step 3: Commit**

```bash
git add vis/frontend/src/hooks/useWebSocket.js
git commit -m "feat: add useWebSocket hook with auto-reconnect and heartbeat

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: App.jsx 集成 + 回放文件选择 + 键盘快捷键

**Files:**
- Modify: `vis/frontend/src/App.jsx`
- Modify: `vis/frontend/src/App.css`（补充回放文件选择器样式）

**Interfaces:**
- Consumes: `useWebSocket`, `useReplay`, 所有组件
- Produces: 完整可运行的 Web 可视化应用

- [ ] **Step 1: 创建完整的 `App.jsx`**

```jsx
import { useState, useMemo, useEffect } from "react";
import CanvasMap from "./components/CanvasMap";
import RightSidebar from "./components/RightSidebar";
import BottomDrawer from "./components/BottomDrawer";
import PlaybackBar from "./components/PlaybackBar";
import useWebSocket from "./hooks/useWebSocket";
import useReplay from "./hooks/useReplay";

const WS_URL = `ws://${location.hostname}:8765/ws/live`;

export default function App() {
  const [mode, setMode] = useState("live");         // "live" | "replay"
  const [selectedUavId, setSelectedUavId] = useState(null);
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [showGrid, setShowGrid] = useState(false);

  // ── 直播模式 ──
  const { frame: liveFrame, connected } = useWebSocket(WS_URL);

  // ── 回放模式 ──
  const [replayFile, setReplayFile] = useState(null);
  const [replayFrames, setReplayFrames] = useState(null);
  const [replayFiles, setReplayFiles] = useState([]);
  const [loadingReplay, setLoadingReplay] = useState(false);

  const {
    currentFrame: replayFrame,
    index: replayIndex,
    total: replayTotal,
    isPlaying,
    speed,
    togglePlay,
    play: doPlay,
    pause: doPause,
    seek,
    stepForward,
    stepBack,
    jumpToStart,
    jumpToEnd,
    changeSpeed,
  } = useReplay(replayFrames);

  // 加载回放文件列表
  useEffect(() => {
    fetch("/api/replay/list")
      .then((r) => r.json())
      .then((data) => setReplayFiles(data.files || []))
      .catch(() => {});
  }, []);

  // 加载回放文件内容
  const loadReplayFile = async (filename) => {
    setLoadingReplay(true);
    try {
      const resp = await fetch(`/api/replay?file=${encodeURIComponent(filename)}`);
      const text = await resp.text();
      const frames = text
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line));
      setReplayFrames(frames);
      setReplayFile(filename);
      setMode("replay");
    } catch (err) {
      console.error("Failed to load replay:", err);
    } finally {
      setLoadingReplay(false);
    }
  };

  // 切换到直播
  const switchToLive = () => {
    setMode("live");
    setReplayFrames(null);
    setReplayFile(null);
  };

  // 当前活动帧
  const currentFrame = mode === "replay" ? replayFrame : liveFrame;

  // 回放时间参数
  const currentTimeSec = mode === "replay"
    ? Math.floor((replayFrame?.sim_time_min || 0) * 60)
    : 0;
  const totalTimeSec = useMemo(() => {
    if (!replayFrames?.length) return 0;
    const last = replayFrames[replayFrames.length - 1];
    return Math.floor((last?.sim_time_min || 0) * 60);
  }, [replayFrames]);

  // 回放进度条 seek（按 sim_time_min 匹配）
  const handleSeekByTime = (timeSec) => {
    if (!replayFrames) return;
    const targetMin = timeSec / 60;
    let best = 0;
    for (let i = 0; i < replayFrames.length; i++) {
      if ((replayFrames[i].sim_time_min || 0) <= targetMin) best = i;
    }
    seek(best);
  };

  // 键盘快捷键（回放模式）
  useEffect(() => {
    if (mode !== "replay") return;
    const handler = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
      switch (e.key) {
        case " ":
          e.preventDefault();
          togglePlay();
          break;
        case "ArrowLeft":
          stepBack();
          break;
        case "ArrowRight":
          stepForward();
          break;
        case "0": case "1": case "2": case "3": case "4":
        case "5": case "6": case "7": case "8": case "9": {
          const pct = parseInt(e.key) / 10;
          seek(Math.floor((replayTotal - 1) * pct));
          break;
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [mode, togglePlay, stepBack, stepForward, seek, replayTotal]);

  return (
    <div className={`app-layout ${mode === "replay" ? "replay-active" : ""}`}>
      {/* 主视图 */}
      <CanvasMap
        frame={currentFrame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
        showGrid={showGrid}
        mode={mode}
      />

      {/* 右侧边栏 */}
      <RightSidebar
        frame={currentFrame}
        mode={mode}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
      />

      {/* 底部抽屉 */}
      <BottomDrawer frame={currentFrame} visible={drawerVisible} />

      {/* 回放控制栏 */}
      {mode === "replay" && (
        <PlaybackBar
          visible
          currentTime={currentTimeSec}
          totalTime={totalTimeSec}
          isPlaying={isPlaying}
          speed={speed}
          onPlay={doPlay}
          onPause={doPause}
          onStepBack={stepBack}
          onStepForward={stepForward}
          onJumpStart={jumpToStart}
          onJumpEnd={jumpToEnd}
          onSeek={handleSeekByTime}
          onChangeSpeed={changeSpeed}
        />
      )}

      {/* 顶部工具栏（模式切换 / 网格线 / 抽屉 / 回放文件选择） */}
      <ToolBar
        mode={mode}
        connected={connected}
        showGrid={showGrid}
        drawerVisible={drawerVisible}
        replayFile={replayFile}
        replayFiles={replayFiles}
        loadingReplay={loadingReplay}
        replayIndex={replayIndex}
        replayTotal={replayTotal}
        onToggleGrid={() => setShowGrid((s) => !s)}
        onToggleDrawer={() => setDrawerVisible((d) => !d)}
        onSwitchToLive={switchToLive}
        onLoadReplay={loadReplayFile}
      />
    </div>
  );
}

// ── 顶部工具栏（浮动在 Canvas 上方） ──
function ToolBar({
  mode, connected, showGrid, drawerVisible,
  replayFile, replayFiles, loadingReplay,
  replayIndex, replayTotal,
  onToggleGrid, onToggleDrawer, onSwitchToLive, onLoadReplay,
}) {
  return (
    <div style={{
      position: "absolute",
      top: 8,
      left: 8,
      zIndex: 10,
      display: "flex",
      gap: 6,
      alignItems: "center",
    }}>
      {/* 模式切换 */}
      <button
        onClick={onSwitchToLive}
        style={toolBtnStyle(mode === "live")}
      >
        {mode === "live" ? "🔴 直播" : "📡 直播"}
      </button>

      {/* 回放文件选择 */}
      <select
        value={replayFile || ""}
        onChange={(e) => {
          if (e.target.value) onLoadReplay(e.target.value);
        }}
        style={{
          ...toolBtnStyle(false),
          maxWidth: 160,
          fontSize: 11,
        }}
        disabled={loadingReplay}
      >
        <option value="">{loadingReplay ? "加载中..." : "选择回放文件"}</option>
        {replayFiles.map((f) => (
          <option key={f} value={f}>{f}</option>
        ))}
      </select>

      {/* 回放进度 */}
      {mode === "replay" && replayTotal > 0 && (
        <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>
          {replayIndex + 1}/{replayTotal}
        </span>
      )}

      <div style={{ width: 1, height: 20, background: "var(--border)", margin: "0 4px" }} />

      {/* 网格线 */}
      <button onClick={onToggleGrid} style={toolBtnStyle(showGrid)}>
        ▦ 网格
      </button>

      {/* 抽屉 */}
      <button onClick={onToggleDrawer} style={toolBtnStyle(drawerVisible)}>
        {drawerVisible ? "▼ 面板" : "▲ 面板"}
      </button>

      {/* 连接状态 */}
      {mode === "live" && (
        <span style={{
          fontSize: 11,
          color: connected ? "var(--color-green)" : "var(--color-red)",
          marginLeft: 4,
        }}>
          {connected ? "已连接" : "未连接"}
        </span>
      )}
    </div>
  );
}

function toolBtnStyle(active) {
  return {
    background: active ? "var(--bg-tertiary)" : "rgba(22,27,34,0.85)",
    border: "1px solid var(--border)",
    color: active ? "var(--text-primary)" : "var(--text-secondary)",
    padding: "4px 10px",
    borderRadius: 4,
    cursor: "pointer",
    fontSize: 12,
    whiteSpace: "nowrap",
  };
}
```

- [ ] **Step 2: 补充 App.css 中 toolbar 相关样式**

在 `App.css` 末尾追加：

```css
/* 顶部工具栏 */
.canvas-area .toolbar {
  pointer-events: auto;
}
```

- [ ] **Step 3: 端到端验证**

```bash
# Terminal 1: 启动后端
cd vis/backend && uvicorn server:create_app --factory --host 0.0.0.0 --port 8765

# Terminal 2: 启动前端
cd vis/frontend && npm run dev
```

浏览器打开 `http://localhost:5173`，确认：
- 直播模式：WebSocket 连接成功，收到帧数据后 Canvas 渲染正常
- 回放模式：文件列表加载正常，选择文件后播放/暂停/拖拽正常工作
- 侧边栏/底部抽屉/键盘快捷键功能正常

- [ ] **Step 4: Commit**

```bash
git add vis/frontend/src/App.jsx vis/frontend/src/App.css
git commit -m "feat: wire App.jsx with live/replay modes, keyboard shortcuts, and toolbar

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Implementation Order

```
Task 1  →  Task 2  →  Task 3  →  Task 4  →  Task 5
                                            ↘
                                          Task 6  →  Task 7
                                            ↘
                                          Task 8  →  Task 9  →  Task 10
```

Tasks 4/5 和 6/7/8 可以部分并行（不同文件无冲突），但建议先完成 Canvas 渲染链路（Task 4+5）确保核心可视化可用，再做 UI chrome（Task 6-9），最后集成（Task 10）。
