import { useEffect, useRef, useState } from "react";
import { Clipboard, GripHorizontal, ListChecks, Map as MapIcon, Satellite, ScrollText, SlidersHorizontal, X } from "lucide-react";
import VesselStatusTab from "./VesselStatusTab";
import { errorName, regionAnchor, regionDisplayName, regionNameById, roleName, translateTriggerReason } from "../renderer/regionName";

const TABS = [
  { label: "决策", icon: ListChecks },
  { label: "区域", icon: MapIcon },
  { label: "日志", icon: ScrollText },
  { label: "参数", icon: SlidersHorizontal },
  { label: "船舶状态", icon: Satellite },
];
const TRIGGERS = {
  event: "事件被动触发",
  periodic: "周期性主动触发",
  initial: "首次部署（主动）",
  retry: "失败重试（主动）",
};
const SOURCE_NAMES = { llm: "模型", algorithm: "算法", runtime: "运行", system: "系统" };

const REGION_STATUS_NAMES = {
  active: "进行中",
  pending: "待分配",
  completed: "已完成",
  stale: "已过期",
  released: "已释放",
  cancelled: "已取消",
};
const PRIORITY_NAMES = { high: "高", medium: "中", low: "低" };
const COMPLETION_BASIS_NAMES = {
  task_sar: "任务SAR",
  legacy_observation: "观测历史",
};

const LOG_STATUSES = {
  started: "开始调用",
  retry: "重试",
  success: "正常完成",
  failed: "调用失败",
  timeout: "超时",
  running: "运行中",
  paused_model: "模型暂停",
  completed: "运行结束",
  mission_assignment_committed: "任务分配已提交",
  mission_selection_failed: "任务选择失败",
  decision_failed: "决策失败",
  mission_model_failure: "模型决策失败",
  mission_model_paused: "模型暂停",
  mission_model_retry_succeeded: "重试成功",
  mission_model_retry_failed: "重试失败",
  route_plan_failed: "航路规划失败",
  task_failed: "任务失败",
  task_completed: "任务完成",
  uav_returned: "UAV 返航",
  target_found: "发现目标",
  search_complete: "搜索完成",
  assessment_applied: "研判完成",
  probe_timed_out: "调查超时",
};

export default function BottomDrawer({ frame, llmCycle, mode = "live", decisions = [], logs = [], logError = "", onSelectDecision,
  editingAllowed = false, vesselCommandBusy = false, vesselCommandStatus, onSetVesselAis, onDeleteVessel, visible, onToggle }) {
  const [activeTab, setActiveTab] = useState(0);
  const [height, setHeight] = useState(220);
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState("");
  const drag = useRef(null);

  useEffect(() => {
    setConfig(null);
    setConfigError("");
    if (activeTab !== 3 || !visible || frame?.config_snapshot || mode === "replay") return;
    const controller = new AbortController();
    fetch("/api/config", { signal: controller.signal })
      .then((response) => { if (!response.ok) throw new Error(); return response.json(); })
      .then((data) => { if (!controller.signal.aborted) setConfig(data); })
      .catch(() => { if (!controller.signal.aborted) setConfigError("参数接口不可用"); });
    return () => controller.abort();
  }, [activeTab, visible, mode, frame?.episode_id, frame?.reset_generation, frame?.config_snapshot]);

  useEffect(() => {
    const move = (event) => {
      if (!drag.current) return;
      setHeight(Math.max(180, Math.min(window.innerHeight * 0.58, drag.current.height + drag.current.y - event.clientY)));
    };
    const up = () => { drag.current = null; };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, []);

  if (!visible) return null;
  return (
    <section className="bottom-drawer" style={{ height }} aria-label="任务详情">
      <button className="drawer-grip" onPointerDown={(event) => { drag.current = { y: event.clientY, height }; }} aria-label="调整面板高度" title="拖动调整高度"><GripHorizontal size={20} /></button>
      <div className="drawer-tabs" role="tablist">
        {TABS.map(({ label, icon: Icon }, index) => (
          <button key={label} role="tab" aria-selected={index === activeTab} className={index === activeTab ? "active" : ""} onClick={() => setActiveTab(index)}>
            <Icon size={15} />{label}
          </button>
        ))}
        <button className="drawer-close" onClick={onToggle} aria-label="关闭任务详情" title="关闭"><X size={16} /></button>
      </div>
      <div className="drawer-content">
        <small data-testid="drawer-context">{mode === "replay" ? "历史回放" : "当前回合"} · {frame?.episode_id || "-"} · {frame?.sim_time_min ?? "-"} min</small>
        {activeTab === 0 && <DecisionTab decisions={decisions} frame={frame} onSelectDecision={onSelectDecision} />}
        {activeTab === 1 && <RegionTab frame={frame} />}
        {activeTab === 2 && <LogTab logs={logs} error={logError} frame={frame} llm={llmCycle} mode={mode} />}
        {activeTab === 3 && <ParamsTab config={frame?.config_snapshot || config} error={mode === "replay" && !frame?.config_snapshot ? "该回放未记录参数快照" : configError} />}
        {activeTab === 4 && <VesselStatusTab vessels={frame?.scenario_vessels || []} mode={mode}
          editingAllowed={editingAllowed} commandBusy={vesselCommandBusy}
          commandStatus={vesselCommandStatus} onSetAis={onSetVesselAis} onDelete={onDeleteVessel} />}
      </div>
    </section>
  );
}

function DecisionTab({ decisions, frame, onSelectDecision }) {
  const regionName = regionNameById([
    ...(frame?.search_regions || []),
    ...(frame?.track_regions || []),
  ]);
  return (
    <div className="decision-table-wrap">
      <table className="decision-table">
        <thead><tr><th>决策时间</th><th>触发类型</th><th>决策原因</th><th>决策内容</th><th>参与调度的 UAV</th></tr></thead>
        <tbody>{decisions.map((event, index) => {
          const data = event.data || {};
          const assigned = data.assignments || [];
          const selected = (data.selected_task_ids || []).map(regionName).join("，");
          const errorText = (data.errors || []).filter(Boolean).map(errorName).join("；")
            || (data.failure_category ? `失败类别：${errorName(data.failure_category)}` : "");
          const outcome = data.status === "committed" && assigned.length
            ? assigned.map((item) => `${regionName(item.task_id) || item.task_id} → ${item.uav_id}`).join("；")
            : `${selected ? `已选择 ${selected} · ` : ""}${data.status === "rejected" ? "分配未提交" : "未完成分配"}${errorText ? `：${errorText}` : ""}`;
          // 决策原因按触发来源取真实内容：事件触发写触发事件的描述，
          // 主动触发写模型的 think 内容；均缺失时如实说明失败情况，绝不放占位词。
          const isEvent = data.trigger_source === "event";
          const reason = (isEvent ? translateTriggerReason(data.trigger_reason) : data.reason_content)
            || (isEvent ? data.reason_content : translateTriggerReason(data.trigger_reason))
            || (errorText ? `模型未产出有效决策：${errorText}` : "")
            || TRIGGERS[data.trigger_source] || data.trigger_source || "未知来源";
          const triggerNote = !isEvent && data.reason_content && data.trigger_reason
            && data.trigger_reason !== data.reason_content ? `触发 · ${translateTriggerReason(data.trigger_reason)}` : "";
          return (
            <tr key={event.event_id || `${event.time}-${index}`} onClick={() => onSelectDecision?.(event)}>
              <td className="decision-time">{Number(data.time_min ?? event.time ?? 0).toFixed(0).padStart(3, "0")} min</td>
              <td><span className={`trigger-label trigger-${data.trigger_source}`}>{TRIGGERS[data.trigger_source] || data.trigger_source || "未知"}</span></td>
              <td className="decision-reason">
                <details onClick={(click) => click.stopPropagation()}><summary>{reason}</summary><div>{reason}</div></details>
                {triggerNote && <small className="muted">{triggerNote}</small>}
              </td>
              <td><span className={data.status === "committed" ? "decision-outcome" : "decision-outcome failed"}>{outcome}</span></td>
              <td className="decision-uavs">{(data.involved_uav_ids || []).join("、") || "无"}</td>
            </tr>
          );
        })}</tbody>
      </table>
      {!decisions.length && <EmptyState text="暂无 LLM 决策记录" />}
    </div>
  );
}

function LogTab({ logs, error, frame, llm, mode }) {
  const regionName = regionNameById([
    ...(frame?.search_regions || []),
    ...(frame?.track_regions || []),
  ]);
  return (
    <div className="runtime-log-list" role="log" aria-label="算法运行日志">
      {error && <div className="runtime-log-error" role="status">{error}</div>}
      {logs.map((item) => (
        <div className={`runtime-log-row level-${item.level || "info"}`} key={item.id}>
          <time>{Number(item.sim_time_min ?? 0).toFixed(0).padStart(3, "0")} min</time>
          <span className="log-source">{roleName(item.role) || SOURCE_NAMES[item.source] || item.source || "运行"}</span>
          <strong>{LOG_STATUSES[item.status] || item.status}{item.attempt != null && ` · 第 ${item.attempt} 次`}</strong>
          <span className="log-entity">{item.uav_id || regionName(item.task_id) || errorName(item.failure_category) || ""}</span>
          {item.call_id && <span className="log-call-id" title={item.call_id}>{item.call_id}</span>}
        </div>
      ))}
      {!logs.length && !error && <EmptyState text="暂无运行日志" />}
      <details className="model-call-details"><summary>模型调用详情</summary><ModelCallsTab frame={frame} llm={llm} mode={mode} /></details>
    </div>
  );
}

function RegionTab({ frame }) {
  const rows = [
    ...(frame?.search_regions || []).map((region) => ({ ...region, displayType: "搜索" })),
    ...(frame?.track_regions || []).map((region) => ({ ...region, displayType: "跟踪" })),
  ];
  if (!rows.length) return <EmptyState text="尚未划分任务区域" />;
  return (
    <div className="table-wrap">
      <table className="region-table">
        <thead><tr><th>区域</th><th>类型</th><th>状态</th><th>边界</th><th>优先级</th><th>信息素</th><th>价值</th><th>完成</th><th>执行单元</th></tr></thead>
        <tbody>{rows.map((region) => (
          <tr key={`${region.displayType}-${region.id}`}>
            <td><b>{regionDisplayName(region.id, regionAnchor(region))}</b></td><td>{region.displayType}</td><td>{REGION_STATUS_NAMES[region.status] || region.status || "-"}</td><td className="mono">[{region.bbox?.join(", ")}]</td>
            <td><span className={`priority ${region.priority || "high"}`}>{PRIORITY_NAMES[region.priority] || region.priority || "持续"}</span></td>
            <td>{region.avg_info == null ? "-" : Number(region.avg_info).toFixed(2)}</td><td>{region.info_value == null ? "-" : Number(region.info_value).toFixed(2)}</td>
            <td>{region.completion_pct == null ? "-" : `${Math.round(region.completion_pct)}%${region.completion_basis ? ` (${COMPLETION_BASIS_NAMES[region.completion_basis] || region.completion_basis})` : ""}`}</td><td>{region.assigned_uav_id || "待分配"}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function ModelCallsTab({ frame, llm, mode }) {
  const [selected, setSelected] = useState("");
  const [remote, setRemote] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setRemote(null);
    setError("");
    if (mode !== "live" || !frame?.episode_id) return;
    const controller = new AbortController();
    let timer;
    const refresh = async () => {
      try {
        const response = await fetch(`/api/model-calls?episode_id=${encodeURIComponent(frame.episode_id)}`, { signal: controller.signal });
        if (!response.ok) throw new Error(`Model calls HTTP ${response.status}`);
        const data = await response.json();
        if (!controller.signal.aborted && data.episode_id === frame.episode_id) {
          setRemote({ episodeId: frame.episode_id, generation: frame.reset_generation, calls: data.calls || [] });
          setError("");
        }
      } catch (err) { if (!controller.signal.aborted) setError(err.message); }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 3000);
    };
    void refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [mode, frame?.episode_id, frame?.reset_generation]);
  // HTTP polling can finish after a newer WebSocket frame. Merge their logs
  // instead of letting an older response hide calls or undo completed results.
  const remoteCalls = mode === "live" && remote?.episodeId === frame?.episode_id
    && remote?.generation === frame?.reset_generation ? remote?.calls || [] : [];
  const byId = new Map(remoteCalls.map(call => [call.call_id, call]));
  for (const call of frame?.model_calls || []) {
    const prior = byId.get(call.call_id);
    const terminal = item => item?.success === true || Boolean(item?.failure_category);
    if (!prior || terminal(call) || (!terminal(prior)
      && (call.attempts?.length || 0) >= (prior.attempts?.length || 0))) {
      byId.set(call.call_id, call);
    }
  }
  const calls = [...byId.values()].sort((a, b) => (a.sim_time_min || 0) - (b.sim_time_min || 0));
  const call = calls.find((item) => item.call_id === selected) || calls.at(-1) || llm;
  if (!call) return <EmptyState text={error || "本时段没有模型调用记录"} />;
  const latest = calls.at(-1);
  return <div>
    {error && <small role="status">{error} · 显示最近一次可用数据</small>}
    {calls.length > 0 && <label>模型调用 <select aria-label="模型调用" value={call.call_id || ""} onChange={(event) => setSelected(event.target.value)}>
      {[...calls].reverse().map((item) => <option key={item.call_id} value={item.call_id}>{roleName(item.role)} · {item.sim_time_min} min · {item.call_id}</option>)}
    </select></label>}
    <small>{mode === "replay" ? "回放·所选帧的记录" : call === latest ? "当前回合最新调用" : "当前回合历史调用"}</small>
    <LLMTab llm={call} />
  </div>;
}

function LLMTab({ llm }) {
  const text = (value) => typeof value === "string" ? value : value == null ? "无返回内容" : JSON.stringify(value, null, 2);
  const copy = (value) => navigator.clipboard?.writeText(text(value));
  const attempts = llm.attempts || [];
  const last = attempts.at(-1) || {};
  const channels = llm.provider_channels || last.provider_channels || [];
  const reasoning = channels.filter((item) => item.kind === "external_provider_reasoning" && item.provenance === "external_api_response");
  const summaries = channels.filter((item) => item.kind === "public_provider_summary" && item.provenance === "external_api_response");
  const sections = [
    ["决策说明 / 理由", llm.decision_summary || "无返回内容"],
    ["外部模型推理（think）", reasoning.length ? reasoning : "无返回内容"],
    ["公开推理摘要", summaries.length ? summaries : llm.public_reasoning_summary || "无返回内容"],
    ["模型响应", llm.response ?? last.raw_output ?? last.response],
    ["校验结果", llm.validation ?? llm.validation_errors ?? last.errors],
    ["尝试记录", attempts],
  ];
  return <div className="llm-log">
    <div className="llm-log-head"><div><span className={llm.success ? "success" : "failed"}>{llm.success ? "有效" : llm.failure_category ? "失败" : "待定"}</span><strong>{llm.model}</strong></div><small>{attempts.length} 次尝试 · {roleName(llm.role)} · {llm.provider} · 思考：{llm.thinking_mode ?? "-"}</small></div>
    <small>{llm.call_id} · 快照: {llm.snapshot_id || "-"} · {llm.sim_time_min ?? "-"} min · {llm.failure_category ? errorName(llm.failure_category) : ""}</small>
    <div className="llm-sections">{sections.map(([label, content], index) => <details key={label} open={index < 4}>
      <summary>{label}<button onClick={(event) => { event.preventDefault(); copy(content); }} aria-label={`复制 ${label}`}><Clipboard size={14} /></button></summary><pre>{text(content)}</pre>
    </details>)}</div>
  </div>;
}

function ParamsTab({ config, error }) {
  if (error) return <EmptyState text={error} />;
  if (!config) return <div className="loading-state"><span />加载参数</div>;
  return <div className="params-grid">{Object.entries(config).map(([section, values]) => (
    <section key={section}><h3>{section}</h3>{Object.entries(values).map(([key, value]) => (
      <div key={key}><span>{key}</span><b>{formatParamValue(value)}</b></div>
    ))}</section>
  ))}</div>;
}

function formatParamValue(value) {
  if (Array.isArray(value)) return value.join(" × ");
  if (value && typeof value === "object") {
    return Object.entries(value)
      .map(([key, child]) => `${key}: ${formatParamValue(child)}`)
      .join("; ");
  }
  return String(value);
}

function EmptyState({ text }) {
  return <div className="tab-empty">{text}</div>;
}
