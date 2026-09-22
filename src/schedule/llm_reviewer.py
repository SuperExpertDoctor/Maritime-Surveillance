"""Periodic real-LLM condensation of long-horizon simulation context."""
from __future__ import annotations

import json
import logging
import os

from src.schedule.config_loader import AppConfig
from src.schedule.llm_client import LLMClient
from src.schedule.state_manager import StateManager
from src.mission.outcome_evaluator import EpisodeOutcome
from src.mission.strategy_memory import StrategyMemory, StrategyMemoryStore


logger = logging.getLogger(__name__)


class LLMReviewer:
    def __init__(
        self,
        config: AppConfig,
        client: LLMClient,
        *,
        strategy_memory_store: StrategyMemoryStore | None = None,
    ):
        self.config = config
        self.client = client
        self.strategy_memory_store = strategy_memory_store
        self._last_review_time = 0.0
        self._memory = ""
        prompt_path = os.path.join(
            os.path.dirname(__file__), "..", "mission", "prompts", "strategy_reviewer.txt",
        )
        with open(prompt_path, "r", encoding="utf-8") as stream:
            self.strategy_system_prompt = stream.read()

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
            "你是 UAV 海上侦察任务 Reviewer。用中文输出80至120字符的单行长期记忆。"
            "硬上限为200个Unicode字符，标点、空格、数字和英文字母也逐个计数，不是200个词。"
            "只保留最重要的两三项，不逐架罗列UAV或复述事件流水。"
            "供下一轮决策模型使用。只陈述输入支持的事实、风险和优先方向，不输出标题或列表。"
        )
        user_prompt = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            memory = self.client.review(
                system_prompt, user_prompt, snapshot_id=f"review-{current_time}",
            )
        except Exception as exc:
            logger.error("LongCat reviewer call failed: %s", type(exc).__name__)
            return None
        if not memory:
            return None
        self._memory = memory
        return memory

    def propose_strategy(
        self,
        outcomes: tuple[EpisodeOutcome, ...],
        decision_summaries: tuple[dict, ...],
    ) -> StrategyMemory | None:
        """Ask the offline strategy reviewer for one bounded candidate."""
        if self.strategy_memory_store is None:
            return None
        outcomes = tuple(outcomes)
        summaries = tuple(decision_summaries)
        if len(outcomes) < 3 or len(summaries) < 3 or len(outcomes) != len(summaries):
            return None
        if any(
            not isinstance(outcome, EpisodeOutcome)
            or not outcome.valid
            or not self.strategy_memory_store.is_safe_episode_id(outcome.episode_id)
            or "fixture" in outcome.episode_id.lower()
            for outcome in outcomes
        ):
            return None
        if any(
            not self.strategy_memory_store.is_safe_decision_summary(summary)
            for summary in summaries
        ):
            return None
        aggregate = {
            "outcomes": [
                {
                    "episode_id": outcome.episode_id,
                    "valid": outcome.valid,
                    "score": outcome.score,
                    "unique_coverage_ratio": outcome.unique_coverage_ratio,
                    "intent_satisfaction_ratio": outcome.intent_satisfaction_ratio,
                    "type_ii_tracking_ratio": outcome.type_ii_tracking_ratio,
                    "classification_accuracy": outcome.classification_accuracy,
                    "type_i_probe_uav_min": outcome.type_i_probe_uav_min,
                    "mean_probe_wait_min": outcome.mean_probe_wait_min,
                }
                for outcome in outcomes
            ],
            "decisions": list(summaries),
        }
        try:
            advice = self.client.review(
                self.strategy_system_prompt,
                json.dumps(aggregate, ensure_ascii=False, allow_nan=False),
                snapshot_id=f"strategy-review-{outcomes[-1].episode_id}",
            )
        except Exception as exc:
            logger.error("strategy reviewer call failed: %s", type(exc).__name__)
            return None
        candidate = self.strategy_memory_store.propose(
            outcomes, summaries, advice=advice or None,
        )
        if candidate is not None:
            self.strategy_memory_store.save_candidate(candidate)
        return candidate


__all__ = ["LLMReviewer"]
