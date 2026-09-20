const EVENT_TYPE_ALIASES = {
  target_found: "target_found",
  ship_detected: "target_found",
  llm_decision: "llm_decision",
  mission_decision: "llm_decision",
  uav_returned: "uav_returned",
  mission_assignment_committed: "mission_assignment_committed",
  mission_assignment_approved: "mission_assignment_committed",
  handoff_assignment_committed: "mission_assignment_committed",
  contact_created: "contact_created",
  probe_phase_changed: "probe_phase_changed",
  type_i_assessed: "type_i_assessed",
  type_ii_assessed: "type_ii_assessed",
  assessment_applied: "assessment_applied",
  probe_timed_out: "probe_timed_out",
  probe_timeout: "probe_timed_out",
  mission_assignment_rejected: "task_failed",
  mission_selection_failed: "task_failed",
  decision_failed: "task_failed",
  route_plan_failed: "task_failed",
  conflict_replan_failed: "task_failed",
  task_failed: "task_failed",
  mission_task_released: "task_completed",
  task_completed: "task_completed",
  lifecycle_completed: "task_completed",
  uav_refueled: "uav_refueled",
};

function stableValue(value) {
  if (Array.isArray(value)) return `[${value.map(stableValue).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableValue(value[key])}`).join(",")}}`;
  }
  if (typeof value === "number") {
    if (Number.isNaN(value)) return "NaN";
    if (!Number.isFinite(value)) return String(value);
  }
  return JSON.stringify(value);
}

function normalizedTime(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : value ?? null;
}

function canonicalType(value) {
  const type = String(value || "");
  return EVENT_TYPE_ALIASES[type] || (type ? type : null);
}

function isEmptyAssignment(event) {
  const data = event.data;
  if (!data || typeof data !== "object") return false;
  const assignments = data.assignments ?? data.selected_tasks ?? data.selection;
  if (Array.isArray(assignments) && assignments.length === 0) return true;
  return ["assignment_count", "assigned_count", "selected_count"].some(
    (key) => data[key] === 0,
  );
}

export function replayEventKey(event, type = canonicalType(event?.type)) {
  return `${type || "unknown"}|${stableValue(normalizedTime(event?.time))}|${stableValue(event?.data || {})}`;
}

/** Collect stable first-occurrence markers from the loaded replay window. */
export function collectReplayMarkers(frames = []) {
  const markers = new Map();
  frames.forEach((frame, frameIndex) => {
    if (!frame) return;
    for (const event of frame.events || []) {
      const type = canonicalType(event?.type);
      if (!type || isEmptyAssignment(event)) continue;
      const key = replayEventKey(event, type);
      if (markers.has(key)) continue;
      const time = normalizedTime(event.time);
      const data = event.data && typeof event.data === "object" ? event.data : {};
      markers.set(key, {
        key,
        frameIndex,
        frameId: frame.frame_id ?? null,
        type,
        sourceType: event.type,
        time,
        data,
        event: { ...event, type, time, data },
      });
    }
  });
  return [...markers.values()];
}

export { canonicalType, stableValue };
