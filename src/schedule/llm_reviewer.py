"""Periodic real-LLM condensation of long-horizon simulation context."""
from __future__ import annotations

import json
import logging

from src.schedule.config_loader import AppConfig
from src.schedule.llm_client import LLMClient
from src.schedule.state_manager import StateManager


logger = logging.getLogger(__name__)


class LLMReviewer:
    def __init__(self, config: AppConfig, client: LLMClient):
        self.config = config
        self.client = client
        self._last_review_time = 0.0
        self._memory = ""

    @property
    def memory(self) -> str:
        return self._memory

    def step(self, current_time: float, sm: StateManager) -> str | None:
        cycle = self.config.llm.reviewer_cycle_min
        if current_time - self._last_review_time < cycle:
            return None
        self._last_review_time = current_time

        coverage = sm.get_coverage_stats()
        payload = {
            "sim_time_min": current_time,
            "coverage_pct": round(float(coverage["coverage_pct"]), 2),
            "events": sm.get_recent_events(since_time=max(0, current_time - 120))[-100:],
            "track_regions": [
                {"id": region.id, "bbox": list(region.bbox), "uav": region.assigned_uav_id}
                for region in sm.get_track_regions()
            ],
            "uavs": [
                {"id": uav.id, "status": uav.status, "fuel": round(uav.fuel_remaining_pct, 3)}
                for uav in sm.get_all_uavs()
            ],
        }
        system_prompt = (
            "你是 UAV 海上侦察任务 Reviewer。根据结构化态势生成一段不超过200字的长期记忆，"
            "供下一轮决策模型使用。只陈述输入支持的事实、风险和优先方向，不输出标题或列表。"
        )
        user_prompt = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            memory = self.client.review(system_prompt, user_prompt)
        except Exception as exc:
            logger.error("LongCat reviewer call failed: %s", exc)
            return None
        if not memory:
            return None
        self._memory = memory
        return memory


__all__ = ["LLMReviewer"]
