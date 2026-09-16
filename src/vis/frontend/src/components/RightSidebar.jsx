import { Bot, CircleX, Crosshair, MousePointer2, Plane, Radio, RadioTower, Radar, Ship, Trash2, Waypoints } from "lucide-react";
import { UAV_STATUS_COLORS } from "../renderer/colors";
import ContactPanel from "./ContactPanel";
import IntentPanel from "./IntentPanel";

const STATUS_LABELS = {
  idle: "待命",
  transit: "转场",
  searching: "搜索",
  tracking: "跟踪",
  returning: "返航",
  refueling: "加油",
};

const VESSEL_CLASS_LABELS = {
  type_i: "I 类",
  type_ii: "II 类",
  unknown: "未知",
};

const SURVEILLANCE_STAGE_LABELS = {
  undetected: "未发现",
  detected: "已发现",
  probing: "递进侦察",
  tracking: "持续跟踪",
};

export default function RightSidebar({
  frame,
  onSelectUav,
  selectedUavId,
  open,
  onClose,
  lastLlmCycle,
  readOnly,
  selection,
  onClearSelection,
  selectedContactId,
  onSelectContact,
  editingAllowed,
  vesselPlacement,
  onSelectVesselType,
  onCancelVesselPlacement,
  selectedScenarioVesselId,
  onSelectScenarioVessel,
  onDeleteVessel,
  onSetVesselAis,
  vesselCommandStatus,
}) {
  const uavs = frame?.uavs || [];
  const ships = frame?.ships || [];
  const contacts = frame?.contacts || [];
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
  const scenarioVessels = frame?.scenario_vessels || [];
  const selectedScenarioVessel = scenarioVessels.find(
    (vessel) => vessel.scenario_entity_id === selectedScenarioVesselId,
  );
  const vesselCommandBusy = vesselCommandStatus?.status === "queued";
  const canEditVessels = Boolean(editingAllowed) && !vesselCommandBusy;

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
            <Metric label="观测接触" value={contacts.length || ships.filter((ship) => ship.is_detected).length} />
          </section>

          <section className="sidebar-section vessel-editor" aria-label="初始化船舶编辑">
            <div className="section-heading">
              <span><Ship size={15} />场景船舶</span>
              <small>{frame.actual_vessel_count ?? ships.length}/{frame.initial_vessel_count ?? ships.length}</small>
            </div>
            <div className="vessel-palette" role="group" aria-label="船舶组件库">
              <button
                type="button"
                className={vesselPlacement === "type_i" ? "vessel-tool active" : "vessel-tool"}
                aria-pressed={vesselPlacement === "type_i"}
                aria-label="I 类船舶"
                draggable={canEditVessels}
                disabled={!canEditVessels}
                onClick={() => onSelectVesselType?.(vesselPlacement === "type_i" ? null : "type_i")}
                onDragStart={(event) => {
                  event.dataTransfer.setData("application/x-vessel-class", "type_i");
                  onSelectVesselType?.("type_i");
                }}
                title="选择后点击地图放置 I 类船舶"
              >
                <Ship size={17} /><span>I 类船舶</span>
              </button>
              <button
                type="button"
                className={vesselPlacement === "type_ii" ? "vessel-tool active" : "vessel-tool"}
                aria-pressed={vesselPlacement === "type_ii"}
                aria-label="II 类船舶"
                draggable={canEditVessels}
                disabled={!canEditVessels}
                onClick={() => onSelectVesselType?.(vesselPlacement === "type_ii" ? null : "type_ii")}
                onDragStart={(event) => {
                  event.dataTransfer.setData("application/x-vessel-class", "type_ii");
                  onSelectVesselType?.("type_ii");
                }}
                title="选择后点击地图放置 II 类船舶"
              >
                <Radar size={17} /><span>II 类船舶</span>
              </button>
            </div>
            {vesselPlacement && canEditVessels && (
              <div className="placement-status">
                <MousePointer2 size={14} />
                <span>地图上选择位置</span>
                <button type="button" className="compact-icon icon-btn" onClick={onCancelVesselPlacement} aria-label="取消放置船舶" title="取消放置船舶"><CircleX size={14} /></button>
              </div>
            )}
            {scenarioVessels.length > 0 && (
              <div className="scenario-vessel-list" aria-label="场景船舶列表">
                {scenarioVessels.map((vessel) => (
                  <button
                    type="button"
                    key={vessel.scenario_entity_id}
                    className={selectedScenarioVesselId === vessel.scenario_entity_id ? "scenario-vessel-row selected" : "scenario-vessel-row"}
                    aria-pressed={selectedScenarioVesselId === vessel.scenario_entity_id}
                    onClick={() => onSelectScenarioVessel?.(
                      selectedScenarioVesselId === vessel.scenario_entity_id ? null : vessel.scenario_entity_id,
                    )}
                  >
                    <span><Ship size={13} />{vessel.scenario_entity_id}</span>
                    <small>{VESSEL_CLASS_LABELS[vessel.vessel_class] || "未知"} · {vessel.ais_enabled ? "AIS 开启" : "AIS 关闭"}</small>
                  </button>
                ))}
              </div>
            )}
            {selectedScenarioVessel && canEditVessels && (
              <button type="button" className="delete-vessel-action" onClick={onDeleteVessel} aria-label="删除选中船舶">
                <Trash2 size={14} />删除选中船舶
              </button>
            )}
            {!editingAllowed && <p className="editor-note">当前场景只读；回放和已结束任务不可编辑</p>}
            {vesselCommandBusy && <p className="editor-note">命令处理中，等待权威 frame 更新</p>}
            {vesselCommandStatus && (
              <div className={`vessel-command-status ${vesselCommandStatus.status}`} role="status">
                <span>{vesselCommandStatus.message}</span>
                {vesselCommandStatus.errorCode && <small>{vesselCommandStatus.errorCode}</small>}
              </div>
            )}
          </section>

          {selectedScenarioVessel && (
            <section className="sidebar-section selected-vessel-detail" aria-label="选中船舶详情">
              <div className="section-heading">
                <span><Ship size={15} />{selectedScenarioVessel.scenario_entity_id}</span>
                <small>REV {selectedScenarioVessel.revision}</small>
              </div>
              <dl className="vessel-detail-grid">
                <div><dt>类别</dt><dd>{VESSEL_CLASS_LABELS[selectedScenarioVessel.vessel_class] || "未知"}</dd></div>
                <div><dt>侦察阶段</dt><dd>{SURVEILLANCE_STAGE_LABELS[selectedScenarioVessel.surveillance_stage] || "未发现"}</dd></div>
                <div><dt>位置</dt><dd className="mono">{selectedScenarioVessel.position?.map((value) => Number(value).toFixed(1)).join(", ") || "-"}</dd></div>
                <div><dt>AIS</dt><dd>{selectedScenarioVessel.ais_enabled ? "开启" : "关闭"}</dd></div>
              </dl>
              <div className="ais-control" role="group" aria-label="AIS 开关">
                <button
                  type="button"
                  className={selectedScenarioVessel.ais_enabled ? "active" : ""}
                  aria-label="开启 AIS"
                  aria-pressed={selectedScenarioVessel.ais_enabled}
                  disabled={!selectedScenarioVessel.ais_controllable || !canEditVessels || selectedScenarioVessel.ais_enabled}
                  onClick={() => onSetVesselAis?.(true)}
                >
                  <RadioTower size={14} />开启
                </button>
                <button
                  type="button"
                  className={!selectedScenarioVessel.ais_enabled ? "active" : ""}
                  aria-label="关闭 AIS"
                  aria-pressed={!selectedScenarioVessel.ais_enabled}
                  disabled={!selectedScenarioVessel.ais_controllable || !canEditVessels || !selectedScenarioVessel.ais_enabled}
                  onClick={() => onSetVesselAis?.(false)}
                >
                  <Radio size={14} />关闭
                </button>
              </div>
              {!selectedScenarioVessel.ais_controllable && <p className="editor-note">I 类船舶 AIS 固定开启</p>}
            </section>
          )}

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

          <IntentPanel
            frame={frame}
            readOnly={readOnly}
            selection={selection}
            onClearSelection={onClearSelection}
          />
          <ContactPanel
            frame={frame}
            selectedContactId={selectedContactId}
            onSelectContact={onSelectContact}
          />

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
