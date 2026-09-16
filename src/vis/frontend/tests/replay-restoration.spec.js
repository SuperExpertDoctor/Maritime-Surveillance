import { expect, test } from "@playwright/test";

test("approaching probe is not displayed as acquired tracking", async ({ page }) => {
  await page.goto("/");
  const state = await page.evaluate(async () => {
    const { uavDisplayState } = await import("/src/renderer/displayState.js");
    return uavDisplayState({
      status: "tracking",
      operation_mode: "probe",
      sensor_mode: "off",
      task_visual: {
        task_type: "probe",
        phase: "baseline",
        observation_started: false,
      },
    });
  });
  expect(state).toEqual({ label: "接近调查", tone: "approach", phase: "probe_approach" });
});

test("label layout is deterministic, bounded, and priority aware", async ({ page }) => {
  await page.goto("/");
  const layouts = await page.evaluate(async () => {
    const { layoutLabels } = await import("/src/renderer/labelLayout.js");
    const input = [
      {
        id: "contact-low",
        anchor: { x: 38, y: 38 },
        text: "C0002 UNKNOWN",
        width: 70,
        height: 16,
        priority: "contact",
      },
      {
        id: "uav-high",
        anchor: { x: 38, y: 38 },
        text: "UAV-1",
        width: 42,
        height: 16,
        priority: "uav",
      },
      {
        id: "scenario-low",
        anchor: { x: 38, y: 38 },
        text: "SCENE VESSEL",
        width: 86,
        height: 16,
        priority: "scenario",
      },
    ];
    return [
      layoutLabels(input, { x: 0, y: 0, width: 80, height: 80 }),
      layoutLabels(input, { x: 0, y: 0, width: 80, height: 80 }),
    ];
  });
  const [labels, repeated] = layouts;

  expect(repeated).toEqual(labels);
  const visible = labels.filter((label) => !label.hidden);
  expect(labels.find((label) => label.id === "uav-high").hidden).toBe(false);
  for (const label of visible) {
    expect(label.x).toBeGreaterThanOrEqual(0);
    expect(label.y).toBeGreaterThanOrEqual(0);
    expect(label.x + label.width).toBeLessThanOrEqual(80);
    expect(label.y + label.height).toBeLessThanOrEqual(80);
  }
  for (let left = 0; left < visible.length; left += 1) {
    for (let right = left + 1; right < visible.length; right += 1) {
      const a = visible[left];
      const b = visible[right];
      expect(a.x + a.width <= b.x
        || b.x + b.width <= a.x
        || a.y + a.height <= b.y
        || b.y + b.height <= a.y).toBe(true);
    }
  }
});

test("contacts take the observation branch and scenario labels remain render-only", async ({ page }) => {
  await page.goto("/");
  const result = await page.evaluate(async () => {
    const { drawLabels, renderFrame } = await import("/src/renderer/layers.js");
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = 480;
    canvas.style.width = "640px";
    canvas.style.height = "480px";
    document.body.appendChild(canvas);
    const context = canvas.getContext("2d");
    const frame = {
      uavs: [],
      contacts: [{ contact_id: "C-OBS", estimated_position: [9, 9], vessel_class: "unknown", state: "pending" }],
      ships: [{ id: "legacy-ship", position: [9, 9], is_detected: true }],
      scenario_vessels: [{ scenario_entity_id: "scene-ship", position: [9, 9], vessel_class: "type_i" }],
      bases: [],
      obstacles: [],
      search_regions: [],
      track_regions: [],
      evidence: [],
      intents: [],
      intent_statuses: [],
      markers: [],
      uav_trails: [],
    };
    const bounds = { x: 20, y: 20, width: 600, height: 420 };
    const withoutScenario = drawLabels(context, frame, 12, 20, 20, bounds, null, null, null, [], false);
    const withScenario = drawLabels(context, frame, 12, 20, 20, bounds, null, null, null, [], true);
    let fetchCalls = 0;
    const originalFetch = window.fetch;
    window.fetch = (...args) => {
      fetchCalls += 1;
      return originalFetch(...args);
    };
    renderFrame(context, frame, {
      cellSize: 12,
      offsetX: 20,
      offsetY: 20,
      mapBounds: bounds,
      legendBounds: { x: 0, y: 0, width: 0, height: 0 },
      showGrid: false,
      showScenario: false,
    });
    window.fetch = originalFetch;
    return {
      withoutScenario: withoutScenario.map((label) => label.id),
      withScenario: withScenario.map((label) => label.id),
      fetchCalls,
    };
  });

  expect(result.withoutScenario).toContain("contact:C-OBS");
  expect(result.withoutScenario).not.toContain("ship:legacy-ship");
  expect(result.withoutScenario).not.toContain("scenario:scene-ship");
  expect(result.withScenario).toContain("contact:C-OBS");
  expect(result.withScenario).toContain("scenario:scene-ship");
  expect(result.withScenario).not.toContain("ship:legacy-ship");
  expect(result.fetchCalls).toBe(0);
});
