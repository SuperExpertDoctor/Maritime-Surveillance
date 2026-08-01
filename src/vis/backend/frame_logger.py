"""JSONL 帧日志——仿真每步写入一行完整帧 JSON。"""
import json
import os
import time
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
        payload = json.dumps(frame, ensure_ascii=False) + "\n"
        for attempt in range(20):
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(payload)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                # Windows readers can briefly deny append access while a
                # replay file is being inspected. Keep the live run intact.
                time.sleep(0.05)
        self._count += 1
