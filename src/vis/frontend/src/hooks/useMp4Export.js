import { useCallback, useEffect, useState } from "react";

function downloadMp4(blob, replayFile) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${replayFile.replace(/\.jsonl$/i, "") || "uav-mission-replay"}.mp4`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 1_000);
}

export default function useMp4Export(replay, mapExporterRef) {
  const [available, setAvailable] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    fetch("/api/export/capabilities")
      .then((response) => response.ok ? response.json() : { mp4: false })
      .then((data) => { if (active) setAvailable(Boolean(data.mp4)); })
      .catch(() => { if (active) setAvailable(false); });
    return () => { active = false; };
  }, []);

  const exportMp4 = useCallback(async () => {
    if (exporting || !replay.selectedFile || !mapExporterRef.current) return;
    setError("");
    setExporting(true);
    setProgress(0);
    const previousIndex = replay.index;
    const wasPlaying = replay.isPlaying;
    replay.setIsPlaying(false);
    try {
      const frames = await replay.loadAll((value) => setProgress(Math.round(value * 15)));
      const webm = await mapExporterRef.current.recordReplay(frames, {
        fps: 20,
        onProgress: (value) => setProgress(15 + Math.round(value * 75)),
      });
      setProgress(92);
      const response = await fetch("/api/export/mp4", {
        method: "POST",
        headers: { "Content-Type": "video/webm" },
        body: webm,
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.error || "MP4 export failed");
      }
      downloadMp4(await response.blob(), replay.selectedFile);
      setProgress(100);
    } catch (exportError) {
      setError(exportError instanceof Error ? exportError.message : "MP4 export failed");
    } finally {
      replay.seek(previousIndex);
      replay.setIsPlaying(wasPlaying);
      setExporting(false);
    }
  }, [exporting, mapExporterRef, replay]);

  return { available, exporting, progress, error, exportMp4 };
}
