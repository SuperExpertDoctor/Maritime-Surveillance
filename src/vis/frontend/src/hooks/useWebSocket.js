import { useEffect, useRef, useState } from "react";

const TELEMETRY_RENDER_INTERVAL_MS = 1000 / 60;

export default function useWebSocket(enabled) {
  const [frame, setFrame] = useState(null);
  const [status, setStatus] = useState("idle");
  const retryCount = useRef(0);
  const pendingFrame = useRef(null);
  const publishFrame = useRef(null);
  const publishTimer = useRef(null);
  const lastPublishedAt = useRef(0);
  const latestFrame = useRef(null);

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      setFrame(null);
      return undefined;
    }
    let disposed = false;
    let socket = null;
    let heartbeat = null;
    let reconnect = null;
    const retiredContexts = new Set();
    const contextOf = item => JSON.stringify([item?.episode_id || "", item?.reset_generation ?? 0]);

    const scheduleFramePublish = () => {
      if (publishFrame.current != null || publishTimer.current != null) return;
      publishFrame.current = window.requestAnimationFrame(() => {
        publishFrame.current = null;
        const elapsed = performance.now() - lastPublishedAt.current;
        if (elapsed < TELEMETRY_RENDER_INTERVAL_MS) {
          publishTimer.current = window.setTimeout(() => {
            publishTimer.current = null;
            scheduleFramePublish();
          }, TELEMETRY_RENDER_INTERVAL_MS - elapsed);
          return;
        }
        lastPublishedAt.current = performance.now();
        if (pendingFrame.current) {
          latestFrame.current = pendingFrame.current;
          setFrame(pendingFrame.current);
          pendingFrame.current = null;
        }
      });
    };

    const connect = () => {
      if (disposed) return;
      setStatus(retryCount.current ? "reconnecting" : "connecting");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const connection = new WebSocket(`${protocol}//${window.location.host}/ws/live`);
      socket = connection;
      const isCurrent = () => !disposed && socket === connection;
      socket.onopen = () => {
        if (!isCurrent()) return;
        retryCount.current = 0;
        setStatus("connected");
        heartbeat = window.setInterval(() => {
          if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
        }, 25000);
      };
      socket.onmessage = (event) => {
        if (!isCurrent() || connection.readyState !== WebSocket.OPEN) return;
        if (event.data === "pong") return;
        try {
          const next = JSON.parse(event.data);
          if (next?.frame_id != null) {
            // WebSocket delivery is asynchronous and can burst when the
            // simulator is faster than the display.  Conflate snapshots and
            // publish at a 60 Hz animation cadence instead of
            // scheduling an unbounded React render queue.
            // Current live frames include their own matrices. Retain support
            // for compact frames from older servers within the same episode.
            const previous = pendingFrame.current || latestFrame.current;
            const sameContext = previous && contextOf(previous) === contextOf(next);
            if (retiredContexts.has(contextOf(next))) return;
            if (previous?.episode_id === next.episode_id
              && (next.reset_generation ?? 0) < (previous.reset_generation ?? 0)) return;
            // Initial/reconnect snapshots can overtake a queued background
            // broadcast. Equal times remain valid for paused command updates.
            if (sameContext && Number.isFinite(next.sim_time_min)
              && Number.isFinite(previous.sim_time_min)
              && next.sim_time_min < previous.sim_time_min) return;
            if (previous && !sameContext) retiredContexts.add(contextOf(previous));
            pendingFrame.current = {
              ...(sameContext ? { info_matrix: previous?.info_matrix, value_matrix: previous?.value_matrix } : {}),
              ...next,
              ...(sameContext && pendingFrame.current ? {
                events: [...(pendingFrame.current.events || []), ...(next.events || [])],
                llm_cycle: next.llm_cycle || pendingFrame.current.llm_cycle,
              } : {}),
            };
            setStatus("connected");
            scheduleFramePublish();
          }
        } catch {
          setStatus("error");
        }
      };
      socket.onerror = () => { if (isCurrent()) connection.close(); };
      socket.onclose = () => {
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
      if (publishFrame.current != null) window.cancelAnimationFrame(publishFrame.current);
      if (publishTimer.current != null) window.clearTimeout(publishTimer.current);
      publishFrame.current = null;
      publishTimer.current = null;
      pendingFrame.current = null;
      lastPublishedAt.current = 0;
      latestFrame.current = null;
      socket?.close();
    };
  }, [enabled]);

  return { frame, status };
}
