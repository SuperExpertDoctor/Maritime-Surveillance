export function informationCategory(value, grid = {}) {
  const white = grid.white_threshold ?? 0.7;
  const gray = grid.gray_threshold ?? 0.2;
  return value >= white ? "white" : value >= gray ? "gray" : "black";
}

const TASK_PHASES = {
  coverage: {
    transit: { label: "转场搜索", tone: "transit", phase: "coverage_transit" },
    transit_astar: { label: "转场搜索", tone: "transit", phase: "coverage_transit" },
    align_scan: { label: "搜索扫描", tone: "search", phase: "coverage_scan" },
    scanning: { label: "搜索扫描", tone: "search", phase: "coverage_scan" },
    completed: { label: "待命", tone: "idle", phase: "idle" },
  },
  probe: {
    near: { label: "近距观察", tone: "observe", phase: "probe_near" },
    closing: { label: "近距观察", tone: "observe", phase: "probe_near" },
    awaiting_assessment: { label: "等待研判", tone: "assessment", phase: "probe_assessment" },
    finished: { label: "待命", tone: "idle", phase: "idle" },
  },
  track: {
    approach_astar: { label: "接近跟踪", tone: "approach", phase: "track_approach" },
    orbit_entry: { label: "接近跟踪", tone: "approach", phase: "track_approach" },
    tracking: { label: "持续跟踪", tone: "track", phase: "track_active" },
    lost: { label: "待命", tone: "idle", phase: "idle" },
    completed: { label: "待命", tone: "idle", phase: "idle" },
  },
  return: {
    return: { label: "返航", tone: "return", phase: "return" },
    transit: { label: "返航", tone: "return", phase: "return" },
  },
  holding: {
    holding: { label: "等待降落", tone: "holding", phase: "holding" },
  },
};

const LEGACY_STATUS = {
  idle: { label: "待命", tone: "idle", phase: "idle" },
  transit: { label: "转场搜索", tone: "transit", phase: "coverage_transit" },
  searching: { label: "搜索扫描", tone: "search", phase: "coverage_scan" },
  tracking: { label: "持续跟踪", tone: "track", phase: "track_active" },
  returning: { label: "返航", tone: "return", phase: "return" },
  holding: { label: "等待降落", tone: "holding", phase: "holding" },
  refueling: { label: "加油", tone: "refuel", phase: "refuel" },
};

function phaseResult(taskType, phase) {
  return TASK_PHASES[taskType]?.[phase] || null;
}

function probeDisplayState(taskVisual) {
  if (taskVisual.phase === "baseline") {
    return taskVisual.observation_started
      ? { label: "基线观察", tone: "observe", phase: "probe_baseline" }
      : { label: "接近调查", tone: "approach", phase: "probe_approach" };
  }
  return phaseResult("probe", taskVisual.phase)
    || { label: "接近调查", tone: "approach", phase: "probe_approach" };
}

/** Return the one display state shared by map, sidebar, and details. */
export function uavDisplayState(uav = {}) {
  if (uav.operational_status === "failed") {
    return { label: "故障停用", tone: "failed", phase: "failed" };
  }
  const taskVisual = uav.task_visual;
  if (taskVisual && taskVisual.route_source !== "none") {
    if (taskVisual.route_status === "cleared") {
      return { label: "待命", tone: "idle", phase: "idle" };
    }
    if (taskVisual.task_type === "probe") return probeDisplayState(taskVisual);
    const mapped = phaseResult(taskVisual.task_type, taskVisual.phase);
    if (mapped) return mapped;
  }
  return LEGACY_STATUS[uav.status] || LEGACY_STATUS.idle;
}

export function taskDisplayLabel(uav) {
  return uavDisplayState(uav).label;
}
