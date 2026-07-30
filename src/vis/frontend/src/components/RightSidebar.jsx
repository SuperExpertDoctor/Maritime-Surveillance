import { Bot, CircleX, Crosshair, Plane, Radar, Ship, Waypoints } from "lucide-react";
import { UAV_STATUS_COLORS } from "../renderer/colors";

const STATUS_LABELS = {
  idle: "待命",
  transit: "转场",
  searching: "搜索",
  tracking: "跟踪",
  returning: "返航",
  refueling: "加油",
};

export default function RightSidebar({ frame, onSelectUav, selectedUavId, open, onClose, lastLlmCycle }) {
  const uavs = frame?.uavs || [];
  const ships = frame?.ships || [];
  const regions = frame?.search_regions || [];
  const tracks = frame?.track_regions || [];
  const info = frame?.info_matrix || [];
  let scanned = 0;
  let total = 0;
  const situations = { white: 0, gray: 0, black: 0 };
  info.forEach((column) => column.forEach((value) => {
    total += 1;
    if (value > 0) scanned += 1;
    if (value > 0.7) situations.white += 1;
    else if (value >= 0.2) situations.gray += 1;
    else situations.black += 1;
  }));
  const coverage = Number.isFinite(frame?.coverage_pct)
    ? frame.coverage_pct
    : (total ? scanned / total * 100 : 0);
  const selected = uavs.find((uav) => uav.id === selectedUavId);

  return (
    <aside className={`sidebar ${open ? "open" : ""}`} aria-label="编队状态">
      <div className="sidebar-header">
        <div><span className="eyebrow">MISSION STATE</span><strong>编队态势</strong></div>
        <button className="icon-btn mobile-only" onClick={onClose} aria-label="关闭编队状态" title="关闭"><CircleX size={17} /></button>
      </div>
      {!frame ? (
        <div className="sidebar-empty"><Radar size={24} /><span>等待任务数据</span></div>
      ) : (
        <>
          <section className="sidebar-section overview-grid" aria-label="任务概览">
            <Metric label="仿真时间" value={frame.timestamp || "--:--:--"} />
            <Metric label="决策周期" value={`#${frame.cycle ?? 0}`} />
            <Metric label="海域覆盖" value={`${coverage.toFixed(1)}%`} emphasized />
            <Metric label="目标发现" value={`${ships.filter((ship) => ship.is_detected).length}/${ships.length}`} />
          </section>

          <section className="sidebar-section">
            <div className="section-heading"><span>信息态势</span><small>{frame.searchable_cells || total || 900} CELLS</small></div>
            <div className="situation-strip">
              <Situation label="白" value={situations.white} tone="white" />
              <Situation label="灰" value={situations.gray} tone="gray" />
              <Situation label="黑" value={situations.black || (total ? 0 : 900)} tone="black" />
            </div>
            <div className="coverage-track"><i style={{ width: `${coverage}%` }} /></div>
          </section>

          <section className="sidebar-section uav-section">
            <div className="section-heading"><span>飞行单元</span><small>{uavs.filter((uav) => uav.status !== "idle").length} ACTIVE</small></div>
            <div className="uav-list">
              {uavs.map((uav) => {
                const color = UAV_STATUS_COLORS[uav.status] || "#94A3B8";
                const fuel = Math.max(0, Math.min(100, (uav.fuel_remaining_pct ?? 0) * 100));
                const isSelected = uav.id === selectedUavId;
                return (
                  <button key={uav.id} className={`uav-row ${isSelected ? "selected" : ""}`} onClick={() => onSelectUav?.(isSelected ? null : uav.id)} aria-pressed={isSelected}>
                    <span className="uav-plane" style={{ color }}><Plane size={16} /></span>
                    <span className="uav-copy"><strong>{uav.id}</strong><small>{STATUS_LABELS[uav.status] || uav.status} · {uav.assigned_region_id || "无任务"}</small></span>
                    <span className="fuel-gauge" style={{ "--fuel": `${fuel}%`, "--fuel-color": color }}><b>{Math.round(fuel)}</b></span>
                  </button>
                );
              })}
            </div>
          </section>

          {selected && (
            <section className="sidebar-section selected-detail">
              <div className="section-heading"><span>{selected.id} 详情</span><small>{Math.round(selected.heading_deg || 0)}°</small></div>
              <dl>
                <div><dt>传感器</dt><dd>{selected.sensor_mode?.toUpperCase() || "OFF"}</dd></div>
                <div><dt>坐标</dt><dd>{selected.position.map((value) => Number(value).toFixed(1)).join(", ")}</dd></div>
                <div><dt>剩余航程</dt><dd>{Math.round(selected.remaining_range_km || 0)} km</dd></div>
                <div><dt>目标群</dt><dd>{selected.target_group_id || "-"}</dd></div>
              </dl>
            </section>
          )}

          <section className="sidebar-section compact-stats">
            <div><Waypoints size={15} /><span>搜索区</span><b>{regions.length}</b></div>
            <div><Crosshair size={15} /><span>跟踪区</span><b>{tracks.length}</b></div>
            <div><Ship size={15} /><span>标记点</span><b>{frame.markers?.length || 0}</b></div>
          </section>

          <section className="sidebar-section llm-summary">
            <div className="section-heading"><span><Bot size={15} />模型决策</span><small>{lastLlmCycle?.model || "LONGCAT-2.0"}</small></div>
            {lastLlmCycle ? (
              <div className="llm-status-row">
                <span className={lastLlmCycle.success ? "success" : "failed"}>{lastLlmCycle.success ? "校验通过" : "决策失败"}</span>
                <b>{lastLlmCycle.attempts?.length || 0} 次请求</b>
              </div>
            ) : <p>等待首次重量触发</p>}
          </section>
        </>
      )}
    </aside>
  );
}

function Metric({ label, value, emphasized }) {
  return <div className={emphasized ? "metric emphasized" : "metric"}><span>{label}</span><strong>{value}</strong></div>;
}

function Situation({ label, value, tone }) {
  return <div className={`situation ${tone}`}><span>{label}态</span><strong>{value}</strong></div>;
}
