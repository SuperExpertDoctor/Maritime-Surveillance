import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

function evidenceDirectory(testInfo) {
  const directory = process.env.REPLAY_SCREENSHOT_DIR
    ? path.resolve(process.env.REPLAY_SCREENSHOT_DIR)
    : testInfo.outputDir;
  fs.mkdirSync(directory, { recursive: true });
  return directory;
}

function evidencePath(testInfo, name) {
  return path.join(evidenceDirectory(testInfo), name);
}

function screenshotPath(testInfo, name) {
  return evidencePath(testInfo, `${name}.png`);
}

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

test("real replay artifacts drive event-timed visual evidence", async ({ page }, testInfo) => {
  test.setTimeout(300_000);
  const listResponse = await page.request.get("/api/replay/list");
  expect(listResponse.ok()).toBe(true);
  const { files } = await listResponse.json();
  expect(files.length).toBeGreaterThan(0);

  const replayFile = files.find((file) => /v07.*frames/i.test(file)) || files[0];
  expect(replayFile).toMatch(/frames\.jsonl$/i);
  const firstChunkResponse = await page.request.get(
    `/api/replay?file=${encodeURIComponent(replayFile)}&offset=0&limit=120`,
  );
  expect(firstChunkResponse.ok()).toBe(true);
  const firstChunk = await firstChunkResponse.json();
  expect(firstChunk.total).toBeGreaterThan(0);
  expect(firstChunk.frames[0].episode_id).toBeTruthy();
  expect(firstChunk.frames[0].frame_id).toBeTruthy();
  const replayFrames = [...firstChunk.frames];
  if (firstChunk.total > firstChunk.frames.length) {
    const secondChunkResponse = await page.request.get(
      `/api/replay?file=${encodeURIComponent(replayFile)}&offset=120&limit=120`,
    );
    expect(secondChunkResponse.ok()).toBe(true);
    const secondChunk = await secondChunkResponse.json();
    replayFrames.push(...secondChunk.frames);
  }

  await page.goto("/");
  await page.locator(".mode-switch button").nth(1).click();
  const fileSelect = page.locator(".file-select");
  await expect(fileSelect.locator(`option[value="${replayFile}"]`)).toHaveCount(1);
  await fileSelect.selectOption(replayFile);
  const totalFrames = Number(firstChunk.total);
  await expect(page.locator(".playback-readout").first()).toContainText(String(totalFrames));
  await expect(page.locator("canvas")).toBeVisible();
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([
      "/assets/background.png",
      "/assets/rainbow-uav.png?v=20260801",
      "/assets/carrier.png?v=20260801",
      "/assets/destroyer.png?v=20260801",
    ].map((source) => new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => image.complete ? resolve() : reject(new Error(`image_incomplete:${source}`));
      image.onerror = () => reject(new Error(`image_failed:${source}`));
      image.src = source;
    })));
  });

  const eventFrames = {};
  const eventTargets = [
    ["mission_assignment_committed", "01-assignment"],
    ["assessment_applied", "02-assessment"],
    ["return_reserved", "03-return"],
    ["handoff_required", "04-handoff"],
  ];
  for (const [eventType, filename] of eventTargets) {
    const index = replayFrames.findIndex((frame) =>
      (frame.events || []).some((event) => event.type === eventType));
    if (index >= 0) eventFrames[filename] = index;
  }
  const phaseTargets = [
    ["baseline", "05-probe-baseline"],
    ["near", "06-probe-near"],
    ["tracking", "07-tracking"],
  ];
  for (const [phase, filename] of phaseTargets) {
    const index = replayFrames.findIndex((frame) =>
      (frame.uavs || []).some((uav) => uav.task_visual?.phase === phase));
    if (index >= 0) eventFrames[filename] = index;
  }

  for (const [name, index] of Object.entries(eventFrames)) {
    await page.locator(".timeline-control input").fill(String(index));
    await expect(page.locator(".playback-readout").first()).toContainText(`${index + 1} /`);
    await expect.poll(async () => page.locator(".connection-state").textContent()).not.toContain("载入目标帧");
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: screenshotPath(testInfo, name), fullPage: true });
  }

  const sensorModes = new Set(
    replayFrames.flatMap((frame) => (frame.uavs || []).map((uav) => uav.sensor_mode)),
  );
  expect(sensorModes.has("eo")).toBe(true);
  expect(sensorModes.has("sar")).toBe(true);
  expect(Object.keys(eventFrames).length).toBeGreaterThanOrEqual(7);
  const canvasEvidence = await page.locator("canvas").evaluate((canvas) => {
    const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    let opaque = 0;
    const colors = new Set();
    for (let index = 0; index < pixels.length; index += 16) {
      if (pixels[index + 3] > 0) opaque += 1;
      colors.add(`${pixels[index]},${pixels[index + 1]},${pixels[index + 2]},${pixels[index + 3]}`);
    }
    return { opaque, colors: colors.size, width: canvas.width, height: canvas.height };
  });
  expect(canvasEvidence.opaque).toBeGreaterThan(100);
  expect(canvasEvidence.colors).toBeGreaterThan(4);
  await page.screenshot({ path: screenshotPath(testInfo, "08-final-map"), fullPage: true });

  for (const [index, name] of [[119, "09-v07-t120"], [479, "10-v07-t480"]]) {
    await page.locator(".timeline-control input").fill(String(index));
    await expect(page.locator(".playback-readout").first()).toContainText(`${index + 1} /`);
    await expect.poll(async () => page.locator(".connection-state").textContent()).not.toContain("载入目标帧");
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: screenshotPath(testInfo, name), fullPage: true });
  }

  const exportButton = page.locator(".export-mp4-btn");
  await expect(exportButton).toBeEnabled();
  const downloadPromise = page.waitForEvent("download", { timeout: 180_000 });
  await exportButton.click();
  const download = await downloadPromise;
  const mp4Path = evidencePath(testInfo, "v07-seed42-replay.mp4");
  await download.saveAs(mp4Path);
  expect(fs.statSync(mp4Path).size).toBeGreaterThan(1_000);
});
