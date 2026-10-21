import { useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowDown, ArrowUpRight, Check, ChevronRight, LoaderCircle,
  Shield, Square, X,
} from "lucide-react";

const STATUS = {
  streaming: "生成中", completed: "完成", failed: "失败",
  cancelled: "已停止", queued: "已排队", delivered: "已送达",
  pending_approval: "待审批", approved: "已批准", auto_approved: "自动批准",
  rejected: "已拒绝", expired: "已过期",
};
const APPROVAL_MODES = [
  ["request", "请求批准"], ["assisted", "帮我批准"], ["full", "完全自动"],
];
const DELIVERY_MODES = [
  ["steer", "当前回合反馈"], ["followUp", "下一回合跟进"],
];
const APPROVAL_OPTIONS = [
  ["approve_once", "仅允许本次", "只对这一个计划生效"],
  ["approve_session", "本会话允许同类操作", "同类计划本会话内自动批准"],
  ["reject", "拒绝 / 调整", "填意见后退回给模型调整"],
];
const TOOL_LABELS = {
  get_mission_snapshot: "读取任务态势",
  validate_selection: "校验任务选择",
  submit_selection: "提交任务选择",
  submit_plan_regions: "提交区域规划",
  get_action_status: "查询动作状态",
};
const TRIGGER_LABELS = {
  event: "事件触发", periodic: "周期性LLM触发",
  retry: "手动重试", initial: "初始化触发", human: "人工消息",
};
const KIND_LABELS = {
  "mission-selection/v1": "任务选择计划",
  "legacy-plan/v1": "区域规划计划",
  "operator-message/v1": "操作员消息",
};
const REASON_LABELS = {
  human_required: "请求批准模式：所有计划需人工确认",
  risk_threshold: "存在抢占 / 非常规任务，超出例行阈值",
};

function annotationPayload(message, quote) {
  return { message_id: message.id, quote: String(quote || "").slice(0, 2000) };
}

function toolsByRun(events) {
  const map = new Map();
  for (const event of events || []) {
    if (!event || !event.run_id) continue;
    const list = map.get(event.run_id) || [];
    list.push(event);
    map.set(event.run_id, list);
  }
  return map;
}

function groupApprovalsByMessage(messages, plans) {
  const byId = new Map(messages.map((m) => [m.id, m]));
  const lastByRun = new Map();
  for (const m of messages) if (m.run_id) lastByRun.set(m.run_id, m);
  const linked = new Map();
  const unlinked = [];
  for (const plan of plans || []) {
    const viaId = messages.find((m) => m.plan_id === plan.plan_id);
    const viaRun = lastByRun.get(plan.run_id);
    const target = viaId || viaRun;
    if (target) {
      const list = linked.get(target.id) || [];
      list.push(plan);
      linked.set(target.id, list);
    } else {
      unlinked.push(plan);
    }
  }
  return { linked, unlinked };
}

function WorkingLine({ compact, children, onCancel }) {
  return (
    <div className={`working-line${compact ? " compact" : ""}`}>
      <LoaderCircle className="spin" size={14} />
      <span>
        {children}
      </span>
      {onCancel ? (
        <button type="button" className="icon-btn" onClick={onCancel} title="停止该任务">
          <Square size={12} />
        </button>
      ) : null}
    </div>
  );
}

function ToolItem({ event }) {
  const name = TOOL_LABELS[event.tool] || event.tool || "工具调用";
  const state = event.state || "done";
  const icon = state === "running" ? <LoaderCircle className="spin" size={13} />
    : state === "approval" ? <Shield size={13} />
    : state === "failed" ? <X size={13} />
    : <Check size={13} />;
  const summary = event.summary || event.error || "";
  return (
    <li className={`activity-item ${state}`}>
      <details className="tool-output">
        <summary>
          {icon}
          <span className="activity-label">{name}</span>
          <span className="activity-status">
            {state === "running" ? "执行中" : state === "failed" ? "失败" : state === "approval" ? "待审批" : "完成"}
          </span>
        </summary>
        {summary ? <pre>{typeof summary === "string" ? summary : JSON.stringify(summary, null, 2)}</pre> : null}
      </details>
    </li>
  );
}

function AnnotationReference({ annotation, onJump }) {
  if (!annotation) return null;
  return (
    <div className="annotation-reference">
      <button type="button" className="annotation-link" onClick={() => onJump(annotation.message_id)}>
        <ArrowUpRight size={12} />
        批注自上文消息
      </button>
      <blockquote>{annotation.quote}</blockquote>
    </div>
  );
}

function planTypeLabel(plan) {
  return KIND_LABELS[plan.kind] || plan.kind || "计划";
}

function TriggerSection({ trigger }) {
  return <span>{TRIGGER_LABELS[trigger] || "周期性LLM触发"}</span>;
}

function PlanTemplate({ plan }) {
  const summary = plan.summary || {};
  const selected = summary.selected_task_ids || [];
  const preempted = summary.preempt_uav_ids || [];
  const regionCount = summary.search_region_count;
  return (
    <div className="plan-template">
      <section>
        <h4>任务分配方案</h4>
        {selected.length ? (
          <p>选中任务：{selected.join("、")}</p>
        ) : null}
        {preempted.length ? (
          <p>抢占 UAV：{preempted.join("、")}</p>
        ) : null}
        {regionCount != null ? <p>新规划搜索子区域 {regionCount} 个</p> : null}
        {summary.notes ? <p>{summary.notes}</p> : null}
        {!selected.length && regionCount == null && !preempted.length ? (
          <p>{planTypeLabel(plan)}</p>
        ) : null}
      </section>
      <section>
        <h4>制定方案的原因</h4>
        <Markdown remarkPlugins={[remarkGfm]}>{plan.decision_reason || ""}</Markdown>
      </section>
      <section>
        <h4>触发方式</h4>
        <p><TriggerSection trigger={plan.trigger} /></p>
      </section>
    </div>
  );
}

function ApprovalRequestItem({ plan, simTimeMin, onDecide, onPreview }) {
  const [option, setOption] = useState("approve_once");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = plan.status === "pending_approval";
  const remaining = plan.expires_at_s != null
    ? Math.max(0, Math.round(plan.expires_at_s - (simTimeMin || 0) * 60))
    : null;
  const decision = option === "reject" && comment.trim() ? "adjust" : option;
  const submit = () => {
    setBusy(true);
    onDecide(plan.plan_id, decision, comment.trim()).finally(() => setBusy(false));
  };
  const members = plan.members || [];
  return (
    <div className={`approval-request${pending ? "" : " resolved"}`}>
      <div className="approval-request-header">
        {pending ? <Shield size={14} /> : <Check size={14} />}
        <strong>{planTypeLabel(plan)}</strong>
        <span>{pending && remaining != null ? `剩余 ${remaining}s` : STATUS[plan.status] || plan.status}</span>
      </div>
      <PlanTemplate plan={plan} />
      <details className="approval-details">
        <summary>授权范围与回退策略</summary>
        <dl>
          <div><dt>审批原因</dt><dd>{REASON_LABELS[plan.reason] || plan.reason || "人工确认"}</dd></div>
          {plan.decided_by ? <div><dt>审批方</dt><dd>{plan.decided_by === "session_grant" ? "会话授权" : plan.decided_by === "autonomy" ? "自主模式" : "操作员"}</dd></div> : null}
          {members.length ? <div><dt>执行范围</dt><dd>{members.join("、")}</dd></div> : null}
          {plan.execution_domain ? <div><dt>覆盖区域</dt><dd>{plan.execution_domain.map((v) => Math.round(v)).join(" ~ ")}</dd></div> : null}
          {plan.domain_policy ? <div><dt>范围策略</dt><dd>{plan.domain_policy}</dd></div> : null}
          {plan.fallback ? <div><dt>回退策略</dt><dd>{plan.fallback}</dd></div> : null}
          <div><dt>风险</dt><dd>{plan.risk != null ? `${Math.round(plan.risk * 100)}%` : "低"}</dd></div>
          <div><dt>计划ID</dt><dd>{plan.plan_id}</dd></div>
          {plan.decision_comment ? <div><dt>审批意见</dt><dd>{plan.decision_comment}</dd></div> : null}
        </dl>
      </details>
      {pending ? (
        <form className="approval-form" onSubmit={(e) => { e.preventDefault(); submit(); }}>
          <fieldset className="approval-options">
            {APPROVAL_OPTIONS.map(([value, title, hint]) => (
              <label key={value} className={`approval-option${option === value ? " selected" : ""}`}>
                <input
                  type="radio" name={`decision-${plan.plan_id}`} value={value}
                  checked={option === value} onChange={() => setOption(value)}
                />
                <span className="approval-option-number">{value === "approve_once" ? "1" : value === "approve_session" ? "2" : "3"}</span>
                <span><strong>{title}</strong><small>{hint}</small></span>
              </label>
            ))}
          </fieldset>
          <textarea
            className="approval-comment" rows={2}
            placeholder={option === "reject" ? "填写退回意见后将转为「退回调整」" : "审批意见（可选）"}
            value={comment} onChange={(e) => setComment(e.target.value)}
          />
          <div className="approval-submit-row">
            {onPreview ? (
              <button type="button" className="approval-preview" onClick={() => onPreview(plan)}>地图预览</button>
            ) : null}
            <button type="submit" className="approval-submit" disabled={busy}>
              {decision === "adjust" ? "退回调整" : decision === "reject" ? "拒绝" : "批准"}
            </button>
            <span>{decision === "adjust" ? "连同意见退回模型" : decision === "reject" ? "计划将被拒绝" : "立即执行"}</span>
          </div>
          <small className="approval-paused">审批期间队列暂停，其他任务等待本次决定</small>
        </form>
      ) : null}
    </div>
  );
}

export default function ConversationPanel({ agent, readOnly, onPreviewPlan }) {
  const agentStatus = agent?.agent || {};
  const {
    messages = [], plans = [], events = [], jobs = [],
    simTimeMin = 0,
  } = agent || {};
  const autonomyMode = agentStatus.autonomy_mode || "request";
  const model = agentStatus.model || "";
  const [draft, setDraft] = useState("");
  const [delivery, setDelivery] = useState("steer");
  const [mode, setMode] = useState(autonomyMode);
  const [annotation, setAnnotation] = useState(null);
  const [selection, setSelection] = useState(null);
  const [follow, setFollow] = useState(true);
  const [highlightId, setHighlightId] = useState(null);
  const timelineRef = useRef(null);
  const pendingCount = plans.filter((p) => p.status === "pending_approval").length;

  useEffect(() => { setMode(autonomyMode); }, [autonomyMode]);

  const toolsMap = useMemo(() => toolsByRun(events), [events]);
  const { linked, unlinked } = useMemo(() => groupApprovalsByMessage(messages, plans), [messages, plans]);

  const jumpToMessage = (id) => {
    const el = timelineRef.current?.querySelector(`[data-message-id="${id}"]`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      setHighlightId(id);
      setFollow(false);
      setTimeout(() => setHighlightId(null), 2400);
    }
  };

  useEffect(() => {
    if (!follow || !timelineRef.current) return;
    timelineRef.current.scrollTop = timelineRef.current.scrollHeight;
  }, [messages, events, plans, follow]);

  useEffect(() => {
    const onSelection = () => {
      const sel = window.getSelection();
      const text = sel && String(sel).trim();
      if (!text) { setSelection(null); return; }
      const anchor = sel.anchorNode?.parentElement?.closest?.("[data-message-id]");
      if (!anchor || !timelineRef.current?.contains(anchor)) { setSelection(null); return; }
      setSelection({ messageId: anchor.dataset.messageId, quote: text });
    };
    document.addEventListener("selectionchange", onSelection);
    return () => document.removeEventListener("selectionchange", onSelection);
  }, []);

  const send = async () => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    const payload = { text, delivery };
    if (annotation) payload.annotation = annotation;
    setAnnotation(null);
    await fetch("/api/pi-agent/messages", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setFollow(true);
  };

  const decide = (planId, decision, comment) => fetch(`/api/pi-agent/plans/${planId}/decision`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, comment }),
  }).then(async (r) => { if (!r.ok) throw new Error(await r.text()); });

  const switchMode = (value) => {
    setMode(value);
    fetch("/api/autonomy-mode", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: value }),
    });
  };

  const cancelJob = (runId) => fetch(`/api/pi-agent/jobs/${runId}/cancel`, { method: "POST" });

  const onScroll = () => {
    const el = timelineRef.current;
    if (!el) return;
    setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  };

  return (
    <div className="conversation-panel">
      <div className="agent-timeline" ref={timelineRef} onScroll={onScroll}>
        {messages.map((m) => {
          const activities = m.run_id ? toolsMap.get(m.run_id) : null;
          const approvals = linked.get(m.id) || [];
          const streaming = m.status === "streaming";
          return (
            <div
              key={m.id}
              className={`agent-turn${m.id === highlightId ? " highlighted" : ""}`}
              data-message-id={m.id}
            >
              <div className={`agent-message ${m.role}${m.id === highlightId ? " linked-message" : ""}`}>
                <header>
                  <strong>{m.role === "user" ? "操作员" : "PI AGENT"}</strong>
                  <span>{STATUS[m.status] || m.status}</span>
                  {m.delivery ? <span>{m.delivery === "steer" ? "打断" : "排队"}</span> : null}
                </header>
                {m.role === "user" && m.annotation ? (
                  <AnnotationReference annotation={m.annotation} onJump={jumpToMessage} />
                ) : null}
                <div className="markdown-body">
                  <Markdown remarkPlugins={[remarkGfm]}>{m.text || m.error || ""}</Markdown>
                </div>
                {streaming ? <WorkingLine compact>Working…</WorkingLine> : null}
              </div>
              {activities?.length ? (
                <details className="activity-group">
                  <summary>
                    <ChevronRight size={12} />
                    活动过程
                    <span className="activity-count">{activities.length}</span>
                  </summary>
                  <ul className="activity-list">
                    {activities.map((event, i) => <ToolItem key={i} event={event} />)}
                  </ul>
                </details>
              ) : null}
              {approvals.map((plan) => (
                <ApprovalRequestItem
                  key={plan.plan_id} plan={plan} simTimeMin={simTimeMin}
                  onDecide={decide} onPreview={onPreviewPlan}
                />
              ))}
            </div>
          );
        })}
        {messages.length === 0 && !jobs.length ? (
          <p className="agent-empty">尚无消息 —— 智能体将在调度触发后输出任务分配方案。</p>
        ) : null}
        {jobs.filter((j) => j.status === "queued" || j.status === "running").map((job) => (
          <WorkingLine key={job.run_id} onCancel={() => cancelJob(job.run_id)}>
            {job.status === "running" ? "正在执行" : "排队中"}：{KIND_LABELS[job.kind] || job.kind}
            <small>{TRIGGER_LABELS[job.trigger] || job.trigger || ""}</small>
          </WorkingLine>
        ))}
        {unlinked.length ? (
          <div className="unlinked-approvals">
            <div className="unlinked-label">未关联到对话回合</div>
            {unlinked.map((plan) => (
              <ApprovalRequestItem
                key={plan.plan_id} plan={plan} simTimeMin={simTimeMin}
                onDecide={decide} onPreview={onPreviewPlan}
              />
            ))}
          </div>
        ) : null}
      </div>
      {!follow ? (
        <button type="button" className="follow-latest" onClick={() => { setFollow(true); timelineRef.current?.scrollTo({ top: timelineRef.current.scrollHeight, behavior: "smooth" }); }}>
          <ArrowDown size={13} /> 最新消息
        </button>
      ) : null}
      {readOnly ? (
        <div className="composer-paused">回放模式 —— 对话面板只读</div>
      ) : pendingCount > 0 ? (
        <div className="composer-paused">等待审批后继续对话</div>
      ) : (
        <div className="chat-composer">
          {selection ? (
            <button
              type="button" className="quote-action"
              onClick={() => {
                const message = messages.find((m) => m.id === selection.messageId);
                if (message) setAnnotation(annotationPayload(message, selection.quote));
                setSelection(null);
              }}
            >
              <ArrowUpRight size={12} /> 批注所选内容
            </button>
          ) : null}
          {annotation ? (
            <div className="annotation-draft">
              <span>批注</span>
              <blockquote>{annotation.quote}</blockquote>
              <button type="button" className="icon-btn" onClick={() => setAnnotation(null)}><X size={13} /></button>
            </div>
          ) : null}
          <div className="composer-box">
            <textarea
              value={draft}
              placeholder="向 PI AGENT 发送消息…"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send(); }}
            />
            <div className="composer-actions">
              <span className="composer-model">{model || "PI AGENT"}</span>
              <label className="composer-permission">
                <select value={mode} onChange={(e) => switchMode(e.target.value)} title="自主模式">
                  {APPROVAL_MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
              <label className="composer-delivery">
                <select value={delivery} onChange={(e) => setDelivery(e.target.value)} title="投递方式">
                  {DELIVERY_MODES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
              <button type="button" className="send-message" onClick={send} disabled={!draft.trim()} title="发送">
                <ArrowUpRight size={15} />
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
