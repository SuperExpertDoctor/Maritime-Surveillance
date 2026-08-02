import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const CHUNK_SIZE = 120;

export default function useReplay(enabled) {
  const [files, setFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState("");
  const [frames, setFrames] = useState([]);
  const [total, setTotal] = useState(0);
  const [index, setIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const framesRef = useRef([]);
  const totalRef = useRef(0);
  const loadedOffsetsRef = useRef(new Set());

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

  /** Fetch one chunk [offset, offset+CHUNK_SIZE) and merge into framesRef. */
  const fetchChunk = useCallback(async (filename, offset) => {
    const key = `${filename}|${offset}`;
    if (loadedOffsetsRef.current.has(key)) return;
    loadedOffsetsRef.current.add(key);
    const url = `/api/replay?file=${encodeURIComponent(filename)}&offset=${offset}&limit=${CHUNK_SIZE}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.error) throw new Error(data.error);

    // Merge chunk into the contiguous frames array
    const current = [...framesRef.current];
    for (let i = 0; i < data.frames.length; i += 1) {
      const dest = offset + i;
      if (dest < current.length) {
        current[dest] = data.frames[i];
      } else {
        // Extend with sparse holes (filled by subsequent chunks)
        while (current.length < dest) current.push(null);
        current.push(data.frames[i]);
      }
    }
    framesRef.current = current;
    totalRef.current = data.total;
    setFrames([...current]);       // trigger React re-render
    setTotal(data.total);
  }, []);

  const load = useCallback(async (filename) => {
    setSelectedFile(filename);
    setIsPlaying(false);
    setError("");
    if (!filename) {
      framesRef.current = [];
      loadedOffsetsRef.current.clear();
      setFrames([]);
      setTotal(0);
      setIndex(0);
      return;
    }
    setLoading(true);
    loadedOffsetsRef.current.clear();
    framesRef.current = [];
    totalRef.current = 0;
    setFrames([]);
    setTotal(0);
    setIndex(0);
    try {
      await fetchChunk(filename, 0);
    } catch {
      framesRef.current = [];
      setFrames([]);
      setError("回放加载失败");
    } finally {
      setLoading(false);
    }
  }, [fetchChunk]);

  /** Preload the next chunk when the user approaches the end of loaded data. */
  const ensureLoaded = useCallback(async (targetIndex) => {
    const safe = Math.max(0, Math.min(targetIndex, Math.max(0, totalRef.current - 1)));
    // If the slot is already filled, we are done.
    if (safe < framesRef.current.length && framesRef.current[safe] !== null && framesRef.current[safe] !== undefined) {
      return;
    }
    // Otherwise load the chunk that contains this index.
    const chunkOffset = Math.floor(safe / CHUNK_SIZE) * CHUNK_SIZE;
    try {
      await fetchChunk(selectedFile, chunkOffset);
    } catch {
      // Silently ignore — the error state is set in `load`.
    }
  }, [fetchChunk, selectedFile]);

  const seek = useCallback((nextIndex) => {
    const upper = Math.max(0, totalRef.current - 1);
    const clamped = Math.max(0, Math.min(upper, Number(nextIndex) || 0));
    setIndex(clamped);
    ensureLoaded(clamped);
  }, [ensureLoaded]);

  useEffect(() => {
    if (!enabled || !isPlaying) return undefined;
    const timer = window.setInterval(() => {
      setIndex((current) => {
        if (current >= Math.max(0, totalRef.current - 1)) {
          setIsPlaying(false);
          return current;
        }
        const next = current + 1;
        ensureLoaded(next);
        return next;
      });
    }, 1000 / speed);
    return () => window.clearInterval(timer);
  }, [enabled, isPlaying, speed, ensureLoaded]);

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
        seek(Math.round((Number(event.key) / 10) * Math.max(0, totalRef.current - 1)));
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled, index, seek]);

  const markers = useMemo(() => {
    const unique = new Map();
    frames.forEach((frame, frameIndex) => {
      if (!frame) return;
      (frame.events || [])
        .filter((event) => ["target_found", "llm_decision", "uav_returned"].includes(event.type))
        .forEach((event) => {
          const key = `${event.time}|${event.type}|${JSON.stringify(event.data)}`;
          if (!unique.has(key)) unique.set(key, { frameIndex, type: event.type });
        });
    });
    return [...unique.values()];
  }, [frames]);

  const currentFrame = (() => {
    if (index < frames.length) {
      const f = frames[index];
      if (f !== null && f !== undefined) return f;
    }
    // Fallback: find the nearest non-null frame
    for (let offset = 0; offset < Math.max(frames.length, 10); offset += 1) {
      const before = frames[index - offset];
      if (before !== null && before !== undefined) return before;
      const after = frames[index + offset];
      if (after !== null && after !== undefined) return after;
    }
    return null;
  })();

  return {
    files,
    selectedFile,
    load,
    frames,
    total,
    frame: currentFrame,
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
