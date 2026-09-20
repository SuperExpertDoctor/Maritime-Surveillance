import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Clock3, Focus, Pencil, Plus, RotateCcw, Send, Trash2, X } from "lucide-react";

const INITIAL_DRAFT = {
  label: "",
  bbox: null,
  mode: "search_priority",
  priority: "high",
  weight: 0.5,
  valid_duration_min: 120,
  revisit_interval_min: 20,
};

const LIFECYCLE_LABELS = {
  active: "活动",
  expired: "已到期",
  cancelled: "已取消",
};

const COMMAND_LABELS = {
  queued: "待应用",
  applied: "已应用",
  rejected: "已拒绝",
};

function commandId(prefix = "cmd") {
  const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

function formatMinutes(value) {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)} min` : "-";
}

export default function IntentPanel({ frame, readOnly = false, selection, onClearSelection }) {
  const [draft, setDraft] = useState(INITIAL_DRAFT);
  const [editingId, setEditingId] = useState(null);
  const [command, setCommand] = useState(null);
  const [runtimeCommand, setRuntimeCommand] = useState(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const statuses = useMemo(
    () => new Map((frame?.intent_statuses || []).map((status) => [status.intent_id, status])),
    [frame?.intent_statuses],
  );

  useEffect(() => {
    if (!selection || editingId) return;
    setDraft((current) => ({ ...current, bbox: selection }));
  }, [editingId, selection]);

  useEffect(() => {
    if (!command?.command_id || command.status !== "queued") return undefined;
    let stopped = false;
    let timer;
    const poll = async () => {
      try {
        const response = await fetch(`/api/intent-commands/${encodeURIComponent(command.command_id)}`);
        if (!response.ok) throw new Error("命令状态不可用");
        const next = await response.json();
        if (stopped) return;
        setCommand(next);
        if (next.status === "queued") timer = window.setTimeout(poll, 700);
      } catch (pollError) {
        if (!stopped) setError(pollError.message || "命令状态不可用");
      }
    };
    timer = window.setTimeout(poll, 250);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [command]);

  useEffect(() => {
    if (!runtimeCommand?.command_id || runtimeCommand.status !== "queued") return undefined;
    let stopped = false;
    let timer;
    const poll = async () => {
      try {
        const response = await fetch(`/api/intent-commands/${encodeURIComponent(runtimeCommand.command_id)}`);
        if (!response.ok) throw new Error("运行命令状态不可用");
        const next = await response.json();
        if (stopped) return;
        setRuntimeCommand(next);
        if (next.status === "queued") timer = window.setTimeout(poll, 700);
      } catch (pollError) {
        if (!stopped) setError(pollError.message || "运行命令状态不可用");
      }
    };
    timer = window.setTimeout(poll, 250);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [runtimeCommand]);

  const updateDraft = (field, value) => setDraft((current) => ({ ...current, [field]: value }));

  const clearDraft = () => {
    setEditingId(null);
    setDraft(INITIAL_DRAFT);
    setError("");
    onClearSelection?.();
  };

  const submit = async (event) => {
    event.preventDefault();
    if (readOnly || !frame?.episode_id || !draft.bbox) return;
    setSubmitting(true);
    setError("");
    const id = commandId(editingId ? "update" : "create");
    const payload = {
      episode_id: frame.episode_id,
      command_id: id,
      label: draft.label,
      bbox: draft.bbox.map(Number),
      mode: draft.mode,
      priority: draft.priority,
      weight: Number(draft.weight),
      valid_duration_min: Number(draft.valid_duration_min),
      revisit_interval_min: draft.mode === "maintain_freshness"
        ? Number(draft.revisit_interval_min) : null,
    };
    if (editingId) {
      payload.expected_revision = editingId.revision;
    }
    const endpoint = editingId ? `/api/intents/${encodeURIComponent(editingId.intent_id)}` : "/api/intents";
    try {
      const response = await fetch(endpoint, {
        method: editingId ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || result.error_code || "重点区命令未入队");
      setCommand({ command_id: result.command_id, status: result.status });
      if (editingId) setEditingId(null);
    } catch (submitError) {
      setError(submitError.message || "重点区命令未入队");
    } finally {
      setSubmitting(false);
    }
  };

  const beginEdit = (intent) => {
    setEditingId(intent);
    setDraft({
      label: intent.label || "",
      bbox: intent.bbox || null,
      mode: intent.mode || "search_priority",
      priority: intent.priority || "medium",
      weight: intent.weight ?? 0.5,
      valid_duration_min: Math.max(1, Number(intent.expires_at_min || 0) - Number(frame?.sim_time_min || 0)),
      revisit_interval_min: intent.revisit_interval_min ?? 20,
    });
    setError("");
  };

  const cancelIntent = async (intent) => {
    if (readOnly || !frame?.episode_id) return;
    const id = commandId("cancel");
    setError("");
    try {
      const response = await fetch(`/api/intents/${encodeURIComponent(intent.intent_id)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          episode_id: frame.episode_id,
          command_id: id,
          expected_revision: intent.revision,
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || result.error_code || "取消命令未入队");
      setCommand({ command_id: result.command_id, status: result.status });
    } catch (cancelError) {
      setError(cancelError.message || "取消命令未入队");
    }
  };

  const sendRuntimeCommand = async (operation) => {
    if (readOnly || !frame?.episode_id) return;
    const id = commandId(operation);
    setError("");
    try {
      const response = await fetch(`/api/runtime/${operation}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ episode_id: frame.episode_id, command_id: id }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || result.error_code || "运行命令未入队");
      setRuntimeCommand(result);
    } catch (runtimeError) {
      setError(runtimeError.message || "运行命令未入队");
    }
  };

  const intents = frame?.intents || [];
  const runtimeBlocked = frame?.runtime_status === "paused_model";

  return (
    <section className="sidebar-section mission-panel intent-panel" aria-label="人工重点区">
      <div className="section-heading">
        <span><Focus size={15} />人工重点区</span>
        <small>{readOnly ? "回放只读" : `${intents.filter((intent) => intent.lifecycle === "active").length} ACTIVE`}</small>
      </div>

      {runtimeBlocked && (
        <div className="runtime-blocked" role="alert">
          <div className="runtime-blocked-title"><AlertTriangle size={14} /><strong>模型暂停</strong><span>{frame.blocked_role || "unknown"}</span></div>
          <p>当前回合等待模型决策，仿真时钟保持不变。</p>
          {!readOnly && (
            <div className="runtime-actions">
              <button type="button" onClick={() => sendRuntimeCommand("retry")}><RotateCcw size={13} />重试</button>
              <button type="button" className="danger-action" onClick={() => sendRuntimeCommand("abort")}><X size={13} />结束回合</button>
            </div>
          )}
          {runtimeCommand && <small className="command-note">{COMMAND_LABELS[runtimeCommand.status] || runtimeCommand.status}</small>}
        </div>
      )}

      {selection && !readOnly && (
        <div className="selection-summary">
          <span><Focus size={13} />框选边界</span>
          <b className="mono">[{selection.join(", ")}]</b>
          <button type="button" className="icon-btn compact-icon" onClick={onClearSelection} aria-label="清除框选" title="清除框选"><X size={13} /></button>
        </div>
      )}

      {!readOnly && (
        <form className="intent-form" onSubmit={submit}>
          <label className="field-wide">名称<input value={draft.label} onChange={(event) => updateDraft("label", event.target.value)} placeholder="例如：东南航道" maxLength={80} required /></label>
          <label>模式<select value={draft.mode} onChange={(event) => updateDraft("mode", event.target.value)}><option value="search_priority">优先搜索</option><option value="maintain_freshness">保持新鲜</option></select></label>
          <label>优先级<select value={draft.priority} onChange={(event) => updateDraft("priority", event.target.value)}><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label>
          <label>权重<input type="number" min="0" max="2" step="0.1" value={draft.weight} onChange={(event) => updateDraft("weight", event.target.value)} /></label>
          <label>有效期<input type="number" min="1" step="1" value={draft.valid_duration_min} onChange={(event) => updateDraft("valid_duration_min", event.target.value)} /></label>
          {draft.mode === "maintain_freshness" && <label>重访间隔<input type="number" min="1" step="1" value={draft.revisit_interval_min} onChange={(event) => updateDraft("revisit_interval_min", event.target.value)} /></label>}
          <div className="intent-form-actions">
            <button type="submit" className="primary-action" disabled={!draft.bbox || submitting}><Send size={13} />{editingId ? "提交修改" : "提交重点区"}</button>
            {(draft.bbox || editingId) && <button type="button" className="icon-btn" onClick={clearDraft} aria-label="清除重点区表单" title="清除"><X size={14} /></button>}
          </div>
        </form>
      )}

      {command && <div className={`command-status command-${command.status}`}><Clock3 size={13} /><span>{COMMAND_LABELS[command.status] || command.status}</span><b className="mono">{command.command_id.slice(0, 18)}</b>{command.error_code && <small>{command.error_code}</small>}</div>}
      {error && <div className="panel-error" role="status">{error}</div>}

      <div className="intent-list">
        {intents.length === 0 ? <div className="panel-empty">暂无重点区</div> : intents.map((intent) => {
          const status = statuses.get(intent.intent_id);
          const expired = intent.lifecycle !== "active";
          return (
            <div className={`intent-row ${expired ? "expired" : ""}`} key={`${intent.intent_id}-${intent.revision}`}>
              <div className="intent-row-main"><span className={`intent-dot ${intent.priority || "medium"}`} /><strong>{intent.label || intent.intent_id}</strong><span className="intent-lifecycle">{LIFECYCLE_LABELS[intent.lifecycle] || intent.lifecycle}</span></div>
              <div className="intent-row-meta"><span className="mono">{intent.intent_id} · [{intent.bbox?.join(", ")}]</span><span>{status ? `${Math.round((status.coverage_ratio || 0) * 100)}% / ${Math.round((status.freshness_ratio || 0) * 100)}%` : "-"}</span></div>
              <div className="intent-row-meta"><span>{intent.mode === "maintain_freshness" ? "保持新鲜" : "优先搜索"} · 到期 {formatMinutes(intent.expires_at_min)}</span><span>{status?.unmet_reason || "满足"}</span></div>
              {!readOnly && intent.lifecycle === "active" && (
                <div className="intent-row-actions"><button type="button" className="icon-btn compact-icon" onClick={() => beginEdit(intent)} aria-label={`编辑 ${intent.label || intent.intent_id}`} title="编辑"><Pencil size={13} /></button><button type="button" className="icon-btn compact-icon danger-icon" onClick={() => cancelIntent(intent)} aria-label={`取消 ${intent.label || intent.intent_id}`} title="取消"><Trash2 size={13} /></button></div>
              )}
            </div>
          );
        })}
      </div>
      {command?.status === "applied" && <div className="applied-note"><Check size={13} />仿真线程已应用命令</div>}
      {readOnly && <div className="readonly-note">回放帧中的重点区状态来自历史记录。</div>}
    </section>
  );
}
