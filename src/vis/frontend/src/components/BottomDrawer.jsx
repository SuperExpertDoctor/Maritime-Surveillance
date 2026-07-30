import { useEffect, useRef, useState } from "react";
import { Activity, Bot, Clipboard, GripHorizontal, Map, SlidersHorizontal, X } from "lucide-react";

const TABS = [
  { label: "时间线", icon: Activity },
  { label: "区域", icon: Map },
  { label: "模型日志", icon: Bot },
  { label: "参数", icon: SlidersHorizontal },
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
};

export default function BottomDrawer({ frame, events = [], llmCycle, visible, onToggle }) {
  const [activeTab, setActiveTab] = useState(0);
  const [height, setHeight] = useState(260);
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState("");
  const drag = useRef(null);

  useEffect(() => {
    if (activeTab !== 3 || config || configError) return;
    fetch("/api/config")
      .then((response) => {
        if (!response.ok) throw new Error();
        return response.json();
      })
      .then(setConfig)
      .catch(() => setConfigError("参数接口不可用"));
  }, [activeTab, config, configError]);

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
        {activeTab === 0 && <TimelineTab events={events} />}
        {activeTab === 1 && <RegionTab frame={frame} />}
        {activeTab === 2 && <LLMTab llm={llmCycle} />}
        {activeTab === 3 && <ParamsTab config={config} error={configError} />}
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
          <span>{event.data?.uav_id || event.data?.ship_id || event.data?.group_id || ""}</span>
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
        <thead><tr><th>ID</th><th>类型</th><th>边界</th><th>优先级</th><th>信息素</th><th>价值</th><th>完成</th><th>执行单元</th></tr></thead>
        <tbody>{rows.map((region) => (
          <tr key={region.id}>
            <td><b>{region.id}</b></td><td>{region.displayType}</td><td className="mono">[{region.bbox?.join(", ")}]</td>
            <td><span className={`priority ${region.priority || "high"}`}>{region.priority || "持续"}</span></td>
            <td>{Number(region.avg_info || 0).toFixed(2)}</td><td>{Number(region.info_value || 0).toFixed(2)}</td>
            <td>{region.displayType === "搜索" ? `${Math.round(region.completion_pct || 0)}%` : "-"}</td><td>{region.assigned_uav_id || "待分配"}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function LLMTab({ llm }) {
  const copy = (text) => navigator.clipboard?.writeText(text || "");
  if (!llm) return <EmptyState text="等待 LongCat-2.0 首次决策" />;
  const sections = [
    ["System Prompt", llm.system_prompt],
    ["User Prompt", llm.user_prompt],
    ["Response", llm.response || llm.attempts?.at(-1)?.response],
    ["Validation", JSON.stringify(llm.validation, null, 2)],
  ];
  return (
    <div className="llm-log">
      <div className="llm-log-head"><div><span className={llm.success ? "success" : "failed"}>{llm.success ? "VALID" : "FAILED"}</span><strong>{llm.model}</strong></div><small>{llm.attempts?.length || 0} attempts</small></div>
      <div className="llm-sections">{sections.map(([label, content], index) => (
        <details key={label} open={index === 2}>
          <summary>{label}<button onClick={(event) => { event.preventDefault(); copy(content); }} aria-label={`复制 ${label}`} title="复制"><Clipboard size={14} /></button></summary>
          <pre>{content || "无内容"}</pre>
        </details>
      ))}</div>
    </div>
  );
}

function ParamsTab({ config, error }) {
  if (error) return <EmptyState text={error} />;
  if (!config) return <div className="loading-state"><span />加载参数</div>;
  return <div className="params-grid">{Object.entries(config).map(([section, values]) => (
    <section key={section}><h3>{section}</h3>{Object.entries(values).map(([key, value]) => (
      <div key={key}><span>{key}</span><b>{Array.isArray(value) ? value.join(" × ") : String(value)}</b></div>
    ))}</section>
  ))}</div>;
}

function EmptyState({ text }) {
  return <div className="tab-empty">{text}</div>;
}
