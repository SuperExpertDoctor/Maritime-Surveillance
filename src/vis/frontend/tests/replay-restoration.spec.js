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

test("replay markers deduplicate reordered events and keep the first frame index", async ({ page }) => {
  await page.goto("/");
  const markers = await page.evaluate(async () => {
    const { collectReplayMarkers } = await import("/src/renderer/replayEvents.js");
    return collectReplayMarkers([
      { frame_id: 41, events: [{ type: "mission_assignment_committed", time: 2.5, data: { uav_id: "UAV-1", task_id: "T-1" } }] },
      { frame_id: 99, events: [{ type: "mission_assignment_committed", time: 2.5, data: { task_id: "T-1", uav_id: "UAV-1" } }] },
      { frame_id: 103, events: [{ type: "mission_assignment_committed", time: 2.5, data: { task_id: "T-1", uav_id: "UAV-1" } }] },
    ]);
  });
  expect(markers).toHaveLength(1);
  expect(markers[0].frameIndex).toBe(0);
  expect(markers[0].type).toBe("mission_assignment_committed");
  expect(markers[0].time).toBe(2.5);
});

test("unloaded replay seeks show no neighboring frame", async ({ page }) => {
  let releaseChunk;
  const delayedChunk = new Promise((resolve) => { releaseChunk = resolve; });
  await page.route("**/api/replay/list", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ files: ["identity.jsonl"] }) });
  });
  await page.route(/\/api\/replay\?/, async (route) => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get("offset") || 0);
    if (offset === 120) await delayedChunk;
    const frames = Array.from({ length: 120 }, (_, index) => ({
      frame_id: offset + index + 1,
      timestamp: `00:${String(Math.floor((offset + index) / 60)).padStart(2, "0")}:${String((offset + index) % 60).padStart(2, "0")}`,
      sim_time_min: offset + index,
      uavs: [],
      contacts: [],
      ships: [],
      events: [],
    }));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ total: 240, frames }),
    });
  });

  await page.goto("/");
  await page.locator(".mode-switch button").nth(1).click();
  await page.locator(".file-select").selectOption("identity.jsonl");
  await expect(page.locator(".playback-readout.time")).toContainText("00:00:00");

  await page.locator(".timeline-control input").fill("150");
  await expect(page.locator(".connection-state")).toContainText("载入目标帧");
  await expect(page.locator(".playback-readout.time")).toContainText("--:--:--");

  releaseChunk();
  await expect(page.locator(".playback-readout.time")).toContainText("00:02:30");
});

test("switching replay files cancels stale frames and markers", async ({ page }) => {
  let releaseNew;
  const delayedNew = new Promise((resolve) => { releaseNew = resolve; });
  await page.route("**/api/replay/list", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ files: ["old.jsonl", "new.jsonl"] }) });
  });
  await page.route(/\/api\/replay\?/, async (route) => {
    const file = new URL(route.request().url()).searchParams.get("file");
    if (file === "new.jsonl") await delayedNew;
    const isOld = file === "old.jsonl";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        total: 1,
        frames: [{
          frame_id: isOld ? 1 : 2,
          timestamp: isOld ? "00:00:01" : "00:09:09",
          sim_time_min: isOld ? 1 : 9,
          uavs: [],
          contacts: [],
          ships: [],
          events: isOld ? [{ type: "target_found", time: 1, data: { contact_id: "OLD" } }] : [],
          llm_cycle: isOld ? { response: "old" } : null,
        }],
      }),
    });
  });

  await page.goto("/");
  await page.locator(".mode-switch button").nth(1).click();
  await page.locator(".file-select").selectOption("old.jsonl");
  await expect(page.locator(".playback-readout.time")).toContainText("00:00:01");
  await expect(page.locator(".event-marks i")).toHaveCount(1);

  await page.locator(".file-select").selectOption("new.jsonl");
  await expect(page.locator(".connection-state")).toContainText("载入中");
  await expect(page.locator(".playback-readout.time")).toContainText("--:--:--");
  await expect(page.locator(".event-marks i")).toHaveCount(0);

  releaseNew();
  await expect(page.locator(".playback-readout.time")).toContainText("00:09:09");
  await expect(page.locator(".event-marks i")).toHaveCount(0);
});
