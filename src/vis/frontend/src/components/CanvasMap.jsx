import { useCallback, useEffect, useRef, useState } from "react";
import { RadioTower } from "lucide-react";

import { computeLayout, pixelToCoord } from "../renderer/geometry";
import { renderFrame } from "../renderer/layers";

const MAP_ASSET_SOURCES = {
  background: "/assets/background.png",
  uav: "/assets/rainbow-uav.png",
  carrier: "/assets/carrier.png",
  destroyer: "/assets/destroyer.png",
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
}) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const layoutRef = useRef({ cellSize: 20, offsetX: 0, offsetY: 0 });
  const hoverRef = useRef(null);
  const [hovered, setHovered] = useState(false);
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
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const context = canvas.getContext("2d");
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let animationFrame = null;
    let phase = 0;

    const render = () => {
      const { cellSize, offsetX, offsetY } = layoutRef.current;
      context.save();
      renderFrame(context, frame, {
        cellSize,
        offsetX,
        offsetY,
        showGrid,
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
  }, [frame, mapAssets, selectedUavId, showGrid, sizeVersion]);

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
