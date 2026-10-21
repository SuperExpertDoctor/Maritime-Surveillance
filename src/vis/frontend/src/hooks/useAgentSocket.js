import { useEffect, useRef, useState } from "react";

const MAX_MESSAGES = 200;
const MAX_EVENTS = 120;

// Subscribes to /ws/agent: PI agent messages, tool events, plan approvals,
// autonomy mode and AssignmentView rows. Reconnects with backoff like
// useWebSocket; the first frame is the full agent_state snapshot.
export default function useAgentSocket(enabled) {
  const [status, setStatus] = useState("idle");
  const [state, setState] = useState({
    agent: null,
    messages: [],
    plans: [],
    assignments: [],
    events: [],
  });
  const retryCount = useRef(0);

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      return undefined;
    }
    let disposed = false;
    let socket = null;
    let heartbeat = null;
    let reconnect = null;

    const upsertMessage = (message) => {
      setState((current) => {
        const index = current.messages.findIndex((item) => item.id === message.id);
        const messages = index === -1
          ? [...current.messages, message]
          : current.messages.map((item) => (item.id === message.id ? { ...item, ...message } : item));
        return { ...current, messages: messages.slice(-MAX_MESSAGES) };
      });
    };
    const upsertPlan = (plan) => {
      setState((current) => {
        const index = current.plans.findIndex((item) => item.plan_id === plan.plan_id);
        const plans = index === -1
          ? [...current.plans, plan]
          : current.plans.map((item) => (item.plan_id === plan.plan_id ? { ...item, ...plan } : item));
        return { ...current, plans };
      });
    };

    const apply = (frame) => {
      if (!frame || typeof frame !== "object") return;
      if (frame.type === "agent_state") {
        setState({
          agent: frame.agent || null,
          messages: (frame.messages || []).slice(-MAX_MESSAGES),
          plans: frame.plans || [],
          assignments: frame.assignments || [],
          events: (frame.events || []).slice(-MAX_EVENTS),
        });
        return;
      }
      if (frame.type === "agent_message" && frame.message) {
        upsertMessage(frame.message);
        return;
      }
      if ((frame.type === "agent_event" || frame.type === "tool_event" || frame.type === "session_event") && frame.event) {
        setState((current) => ({ ...current, events: [...current.events, frame.event].slice(-MAX_EVENTS) }));
        return;
      }
      if (frame.type === "plan_event" && frame.plan) {
        upsertPlan(frame.plan);
        return;
      }
      if (frame.type === "assignment_view" && frame.assignments) {
        setState((current) => ({ ...current, assignments: frame.assignments }));
        return;
      }
      if (frame.type === "plan_update" && frame.payload?.plan) {
        upsertPlan(frame.payload.plan);
        return;
      }
      if (frame.type === "autonomy_mode") {
        setState((current) => ({ ...current, agent: { ...(current.agent || {}), autonomy_mode: frame.mode } }));
      }
    };

    const connect = () => {
      if (disposed) return;
      setStatus(retryCount.current ? "reconnecting" : "connecting");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const connection = new WebSocket(`${protocol}//${window.location.host}/ws/agent`);
      socket = connection;
      const isCurrent = () => !disposed && socket === connection;
      connection.onopen = () => {
        if (!isCurrent()) return;
        retryCount.current = 0;
        setStatus("connected");
        heartbeat = window.setInterval(() => {
          if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
        }, 25000);
      };
      connection.onmessage = (event) => {
        if (!isCurrent() || connection.readyState !== WebSocket.OPEN) return;
        if (event.data === "pong") return;
        try {
          apply(JSON.parse(event.data));
        } catch {
          setStatus("error");
        }
      };
      connection.onerror = () => { if (isCurrent()) connection.close(); };
      connection.onclose = () => {
        if (!isCurrent()) return;
        socket = null;
        if (heartbeat) window.clearInterval(heartbeat);
        setStatus("reconnecting");
        const delay = Math.min(1000 * 2 ** retryCount.current, 30000);
        retryCount.current += 1;
        reconnect = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      disposed = true;
      if (heartbeat) window.clearInterval(heartbeat);
      if (reconnect) window.clearTimeout(reconnect);
      socket?.close();
    };
  }, [enabled]);

  return { status, ...state };
}
