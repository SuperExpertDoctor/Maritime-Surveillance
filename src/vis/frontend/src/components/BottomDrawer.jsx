import { useEffect, useRef, useState } from "react";
import { Activity, Bot, Clipboard, GripHorizontal, Map, Satellite, SlidersHorizontal, X } from "lucide-react";

const TABS = [
  { label: "时间线", icon: Activity },
  { label: "区域", icon: Map },
  { label: "模型日志", icon: Bot },
  { label: "参数", icon: SlidersHorizontal },
  { label: "AIS", icon: Satellite },
];
const EVENT_NAMES = {
  target_found: "发现目标",
  ship_detected: "舰船确认",
  target_lost: "目标丢失",
  uav_returned: "UAV 返航",
  uav_refueled: "加油完成",
  search_complete: "搜索完成",
  llm_decision: "模型决策",
  route_plan_failed: "航路失败",
  route_replanned: "航路重规划",
  environment_reset: "环境重置",
  mission_assignment_committed: "任务已提交",
  contact_created: "创建接触",
  probe_phase_changed: "调查阶段变化",
  type_i_assessed: "I 类研判",
  type_ii_assessed: "II 类研判",
  assessment_applied: "研判完成",
  probe_timed_out: "调查超时",
  task_failed: "任务失败",
  task_completed: "任务结束",
  uav_refueled: "加油完成",
};

export default function BottomDrawer({ frame, events = [], llmCycle, mode = "live", visible, onToggle }) {
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
        <small data-testid="drawer-context">{mode === "replay" ? "Historical replay" : "Active episode"} · {frame?.episode_id || "not provided"} · {frame?.sim_time_min ?? "-"} min</small>
        {activeTab === 0 && <TimelineTab events={events} />}
        {activeTab === 1 && <RegionTab frame={frame} />}
        {activeTab === 2 && <ModelCallsTab frame={frame} llm={llmCycle} mode={mode} />}
        {activeTab === 3 && <ParamsTab config={frame?.config_snapshot || config} error={mode === "replay" && !frame?.config_snapshot ? "Historical parameters: not provided" : configError} />}
        {activeTab === 4 && <AisTab frame={frame} />}
      </div>
    </section>
  );
}

function TimelineTab({ events }) {
  if (!events.length) return <EmptyState text="暂无任务事件" />;
  return (
    <div className="timeline-list">
      {[...events].reverse().slice(0, 120).map((event, index) => (
        <div className={`timeline-item event-${event.type}`} key={`${event.time}-${event.type}-${index}`}>
          <time>{Number(event.time || 0).toFixed(0).padStart(3, "0")} min</time>
          <i />
          <strong>{EVENT_NAMES[event.type] || event.type}</strong>
          <details><summary>{event.data?.uav_id || event.data?.ship_id || event.data?.group_id || "Details"}</summary><pre>{JSON.stringify(event.data || {}, null, 2)}</pre></details>
        </div>
      ))}
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
        <thead><tr><th>ID</th><th>类型</th><th>状态</th><th>边界</th><th>优先级</th><th>信息素</th><th>价值</th><th>完成</th><th>执行单元</th></tr></thead>
        <tbody>{rows.map((region) => (
          <tr key={`${region.displayType}-${region.id}`}>
            <td><b>{region.id}</b></td><td>{region.displayType}</td><td>{region.status || "not provided"}</td><td className="mono">[{region.bbox?.join(", ")}]</td>
            <td><span className={`priority ${region.priority || "high"}`}>{region.priority || "持续"}</span></td>
            <td>{region.avg_info == null ? "-" : Number(region.avg_info).toFixed(2)}</td><td>{region.info_value == null ? "-" : Number(region.info_value).toFixed(2)}</td>
            <td>{region.completion_pct == null ? "-" : `${Math.round(region.completion_pct)}% (${region.completion_basis || "not provided"})`}</td><td>{region.assigned_uav_id || "待分配"}</td>
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
        if (!controller.signal.aborted && data.episode_id === frame.episode_id) { setRemote(data.calls); setError(""); }
      } catch (err) { if (!controller.signal.aborted) setError(err.message); }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 3000);
    };
    void refresh();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [mode, frame?.episode_id]);
  const calls = remote || frame?.model_calls || [];
  const call = calls.find((item) => item.call_id === selected) || calls.at(-1) || llm;
  if (!call) return <EmptyState text={error || "Model calls: not provided"} />;
  const latest = calls.at(-1);
  return <div>
    {error && <small role="status">{error} · showing last frame data</small>}
    {calls.length > 0 && <label>Model call <select aria-label="Model call" value={call.call_id || ""} onChange={(event) => setSelected(event.target.value)}>
      {[...calls].reverse().map((item) => <option key={item.call_id} value={item.call_id}>{item.role} · {item.sim_time_min} min · {item.call_id}</option>)}
    </select></label>}
    <small>{mode === "replay" ? "Recorded at selected frame" : call === latest ? "Latest call in active episode" : "Historical call in active episode"}</small>
    <LLMTab llm={call} />
  </div>;
}

function LLMTab({ llm }) {
  const text = (value) => typeof value === "string" ? value : value == null ? "not provided" : JSON.stringify(value, null, 2);
  const copy = (value) => navigator.clipboard?.writeText(text(value));
  const attempts = llm.attempts || [];
  const last = attempts.at(-1) || {};
  const channels = llm.provider_channels || last.provider_channels || [];
  const reasoning = channels.filter((item) => item.kind === "external_provider_reasoning" && item.provenance === "external_api_response");
  const summaries = channels.filter((item) => item.kind === "public_provider_summary" && item.provenance === "external_api_response");
  const sections = [
    ["Decision notes / rationale", llm.decision_summary || "not provided"],
    ["External provider reasoning (think)", reasoning.length ? reasoning : "not provided"],
    ["Public provider reasoning summary", summaries.length ? summaries : llm.public_reasoning_summary || "not provided"],
    ["Response", llm.response ?? last.raw_output ?? last.response],
    ["Validation", llm.validation ?? llm.validation_errors ?? last.errors],
    ["Attempts", attempts],
  ];
  return <div className="llm-log">
    <div className="llm-log-head"><div><span className={llm.success ? "success" : "failed"}>{llm.success ? "VALID" : llm.failure_category ? "FAILED" : "PENDING / UNKNOWN"}</span><strong>{llm.model}</strong></div><small>{attempts.length} attempts · {llm.role} · {llm.provider} · thinking: {llm.thinking_mode ?? "not provided"}</small></div>
    <small>{llm.call_id} · snapshot: {llm.snapshot_id || "not provided"} · {llm.sim_time_min ?? "-"} min · {llm.failure_category}</small>
    <div className="llm-sections">{sections.map(([label, content], index) => <details key={label} open={index < 4}>
      <summary>{label}<button onClick={(event) => { event.preventDefault(); copy(content); }} aria-label={`复制 ${label}`}><Clipboard size={14} /></button></summary><pre>{text(content)}</pre>
    </details>)}</div>
  </div>;
}

function AisTab({ frame }) {
  const rows = Array.isArray(frame?.contacts)
    ? frame.contacts.map((contact) => {
      const ais = contact.latest_ais_sample || [...(contact.samples || [])].reverse().find((sample) => sample.source === "ais");
      return { id: contact.contact_id, mmsi: contact.ais_mmsi, aisPosition: ais?.position, position: contact.estimated_position, sampleCount: contact.sample_count ?? contact.samples?.length ?? 0, observedAt: ais?.observed_at_min, state: contact.vessel_class || "unknown", lifecycle: contact.state };
    })
    : (frame?.ships || []).map((ship) => ({
      id: ship.id,
      mmsi: ship.ais?.mmsi,
      aisPosition: ship.ais?.reported_position,
      position: ship.estimated_position,
      state: "historical",
    }));
  if (!rows.length) return <EmptyState text="No AIS contacts in this frame" />;
  return (
    <div className="table-wrap">
      <table className="region-table ais-table">
        <thead><tr><th>接触</th><th>MMSI</th><th>AIS 位置</th><th>估计位置</th><th>样本</th><th>AIS 时间</th><th>状态</th></tr></thead>
        <tbody>{rows.map((contact) => (
          <tr key={contact.id}>
            <td><b>{contact.id}</b></td>
            <td>{contact.mmsi || "无"}</td>
            <td className="mono">{formatPosition(contact.aisPosition)}</td>
            <td className="mono">{formatPosition(contact.position)}</td>
            <td>{contact.sampleCount ?? "-"}</td><td>{contact.observedAt == null ? "not provided" : `${contact.observedAt} min`}</td>
            <td>{contact.state === "unknown" ? "待核查" : contact.state === "type_i" ? "I 类船舶" : contact.state === "type_ii" ? "II 类船舶" : contact.state === "historical" ? "历史帧" : contact.state} · {contact.lifecycle}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function formatPosition(position) {
  return Array.isArray(position) ? position.map((value) => Number(value).toFixed(1)).join(", ") : "-";
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
