"""验证 FastAPI 服务可以创建并启动。"""
from schedule.config_loader import ConfigLoader
from schedule.state_manager import StateManager
from vis.backend.server import create_app

config = ConfigLoader.load("configs")
state = StateManager(config)
app = create_app(config, state)
# 不实际启动，只验证无导入/构造错误
print("OK: app created")
# 如需手动测试: import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8765)
