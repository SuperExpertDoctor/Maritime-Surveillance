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

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      return undefined;
    }
    let disposed = false;
    let socket = null;
    let heartbeat = null;
    let reconnect = null;

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
        if (pendingFrame.current) setFrame(pendingFrame.current);
      });
    };

    const connect = () => {
      if (disposed) return;
      setStatus(retryCount.current ? "reconnecting" : "connecting");
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/ws/live`);
      socket.onopen = () => {
        retryCount.current = 0;
        setStatus("connected");
        heartbeat = window.setInterval(() => {
          if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
        }, 25000);
      };
      socket.onmessage = (event) => {
        if (event.data === "pong") return;
        try {
          const next = JSON.parse(event.data);
          if (next?.frame_id != null) {
            // WebSocket delivery is asynchronous and can burst when the
            // simulator is faster than the display.  Conflate snapshots and
            // publish at a 60 Hz animation cadence instead of
            // scheduling an unbounded React render queue.
            pendingFrame.current = next;
            scheduleFramePublish();
          }
        } catch {
          setStatus("error");
        }
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (heartbeat) window.clearInterval(heartbeat);
        if (disposed) return;
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
      socket?.close();
    };
  }, [enabled]);

  return { frame, status };
}
