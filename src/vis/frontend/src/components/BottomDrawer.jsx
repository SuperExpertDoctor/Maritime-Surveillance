import { useState, useEffect } from "react";

const TABS = ["时间线", "区域详情", "LLM日志", "参数"];

const EVENT_LABELS = {
  target_found: "🎯 发现目标",
  uav_returned: "🛬 UAV 返航",
  uav_refueled: "⛽ UAV 加油完成",
  search_complete: "✅ 搜索完成",
};

export default function BottomDrawer({ frame, visible, onToggle }) {
  const [activeTab, setActiveTab] = useState(0);
  const [config, setConfig] = useState(null);

  // 参数面板加载配置
  useEffect(() => {
    if (activeTab === 3 && !config) {
      fetch("/api/config")
        .then((r) => r.json())
        .then(setConfig)
        .catch(() => {});
    }
  }, [activeTab, config]);

  if (!visible) return null;

  return (
    <div className="bottom-drawer" style={{ height: "35vh" }}>
      <div className="drawer-tabs">
        {TABS.map((t, i) => (
          <div
            key={t}
            className={`drawer-tab ${i === activeTab ? "active" : ""}`}
            onClick={() => setActiveTab(i)}
          >
            {t}
          </div>
        ))}
      </div>
      <div className="drawer-content">
        {activeTab === 0 && <TimelineTab frame={frame} />}
        {activeTab === 1 && <RegionTab frame={frame} />}
        {activeTab === 2 && <LLMTab frame={frame} />}
        {activeTab === 3 && <ParamsTab config={config} />}
      </div>
    </div>
  );
}

// ── Tab 1: 时间线 ──
function TimelineTab({ frame }) {
  const events = frame?.events || [];
  if (events.length === 0) {
    return <div className="tab-empty">暂无事件</div>;
  }
  const recent = [...events].reverse().slice(0, 50);
  return (
    <div className="timeline-list">
      {recent.map((evt, i) => {
        const label = EVENT_LABELS[evt.type] || evt.type;
        const t = typeof evt.time === "number" ? evt.time.toFixed(0) : evt.time;
        return (
          <div key={i} className="timeline-item">
            <span className="tl-time">t={t}min</span>
            <span className="tl-type">{label}</span>
            {evt.data?.ship_id && <span className="tl-detail">{evt.data.ship_id}</span>}
            {evt.data?.uav_id && <span className="tl-detail">{evt.data.uav_id}</span>}
          </div>
        );
      })}
    </div>
  );
}

// ── Tab 2: 区域详情 ──
function RegionTab({ frame }) {
  const searchRegions = frame?.search_regions || [];
  const trackRegions = frame?.track_regions || [];

  return (
    <div>
      {searchRegions.length === 0 && trackRegions.length === 0 && (
        <div className="tab-empty">暂无区域</div>
      )}
      {searchRegions.map((r) => (
        <div key={r.id} className="region-card search">
          <div className="rc-header">
            <span className="rc-id">{r.id}</span>
            <span className={`rc-priority ${r.priority}`}>{r.priority}</span>
            <span className="rc-type">搜索</span>
          </div>
          <div className="rc-body">
            bbox: [{r.bbox?.join(",")}]
            {r.completion_pct != null && ` | 完成 ${Math.round(r.completion_pct)}%`}
            {r.assigned_uav_id && ` | ${r.assigned_uav_id}`}
          </div>
        </div>
      ))}
      {trackRegions.map((r) => (
        <div key={r.id} className="region-card track">
          <div className="rc-header">
            <span className="rc-id">{r.id}</span>
            <span className="rc-type">跟踪</span>
          </div>
          <div className="rc-body">
            bbox: [{r.bbox?.join(",")}]
            {r.assigned_uav_id && ` | ${r.assigned_uav_id}`}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Tab 3: LLM 日志 ──
function LLMTab({ frame }) {
  const llm = frame?.llm_cycle;
  if (!llm) {
    return <div className="tab-empty">本帧无 LLM 决策</div>;
  }
  return (
    <div>
      <div className="llm-cycle">LLM 周期 #{llm.cycle}</div>
      <pre className="llm-json">{JSON.stringify(llm, null, 2)}</pre>
    </div>
  );
}

// ── Tab 4: 参数 ──
function ParamsTab({ config }) {
  if (!config) {
    return <div className="tab-empty">加载中...</div>;
  }
  return (
    <div className="params-grid">
      {Object.entries(config).map(([section, items]) => (
        <div key={section} className="param-section">
          <div className="param-section-title">{section}</div>
          {Object.entries(items).map(([k, v]) => (
            <div key={k} className="param-row">
              <span className="param-key">{k}</span>
              <span className="param-val">
                {Array.isArray(v) ? v.join(", ") : String(v)}
              </span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
