"""Public, JSON-ready outcome of one model allocation decision."""


def build_decision_record(result: dict, batch, applied: bool, time_min: float) -> dict | None:
    if result.get("trigger_type") != "heavy" or result.get("action") == "mission_selection_skipped":
        return None
    cycle = result.get("llm_cycle") or {}
    assignments = (
        [{"task_id": item.task_id, "uav_id": item.uav_id} for item in batch.assignments]
        if applied and batch is not None else []
    )
    return {
        "call_id": cycle.get("call_id"),
        "snapshot_id": result.get("snapshot_id"),
        "time_min": time_min,
        "trigger_source": result.get("trigger_source", "unknown"),
        "trigger_reason": result.get("trigger_reason", ""),
        "reason_content": cycle.get("reason_content", ""),
        "selected_task_ids": list(result.get("selected_task_ids") or []),
        "assignments": assignments,
        "involved_uav_ids": sorted(set(cycle.get("affected_uav_ids") or ()) | {
            item["uav_id"] for item in assignments
        }),
        "status": "committed" if applied and batch is not None else (
            "rejected" if batch is not None else "failed"
        ),
    }
