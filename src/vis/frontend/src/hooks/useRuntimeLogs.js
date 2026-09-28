import { useEffect, useState } from "react";

export default function useRuntimeLogs(episodeId, generation) {
  const [decisions, setDecisions] = useState([]);
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    setDecisions([]);
    setLogs([]);
    setError("");
    if (!episodeId) {
      return undefined;
    }
    let stopped = false;
    let timer;
    let cursor = 0;
    const controller = new AbortController();

    const poll = async () => {
      try {
        const [decisionResponse, logResponse] = await Promise.all([
          fetch(`/api/runtime/decisions?episode_id=${encodeURIComponent(episodeId)}`, { signal: controller.signal }),
          fetch(`/api/runtime/logs?after=${cursor}&episode_id=${encodeURIComponent(episodeId)}`, { signal: controller.signal }),
        ]);
        if (!decisionResponse.ok || !logResponse.ok) throw new Error("运行日志暂不可用");
        const [decisionData, logData] = await Promise.all([
          decisionResponse.json(), logResponse.json(),
        ]);
        if (stopped) return;
        setDecisions(decisionData.decisions || []);
        setLogs((current) => [...current, ...(logData.entries || [])].slice(-300));
        cursor = logData.cursor ?? cursor;
        setError("");
      } catch (cause) {
        if (!stopped) setError(cause.message || "运行日志暂不可用");
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 1000);
      }
    };
    poll();
    return () => {
      stopped = true;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [episodeId, generation]);

  return { decisions, logs, error };
}
