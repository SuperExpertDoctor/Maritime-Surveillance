import { useEffect, useRef, useState } from "react";
import { Bot, CheckCircle2, Send, ShieldCheck, UserRound, XCircle } from "lucide-react";
import Markdown from "./Markdown";

const MODE_LABELS = { request: "请求审批", assisted: "辅助自动", full: "完全自动" };
const DELIVERY_LABELS = { steer: "steer(打断当前推理)", followUp: "followUp(排队跟进)" };

// PI AGENT 对话窗口：消息时间线 + 工具/计划事件 + 操作员输入 + 计划审批卡片。
export default function ConversationPanel({ agent, events, messages, plans, readOnly }) {
  const [text, setText] = useState("");
  const [delivery, setDelivery] = useState("followUp");
  const [sending, setSending] = useState(false);
  const timelineRef = useRef(null);
  const mode = agent?.autonomy_mode || "full";
  const pendingPlans = plans.filter((plan) => plan.status === "pending_approval");

  useEffect(() => {
    const node = timelineRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages.length, pendingPlans.length]);

  const sendMessage = async () => {
    const trimmed = text.trim();
    if (!trimmed || sending || readOnly) return;
    setSending(true);
    try {
      await fetch("/api/pi-agent/messages", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: trimmed, delivery }),
      });
      setText("");
    } catch { /* 回执经 ws 帧下发 */ }
    setSending(false);
  };

  const decidePlan = async (planId, action) => {
    if (readOnly) return;
    try {
      await fetch(`/api/plans/${encodeURIComponent(planId)}/${action}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
    } catch { /* 计划状态经 ws 帧下发 */ }
  };

  return (
    <div className="conversation-panel">
      <section className="sidebar-section agent-status" aria-label="代理状态">
        <div className="section-heading"><span><ShieldCheck size={15} />PI AGENT</span><small>{agent?.status || "offline"}</small></div>
        <div className="agent-mode-row">
          <span>自主模式</span>
          <strong title="request=每次计划均需审批 · assisted=例行自动/高风险审批 · full=全部自动">{MODE_LABELS[mode] || mode}</strong>
        </div>
        {agent?.error && <p className="editor-note">代理异常：{agent.error}</p>}
      </section>

      <div className="agent-timeline" ref={timelineRef} aria-label="代理消息时间线">
        {messages.map((message) => (
          <article key={message.id} className={`agent-message ${message.role}`}>
            <header>
              {message.role === "user" ? <UserRound size={13} /> : <Bot size={13} />}
              <span>{message.role === "user" ? "操作员" : "PI AGENT"}</span>
              {message.delivery && <small>{message.delivery}</small>}
              {message.status && message.status !== "completed" && <small>{message.status}</small>}
            </header>
            <Markdown text={message.text} />
          </article>
        ))}
        {pendingPlans.map((plan) => (
          <article key={plan.plan_id} className="agent-message plan-card">
            <header><ShieldCheck size={13} /><span>待审批计划</span><small>{plan.kind}</small></header>
            <Markdown text={plan.decision_reason || "模型提交了任务分配计划，等待人工确认。"} />
            <dl className="plan-summary">
              <div><dt>选中任务</dt><dd>{(plan.summary?.selected_task_ids || []).join("、") || `区域 ${plan.summary?.search_region_count ?? "-"} 个`}</dd></div>
              {plan.summary?.preempt_uav_ids?.length > 0 && <div><dt>抢占 UAV</dt><dd>{plan.summary.preempt_uav_ids.join("、")}</dd></div>}
              {plan.summary?.notes && <div><dt>备注</dt><dd>{plan.summary.notes}</dd></div>}
            </dl>
            {!readOnly && (
              <div className="plan-actions">
                <button type="button" className="btn approve" onClick={() => decidePlan(plan.plan_id, "approve")}>
                  <CheckCircle2 size={14} />批准
                </button>
                <button type="button" className="btn reject" onClick={() => decidePlan(plan.plan_id, "reject")}>
                  <XCircle size={14} />拒绝
                </button>
              </div>
            )}
          </article>
        ))}
        {!messages.length && !pendingPlans.length && (
          <div className="sidebar-empty"><Bot size={22} /><span>等待 PI AGENT 消息</span></div>
        )}
      </div>

      <section className="sidebar-section agent-events" aria-label="工具与计划事件">
        <div className="section-heading"><span>工具调用与计划过程</span><small>{events.length} EVENTS</small></div>
        <ul className="agent-event-list">
          {events.slice(-12).map((event, index) => (
            <li key={event.run_id ? `${event.run_id}-${index}` : index}>
              <small>{event.type || "event"}</small>
              <span>{event.text || event.error || event.plan_id || ""}</span>
            </li>
          ))}
          {!events.length && <li><span>尚无代理事件</span></li>}
        </ul>
      </section>

      {!readOnly ? (
        <form className="agent-input" onSubmit={(event) => { event.preventDefault(); sendMessage(); }}>
          <select value={delivery} onChange={(event) => setDelivery(event.target.value)} aria-label="投递方式">
            {Object.entries(DELIVERY_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <input
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="向 PI AGENT 发送指令…"
            aria-label="操作员消息"
          />
          <button type="submit" className="icon-btn" disabled={!text.trim() || sending} aria-label="发送" title="发送"><Send size={15} /></button>
        </form>
      ) : <p className="editor-note replay-note">回放模式：对话面板只读</p>}
    </div>
  );
}
