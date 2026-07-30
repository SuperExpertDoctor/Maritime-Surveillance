from src.schedule.config_loader import AppConfig
from src.schedule.state_manager import StateManager


class LLMReviewer:
    """Long-term memory condensation for the decision-maker agent.

    Current implementation uses a statistical summary of recent events
    (target found/lost, UAV returns, coverage percentage) rather than
    LLM-based condensation.  This keeps the reviewer lightweight and
    deterministic.

    TODO: Integrate LLMClient for true LLM-based memory condensation.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self._last_review_time: float = -float("inf")
        self._memory: str = ""

    @property
    def memory(self) -> str:
        return self._memory

    def step(self, current_time: float, sm: StateManager) -> str | None:
        """按 15min 周期更新长期记忆。返回新记忆或 None。"""
        cycle = self.config.llm.reviewer_cycle_min
        if current_time - self._last_review_time < cycle:
            return None

        self._last_review_time = current_time

        # 收集统计信息
        events = sm.get_recent_events(since_time=max(0, current_time - 120))  # 过去2小时

        total_found = sum(1 for e in events if e["type"] == "target_found")
        total_lost = sum(1 for e in events if e["type"] == "target_lost")
        total_returned = sum(1 for e in events if e["type"] == "uav_returned")

        tracks = sm.get_track_regions()
        tracking_info = ""
        for t in tracks:
            tracking_info += f"{t.id}跟踪中 "

        # 搜索覆盖率
        info_mat = sm.get_info_matrix()
        searched = (info_mat > 0.0).sum()
        total_cells = info_mat.size
        coverage_pct = searched / total_cells * 100

        # 凝练为自然语言
        memory = (
            f"过去2小时内，共搜索约{coverage_pct:.0f}%海域，"
            f"发现目标{total_found}次，丢失{total_lost}次。"
        )
        if tracking_info:
            memory += f" {tracking_info}。"
        if total_returned > 0:
            memory += f" {total_returned}架次UAV返航。"
        memory += f" (更新于t={current_time:.0f}min)"

        # 精简到 ≤ 200 字
        if len(memory) > 200:
            memory = memory[:197] + "..."

        self._memory = memory
        return memory
