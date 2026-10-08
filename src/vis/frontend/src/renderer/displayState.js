export function informationCategory(value, grid = {}) {
  const white = grid.white_threshold ?? 0.7;
  const gray = grid.gray_threshold ?? 0.2;
  return value >= white ? "white" : value >= gray ? "gray" : "black";
}

// Canonical UAV operating states — the algorithm only allows three:
// 覆盖搜索 (coverage search, including the idle/transit pool), 跟踪目标
// (all contact-directed work: probing and tracking), 返航基地 (return leg,
// landing queue, refueling). A crashed airframe is a terminal marker, not
// an operating state.
const CANONICAL = {
  search: { label: "覆盖搜索", tone: "search", phase: "coverage" },
  tracking: { label: "跟踪目标", tone: "track", phase: "tracking" },
  returning: { label: "返航基地", tone: "return", phase: "returning" },
  crashed: { label: "坠毁", tone: "failed", phase: "crashed" },
};

const TARGET_TASK_TYPES = new Set(["probe", "track"]);
const RETURN_TASK_TYPES = new Set(["return", "holding"]);

function nearHomeBase(uav) {
  const pos = uav.position;
  const home = uav.home_base_grid;
  if (pos?.length >= 2 && home?.length >= 2) {
    return Math.hypot(pos[0] - home[0], pos[1] - home[1]) <= 3;
  }
  return true;
}

/** Return the one canonical display state shared by map, sidebar, details. */
export function uavDisplayState(uav = {}) {
  if (uav.operational_status === "failed") {
    return CANONICAL.crashed;
  }
  // A parked recovery-retry airframe is waiting to fly home — its only
  // legal outward state is 返航基地, no matter where it is parked.
  if (uav.operational_status === "recovery_wait") {
    return CANONICAL.returning;
  }
  const taskVisual = uav.task_visual;
  if (taskVisual && taskVisual.route_source !== "none") {
    if (taskVisual.route_status === "cleared") {
      return uav.status === "tracking" ? CANONICAL.tracking : CANONICAL.search;
    }
    if (TARGET_TASK_TYPES.has(taskVisual.task_type)) {
      return CANONICAL.tracking;
    }
    if (RETURN_TASK_TYPES.has(taskVisual.task_type)) {
      // A hold far from home base is a retask loiter — still in the
      // coverage fleet, not a landing queue.
      if (taskVisual.task_type === "holding" && !nearHomeBase(uav)) {
        return CANONICAL.search;
      }
      return CANONICAL.returning;
    }
    return CANONICAL.search;
  }
  if (uav.status === "tracking") return CANONICAL.tracking;
  if (uav.status === "returning" || uav.status === "refueling") {
    return CANONICAL.returning;
  }
  if (uav.status === "holding" && nearHomeBase(uav)) {
    return CANONICAL.returning;
  }
  return CANONICAL.search;
}

export function taskDisplayLabel(uav) {
  return uavDisplayState(uav).label;
}
