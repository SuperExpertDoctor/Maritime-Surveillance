import os
from src.schedule.state_manager import StateManager
from src.schedule.info_value_table import InfoValueTable
from src.schedule.candidate_extractor import CandidateResult


class PromptBuilder:
    def __init__(self, system_prompt_path: str = None):
        if system_prompt_path is None:
            system_prompt_path = os.path.join(
                os.path.dirname(__file__), "prompts", "system_prompt.txt"
            )
        with open(system_prompt_path, "r", encoding="utf-8") as f:
            self.system_prompt = f.read()

    def build(self, sm: StateManager, ivt: InfoValueTable,
              candidate_result: CandidateResult,
              reviewer_memory: str = "") -> tuple[str, str]:
        """返回 (system_prompt, user_prompt)"""
        user = self._build_user_prompt(sm, ivt, candidate_result, reviewer_memory)
        return self.system_prompt, user

    def _build_user_prompt(self, sm: StateManager, ivt: InfoValueTable,
                           candidate_result: CandidateResult, reviewer_memory: str) -> str:
        parts = []

        # 长期记忆
        if reviewer_memory:
            parts.append(f"【长期记忆】\n{reviewer_memory}")

        # 候选搜索区域
        parts.append("【候选搜索区域】(按信息价值降序)")
        for i, cand in enumerate(candidate_result.candidate_regions):
            b = cand["bbox"]
            area = (b.col_end - b.col_start) * (b.row_end - b.row_start)
            info = cand.get("avg_info", 0.0)
            situation = "黑" if info < 0.2 else ("灰" if info <= 0.7 else "白")
            parts.append(
                f"{i+1}. bbox({b.col_start},{b.row_start},{b.col_end},{b.row_end}) "
                f"面积{area}格 平均信息{info:.2f}({situation}) "
                f"总价值{cand.get('total_value', 0):.2f}"
            )
        if not candidate_result.candidate_regions:
            parts.append(
                "无满足几何、占位与航路约束的候选区；本轮必须输出 "
                '"search_regions": []，不得自行创造 bbox。'
            )

        # 跟踪中区域
        tracks = sm.get_track_regions()
        if tracks:
            parts.append("\n【跟踪中区域】(由规则维护，不参与重新划分)")
            for t in tracks:
                b = t.bbox
                uav_id = t.assigned_uav_id or "unassigned"
                parts.append(
                    f"{t.id}: bbox({b.col_start},{b.row_start},{b.col_end},{b.row_end}) "
                    f"UAV={uav_id}"
                )

        # 上一轮搜索区状态
        ivt.update_all()
        prev_rows = [r for r in ivt.get_rows() if r.type == "search"]
        if prev_rows:
            parts.append("\n【上一轮搜索区状态】")
            for row in prev_rows:
                info = row.avg_info
                situation = "白" if info > 0.7 else ("灰" if info > 0.2 else "黑")
                uav = row.assigned_uav_id or "unassigned"
                st = row.status
                parts.append(
                    f"{row.region_id}: bbox({row.bbox.col_start},{row.bbox.row_start},"
                    f"{row.bbox.col_end},{row.bbox.row_end}) "
                    f"信息{info:.2f}({situation}) 状态={st} UAV={uav}"
                )

        # 碎片提醒
        if candidate_result.fragment_alerts:
            parts.append("\n【碎片提醒】")
            for frag in candidate_result.fragment_alerts:
                parts.append(f"- {frag['reason']}")

        obstacles = getattr(sm, "obstacles", [])
        if obstacles:
            parts.append("\n【禁飞障碍物】(搜索区不得覆盖，航路必须绕行)")
            for obstacle in obstacles:
                if hasattr(obstacle, "radius"):
                    parts.append(
                        f"- 雷云 {obstacle.id}: center=({obstacle.center[0]:.1f},"
                        f"{obstacle.center[1]:.1f}) radius={obstacle.radius:.1f}"
                    )
                else:
                    points = ",".join(
                        f"({x:.1f},{y:.1f})" for x, y in obstacle.vertices
                    )
                    parts.append(f"- 岛屿 {obstacle.id}: vertices={points}")

        # UAV 可用状态
        parts.append("\n【UAV 可用状态】")
        all_uavs = sm.get_all_uavs()
        available = [u for u in all_uavs if u.status == "idle"]
        in_use = [u for u in all_uavs if u.status != "idle"]
        retained = sm.get_active_search_regions()
        pending = sum(region.assigned_uav_id is None for region in retained)
        if sm.lifecycle_mode:
            new_capacity = max(
                0,
                10 - len(sm.get_track_regions()) - len(retained),
            )
        else:
            new_capacity = max(0, len(available) - pending)
        parts.append(
            f"现役搜索区将原样保留{len(retained)}个，其中待续派{pending}个；"
            f"本轮只输出新增区域，新增上限{new_capacity}个。"
        )
        parts.append(f"现可用UAV: {len(available)}架")
        for u in in_use:
            parts.append(f"  {u.id}: {u.status}, 油量{u.fuel_remaining_pct:.0%}, "
                        f"区域={u.assigned_region_id or 'none'}")
        parts.append(f"本周期新增区域容量: {new_capacity}个")

        parts.append("\n请输出本周期任务区域划分方案。")
        return "\n".join(parts)
