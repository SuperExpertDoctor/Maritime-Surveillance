import { useCallback, useEffect, useRef, useState } from "react";
import { RadioTower } from "lucide-react";

import { computeLayout, pixelToCoord } from "../renderer/geometry";
import { renderFrame } from "../renderer/layers";

const MAP_ASSET_SOURCES = {
  background: "/assets/background.png",
  uav: "/assets/rainbow-uav.png?v=20260801",
  carrier: "/assets/carrier.png?v=20260801",
  destroyer: "/assets/destroyer.png?v=20260801",
};

function loadMapAsset(source) {
  return new Promise((resolve) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = source;
  });
}

export default function CanvasMap({
  frame,
  selectedUavId,
  onSelectUav,
  showGrid = false,
  trailMode = "tail",
}) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const layoutRef = useRef({ cellSize: 20, offsetX: 0, offsetY: 0 });
  const hoverRef = useRef(null);
  const [hovered, setHovered] = useState(false);
  const prevFrameRef = useRef(null);
  const targetFrameRef = useRef(null);
  const frameReceivedRef = useRef(0);
  const [sizeVersion, setSizeVersion] = useState(0);
  const [mapAssets, setMapAssets] = useState({});

  useEffect(() => {
    let disposed = false;
    Promise.all(Object.entries(MAP_ASSET_SOURCES).map(async ([key, source]) => [
      key,
      await loadMapAsset(source),
    ])).then((entries) => {
      if (!disposed) setMapAssets(Object.fromEntries(entries.filter(([, image]) => image)));
    });
    return () => { disposed = true; };
  }, []);

  const updateSize = useCallback(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    const width = Math.max(1, container.clientWidth);
    const height = Math.max(1, container.clientHeight);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
    layoutRef.current = computeLayout(width, height);
    setSizeVersion((version) => version + 1);
  }, []);

  useEffect(() => {
    updateSize();
    const observer = new ResizeObserver(updateSize);
    if (containerRef.current) observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, [updateSize]);

  useEffect(() => {
    if (!frame) return;
    prevFrameRef.current = targetFrameRef.current;
    targetFrameRef.current = frame;
    frameReceivedRef.current = performance.now();
  }, [frame]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const context = canvas.getContext("2d");
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let animationFrame = null;
    let phase = 0;
    const INTERP_MS = 180;

    const lerpAngle = (a, b, k) => {
      if (a == null || b == null) return b ?? a ?? 0;
      let d = ((b - a) % 360 + 540) % 360 - 180;
      return a + d * k;
    };

    const render = () => {
      const prev = prevFrameRef.current;
      const target = targetFrameRef.current;
      let displayFrame = target;

      if (prev && target) {
        const elapsed = performance.now() - frameReceivedRef.current;
        const raw = Math.min(1, elapsed / INTERP_MS);
        if (raw < 1) {
          const t = 1 - (1 - raw) ** 2; // ease-out quad

          const prevUavMap = new Map();
          for (const u of prev.uavs || []) prevUavMap.set(u.id, u);
          const prevShipMap = new Map();
          for (const s of prev.ships || []) prevShipMap.set(s.id, s);

          const uavs = (target.uavs || []).map((u) => {
            const prevU = prevUavMap.get(u.id);
            if (!prevU) return u;
            return {
              ...u,
              position: [
                prevU.position[0] + (u.position[0] - prevU.position[0]) * t,
                prevU.position[1] + (u.position[1] - prevU.position[1]) * t,
              ],
              heading_deg: lerpAngle(prevU.heading_deg, u.heading_deg, t),
            };
          });

          const ships = (target.ships || []).map((s) => {
            const prevS = prevShipMap.get(s.id);
            if (!prevS) return s;
            return {
              ...s,
              position: [
                prevS.position[0] + (s.position[0] - prevS.position[0]) * t,
                prevS.position[1] + (s.position[1] - prevS.position[1]) * t,
              ],
              heading_deg: lerpAngle(prevS.heading_deg, s.heading_deg, t),
            };
          });

          displayFrame = { ...target, uavs, ships };
        }
      }

      const { cellSize, offsetX, offsetY, mapBounds, legendBounds } = layoutRef.current;
      context.save();
      renderFrame(context, displayFrame, {
        cellSize,
        offsetX,
        offsetY,
        mapBounds,
        legendBounds,
        showGrid,
        trailMode,
        hoverInfo: hoverRef.current,
        selectedUavId,
        frameCount: phase,
        assets: mapAssets,
      });
      context.restore();
      phase += 1;
      if (!reducedMotion) animationFrame = window.requestAnimationFrame(render);
    };

    render();
    return () => {
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
    };
  }, [mapAssets, selectedUavId, showGrid, sizeVersion, trailMode]);

  const handleMouseMove = useCallback((event) => {
    const canvas = canvasRef.current;
    if (!canvas || !frame) return;
    const rect = canvas.getBoundingClientRect();
    const { cellSize, offsetX, offsetY } = layoutRef.current;
    const coord = pixelToCoord(
      event.clientX - rect.left,
      event.clientY - rect.top,
      cellSize,
      offsetX,
      offsetY,
    );

    if (coord && frame.info_matrix && frame.value_matrix) {
      const info = Number(frame.info_matrix?.[coord.col]?.[coord.row] || 0);
      const value = Number(frame.value_matrix?.[coord.col]?.[coord.row] || 0);
      const category = info >= 0.7 ? "white" : info >= 0.2 ? "gray" : "black";
      hoverRef.current = { col: coord.col, row: coord.row, I: info, V: value, category };
      setHovered(true);
    } else {
      hoverRef.current = null;
      setHovered(false);
    }
  }, [frame]);

  const handleMouseLeave = useCallback(() => {
    hoverRef.current = null;
    setHovered(false);
  }, []);

  const handleClick = useCallback((event) => {
    const canvas = canvasRef.current;
    if (!canvas || !frame?.uavs) return;
    const rect = canvas.getBoundingClientRect();
    const mouseX = event.clientX - rect.left;
    const mouseY = event.clientY - rect.top;
    const { cellSize, offsetX, offsetY } = layoutRef.current;

    for (const uav of frame.uavs) {
      const [col, row] = uav.position;
      const centerX = offsetX + (col + 0.5) * cellSize;
      const centerY = offsetY + (row + 0.5) * cellSize;
      if (Math.hypot(mouseX - centerX, mouseY - centerY) < Math.max(9, cellSize * 0.55)) {
        onSelectUav?.(uav.id === selectedUavId ? null : uav.id);
        return;
      }
    }
  }, [frame, onSelectUav, selectedUavId]);

  return (
    <div className="canvas-area" ref={containerRef}>
      <canvas
        ref={canvasRef}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        style={{ cursor: hovered ? "crosshair" : "default" }}
        aria-label="Operational map"
      />
      {!frame && (
        <div className="map-empty" role="status">
          <RadioTower size={22} />
          <strong>WAITING FOR MISSION DATA</strong>
          <span>Live telemetry or replay frames will appear here.</span>
        </div>
      )}
      <div className="map-scale" aria-hidden="true"><i />20 KM</div>
    </div>
  );
}
