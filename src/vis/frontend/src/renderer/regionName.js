// 任务区域/任务 id → 中文显示名，全界面共用（地图标签、侧栏、决策表、
// 重点区面板、区域表）。后端 id 形如 partition:c0:r0:c1:r1 /
// search:c0:r0:c1:r1 / direction:OBS-<hash> / investigation:EMITTER-<目标> /
// T<序号>（跟踪区），一律显示中文名字，不出现英文代号。

export function regionAnchor(region) {
  const cells = region?.cells;
  if (Array.isArray(cells) && cells.length) return cells[0];
  if (Array.isArray(region?.bbox)) return [region.bbox[0], region.bbox[1]];
  return null;
}

export function regionDisplayName(id, anchor = null) {
  const raw = String(id || "");
  if (!raw) return "";
  const tail = (prefix) => raw.slice(prefix.length);
  const cell = Array.isArray(anchor) ? `${anchor[0]},${anchor[1]}` : "";
  if (raw.startsWith("partition:")) {
    const [c0, r0] = tail("partition:").split(":");
    return `分区${c0},${r0}`;
  }
  if (raw.startsWith("search:focus:")) {
    return `重点搜索${tail("search:focus:")}`;
  }
  if (raw.startsWith("search:")) {
    const [c0, r0] = tail("search:").split(":");
    return `搜索${c0},${r0}`;
  }
  if (raw.startsWith("direction:")) {
    return cell ? `定向${cell}` : "定向侦察";
  }
  if (raw.startsWith("investigation:")) {
    const target = tail("investigation:")
      .replace(/^EMITTER-/, "")
      .replace(/^scenario-vessel-/, "船");
    return `核查${target}`;
  }
  if (/^T\d+$/.test(raw)) return `跟踪区${raw}`;
  return raw.length > 14 ? `${raw.slice(0, 14)}…` : raw;
}

export function regionNameById(regions) {
  const byId = new Map((regions || []).map((region) => [region.id, region]));
  return (id) => regionDisplayName(id, regionAnchor(byId.get(id)));
}

// 事件类型 → 中文名（与后端 trigger_manager._EVENT_NAMES 同步）。
// 旧回放数据的决策原因存的是英文代号，这里在前端补译，新老数据统一显示中文。
const EVENT_NAMES = {
  contact_created: "新接触",
  contact_merged: "接触合并",
  contact_lost: "接触失联",
  assessment_changed: "评估更新",
  type_i_assessed: "I类船评估完成",
  type_ii_assessed: "II类船评估完成",
  type_i_released: "I类船解除",
  type_ii_confirmed: "II类船确认",
  resource_available: "资源空闲",
  mission_task_released: "任务释放",
  intent_changed: "重点区变更",
  intent_expired: "重点区过期",
  uav_returned: "无人机返航",
  target_found: "发现目标",
  target_lost: "目标丢失",
  lifecycle_completed: "轮换完成",
  target_departed: "目标驶离",
  storm_spawned: "风暴生成",
  storm_dissipated: "风暴消散",
  handoff_required: "需要交接",
  ais_transmission_changed: "AIS状态变更",
  surveillance_stage_changed: "侦察阶段变更",
  search_complete: "搜索完成",
  uav_refueled: "加油完成",
  base_capacity_full: "基地机位满",
  uav_fuel_low_warning: "油量不足预警",
  information_delta: "信息场更新",
};

const TRIGGER_REASON_NAMES = {
  "initial fleet deployment": "初始编队部署",
  operator_retry: "操作员手动重试",
};

// 信息场更新括号内的 reason code → 中文名（与后端 _REASON_CODE_NAMES 同步）。
const REASON_CODE_NAMES = {
  scan_sar: "SAR扫描",
  scan_search: "搜索扫描",
  scan_track: "跟踪扫描",
  scan_optical: "光电扫描",
  evasive_maneuver: "规避机动",
  passive_position: "被动定位",
  passive_bearing: "被动测向",
  ais_position: "AIS定位",
  type_ii_assessment: "II类评估",
  violation_assessment: "违规评估",
  handoff: "目标交接",
  evidence_expired: "证据过期",
  time_decay: "时间衰减",
  contact_created: "新接触",
};

const translateReasonBits = (text) => String(text || "")
  .split(", ")
  .map((bit) => REASON_CODE_NAMES[bit.trim()] || bit.trim())
  .join(", ");

// 模型调用角色 → 中文名。
const ROLE_NAMES = {
  decision_maker: "决策方",
  reviewer: "审核方",
  red_commander: "红方指挥",
};
export const roleName = (role) => ROLE_NAMES[role] || role || "";

// 后端错误码 / 失败类别 → 中文名。
const ERROR_NAMES = {
  model_selection_unavailable: "模型未返回有效选择",
  decision_deadline_exceeded: "决策超时",
  output_truncated: "输出被截断",
  validation: "校验未通过",
  transport: "传输失败",
  timeout: "超时",
  matching: "指派匹配失败",
  preparation: "决策准备失败",
  configuration: "配置错误",
};
export const errorName = (code) =>
  ERROR_NAMES[code] || String(code || "").replace(/^http_(\d+)$/, "HTTP $1 错误");

export function translateTriggerReason(text) {
  const raw = String(text || "");
  if (!raw) return "";
  if (TRIGGER_REASON_NAMES[raw]) return TRIGGER_REASON_NAMES[raw];
  const periodic = raw.match(/^periodic (\d+)min cycle$/);
  if (periodic) return `周期性重规划(每${periodic[1]}分钟)`;
  const retry = raw.match(/^retry after (.+)$/);
  if (retry) return `上次决策失败后重试(${retry[1]})`;
  return raw
    .split("、")
    .map((clause) => {
      const match = clause.match(/^([a-z_]+)\((.*)\)$/);
      if (match && EVENT_NAMES[match[1]]) {
        return `${EVENT_NAMES[match[1]]}(${translateReasonBits(match[2])})`;
      }
      return EVENT_NAMES[clause] || clause;
    })
    .join("、");
}
