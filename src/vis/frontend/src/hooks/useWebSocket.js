import { useEffect, useRef, useState } from "react";

export default function useWebSocket(enabled) {
  const [frame, setFrame] = useState(null);
  const [status, setStatus] = useState("idle");
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
          if (next?.frame_id != null) setFrame(next);
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
      socket?.close();
    };
  }, [enabled]);

  return { frame, status };
}
