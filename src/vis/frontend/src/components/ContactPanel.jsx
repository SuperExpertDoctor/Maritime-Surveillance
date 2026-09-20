import { useMemo } from "react";
import { CircleDot, Eye, FileSearch, Radio, ShieldQuestion } from "lucide-react";
import { uavDisplayState } from "../renderer/displayState";

const VESSEL_CLASS_LABELS = {
  unknown: "待核查",
  type_i: "I 类船舶",
  type_ii: "II 类船舶",
};

const STATE_LABELS = {
  pending: "待处理",
  queued: "排队",
  approaching: "接近中",
  observing: "观察中",
  tracking: "跟踪中",
  cleared: "已解除",
  lost: "暂时丢失",
  departed: "已离场",
};

function position(values) {
  return Array.isArray(values) ? values.map((value) => Number(value).toFixed(1)).join(", ") : "-";
}

function sourceLabel(source) {
  return source === "ais" ? "AIS" : source === "sar" ? "SAR" : "EO";
}

export default function ContactPanel({ frame, selectedContactId, onSelectContact }) {
  const contacts = frame?.contacts || [];
  const uavs = frame?.uavs || [];
  const selected = contacts.find((contact) => contact.contact_id === selectedContactId) || contacts[0];
  const assignedUav = selected?.assigned_uav_id
    ? uavs.find((uav) => uav.id === selected.assigned_uav_id)
    : null;
  const samples = useMemo(() => (selected?.samples || []).slice(-8).reverse(), [selected]);
  const assessment = selected?.last_assessment;

  return (
    <section className="sidebar-section mission-panel contact-panel" aria-label="观测接触">
      <div className="section-heading">
        <span><FileSearch size={15} />观测接触</span>
        <small>{contacts.length} CONTACTS</small>
      </div>
      {!contacts.length ? (
        <div className="panel-empty"><ShieldQuestion size={15} />暂无观测接触</div>
      ) : (
        <>
          <div className="contact-list">
            {contacts.map((contact) => (
              (() => {
                const assigned = contact.assigned_uav_id
                  ? uavs.find((uav) => uav.id === contact.assigned_uav_id)
                  : null;
                return (
              <button
                type="button"
                key={contact.contact_id}
                className={`contact-row ${contact.contact_id === selected?.contact_id ? "selected" : ""}`}
                onClick={() => onSelectContact?.(contact.contact_id)}
                aria-pressed={contact.contact_id === selected?.contact_id}
              >
                <span className={`contact-state-dot vessel-class-${contact.vessel_class || "unknown"}`}><CircleDot size={15} /></span>
                <span className="contact-copy"><strong>{contact.contact_id}</strong><small>{VESSEL_CLASS_LABELS[contact.vessel_class] || "待核查"} · {STATE_LABELS[contact.state] || contact.state}{assigned ? ` · ${uavDisplayState(assigned).label}` : ""}</small></span>
                <span className="contact-seen">{contact.samples?.length || 0}</span>
              </button>
                );
              })()
            ))}
          </div>
          {selected && (
            <div className="contact-detail">
              <div className="contact-detail-head"><strong>{selected.contact_id}</strong><span className={`vessel-class-badge vessel-class-${selected.vessel_class || "unknown"}`}>{VESSEL_CLASS_LABELS[selected.vessel_class] || "待核查"}</span></div>
              <dl>
                <div><dt>阶段</dt><dd>{STATE_LABELS[selected.state] || selected.state || "-"}</dd></div>
                <div><dt>AIS 来源</dt><dd>{selected.ais_mmsi || "无"}</dd></div>
                <div><dt>位置</dt><dd className="mono">{position(selected.estimated_position)}</dd></div>
                <div><dt>观测次数</dt><dd>{selected.samples?.length || 0}</dd></div>
                <div><dt>任务阶段</dt><dd>{assignedUav ? uavDisplayState(assignedUav).label : "未派工"}</dd></div>
              </dl>
              <div className="evidence-heading"><span><Eye size={13} />证据关键点</span><small>{samples.length}</small></div>
              <div className="evidence-list">
                {samples.length ? samples.map((sample) => <div className="evidence-row" key={sample.sample_id}><span className={`source-mark source-${sample.source}`}><Radio size={11} />{sourceLabel(sample.source)}</span><span className="mono">{position(sample.position)}</span><small>{sample.sample_id}</small></div>) : <span className="panel-empty">暂无关键点</span>}
              </div>
              {assessment && <div className="assessment-note"><strong>研判理由</strong><span>{assessment.reasons?.join("；") || "已记录评估"}</span><small>置信度 {Math.round(Number(assessment.confidence || 0) * 100)}% · 证据 {assessment.evidence_sample_ids?.length || 0} 条</small></div>}
            </div>
          )}
        </>
      )}
    </section>
  );
}
