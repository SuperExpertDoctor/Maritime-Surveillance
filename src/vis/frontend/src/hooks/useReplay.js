import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export default function useReplay(enabled) {
  const [files, setFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState("");
  const [frames, setFrames] = useState([]);
  const [index, setIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const framesRef = useRef([]);

  useEffect(() => {
    if (!enabled) return;
    setError("");
    fetch("/api/replay/list")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((data) => setFiles(data.files || []))
      .catch(() => setError("无法读取回放列表"));
  }, [enabled]);

  const load = useCallback(async (filename) => {
    setSelectedFile(filename);
    setIsPlaying(false);
    setError("");
    if (!filename) {
      framesRef.current = [];
      setFrames([]);
      setIndex(0);
      return;
    }
    setLoading(true);
    try {
      const response = await fetch(`/api/replay?file=${encodeURIComponent(filename)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const text = await response.text();
      const parsed = text.trim() ? text.trim().split("\n").map((line) => JSON.parse(line)) : [];
      framesRef.current = parsed;
      setFrames(parsed);
      setIndex(0);
      if (!parsed.length) setError("回放文件没有帧数据");
    } catch {
      framesRef.current = [];
      setFrames([]);
      setError("回放加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const seek = useCallback((nextIndex) => {
    const upper = Math.max(0, framesRef.current.length - 1);
    setIndex(Math.max(0, Math.min(upper, Number(nextIndex) || 0)));
  }, []);

  useEffect(() => {
    if (!enabled || !isPlaying || frames.length < 2) return undefined;
    const timer = window.setInterval(() => {
      setIndex((current) => {
        if (current >= framesRef.current.length - 1) {
          setIsPlaying(false);
          return current;
        }
        return current + 1;
      });
    }, 1000 / speed);
    return () => window.clearInterval(timer);
  }, [enabled, frames.length, isPlaying, speed]);

  useEffect(() => {
    const onKeyDown = (event) => {
      if (!enabled || /INPUT|SELECT|TEXTAREA/.test(event.target?.tagName || "")) return;
      if (event.code === "Space") {
        event.preventDefault();
        setIsPlaying((current) => !current);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        seek(index - 1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        seek(index + 1);
      } else if (/^[0-9]$/.test(event.key)) {
        seek(Math.round((Number(event.key) / 10) * Math.max(0, framesRef.current.length - 1)));
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled, index, seek]);

  const markers = useMemo(() => {
    const unique = new Map();
    frames.forEach((frame, frameIndex) => {
      (frame.events || [])
        .filter((event) => ["target_found", "llm_decision", "uav_returned"].includes(event.type))
        .forEach((event) => {
          const key = `${event.time}|${event.type}|${JSON.stringify(event.data)}`;
          if (!unique.has(key)) unique.set(key, { frameIndex, type: event.type });
        });
    });
    return [...unique.values()];
  }, [frames]);

  return {
    files,
    selectedFile,
    load,
    frames,
    frame: frames[index] || null,
    index,
    seek,
    isPlaying,
    setIsPlaying,
    speed,
    setSpeed,
    loading,
    error,
    markers,
  };
}
