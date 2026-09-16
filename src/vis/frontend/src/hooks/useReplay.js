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
  const loadGenerationRef = useRef(0);
  const selectedFileRef = useRef("");

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
  const fetchChunk = useCallback(async (filename, offset, generation = loadGenerationRef.current) => {
    const key = `${filename}|${offset}`;
    if (generation !== loadGenerationRef.current || filename !== selectedFileRef.current) {
      return false;
    }
    if (loadedOffsetsRef.current.has(key)) return true;
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
    loadedOffsetsRef.current.add(key);
    setFrames([...current]);       // trigger React re-render
    setTotal(data.total);
    setError("");
    return true;
  }, []);

  const load = useCallback(async (filename) => {
    const generation = loadGenerationRef.current + 1;
    loadGenerationRef.current = generation;
    selectedFileRef.current = filename;
    setSelectedFile(filename);
    setIsPlaying(false);
    setError("");
    if (!filename) {
      framesRef.current = [];
      loadedOffsetsRef.current.clear();
      setLoading(false);
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
      await fetchChunk(filename, 0, generation);
    } catch {
      if (generation !== loadGenerationRef.current) return;
      framesRef.current = [];
      setFrames([]);
      setError("回放加载失败");
    } finally {
      if (generation === loadGenerationRef.current) setLoading(false);
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
    const generation = loadGenerationRef.current;
    const filename = selectedFileRef.current || selectedFile;
    if (!filename) return;
    try {
      await fetchChunk(filename, chunkOffset, generation);
    } catch {
      if (generation === loadGenerationRef.current && filename === selectedFileRef.current) {
        setError("回放加载失败");
      }
    }
  }, [fetchChunk, selectedFile]);

  const seek = useCallback((nextIndex) => {
    const upper = Math.max(0, totalRef.current - 1);
    const clamped = Math.max(0, Math.min(upper, Number(nextIndex) || 0));
    setIndex(clamped);
    ensureLoaded(clamped);
  }, [ensureLoaded]);

  const loadAll = useCallback(async (onProgress) => {
    const filename = selectedFileRef.current || selectedFile;
    const generation = loadGenerationRef.current;
    if (!filename || totalRef.current <= 0) return [];
    const totalChunks = Math.ceil(totalRef.current / CHUNK_SIZE);
    for (let chunk = 0; chunk < totalChunks; chunk += 1) {
      const current = await fetchChunk(filename, chunk * CHUNK_SIZE, generation);
      if (!current) throw new Error("Replay selection changed");
      onProgress?.((chunk + 1) / totalChunks);
    }
    const complete = framesRef.current.slice(0, totalRef.current);
    if (complete.some((frame) => frame == null)) {
      throw new Error("Replay frames are incomplete");
    }
    return complete;
  }, [fetchChunk, selectedFile]);

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
    loadAll,
  };
}
