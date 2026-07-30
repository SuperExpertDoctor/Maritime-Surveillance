"""开发模式启动：同时运行仿真后端 + 前端提示。

Usage:
    # 终端1: 启动仿真 (含 FastAPI WebSocket 服务)
    python scripts/run_simulation.py

    # 终端2: 启动前端 Vite 开发服务器
    cd src/vis/frontend && npm install && npm run dev

    # 浏览器访问: http://localhost:5173
"""

if __name__ == "__main__":
    print(__doc__)
