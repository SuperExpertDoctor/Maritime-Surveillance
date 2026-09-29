import { expect, test } from "@playwright/test";
import {
  drawFocusArea,
  selectVesselPlacement,
  deleteVessel,
  setVesselAis,
  retryPausedRuntime,
  createFrameTracker,
} from "../../../../scripts/live_interaction_ui.mjs";
import { frameFixture, installFrameSocket } from "./helpers/frameSocket.js";

const typeI = { scenario_entity_id: "ship-i", revision: 2, vessel_class: "type_i",
  position: [4.5, 7.5], ais_enabled: true, ais_controllable: false };
const typeII = { scenario_entity_id: "ship-ii", revision: 4, vessel_class: "type_ii",
  position: [12.5, 8.5], ais_enabled: true, ais_controllable: true };

test.beforeEach(async ({ page }) => {
  await page.route("**/api/export/capabilities", (route) => route.fulfill({ json: { mp4: false } }));
  await page.route("**/api/runtime/decisions?*", (route) => route.fulfill({ json: { decisions: [] } }));
  await page.route("**/api/runtime/logs?*", (route) => route.fulfill({ json: { entries: [], cursor: 0 } }));
  await page.route("**/api/model-calls?*", (route) => route.fulfill({ json: { episode_id: "episode-browser", calls: [] } }));
});

test("focus helper draws exactly 5x5 on the map and waits for the authoritative frame", async ({ page }) => {
  const fixture = frameFixture();
  await installFrameSocket(page, fixture);
  const tracker = createFrameTracker(fixture);
  let submitted;
  await page.route("**/api/intents", async (route) => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ json: { command_id: submitted.command_id, status: "queued" } });
  });
  await page.route("**/api/intent-commands/*", async (route) => route.fulfill({
    json: {
      command_id: submitted.command_id,
      status: "applied",
      error_code: null,
      intent: {
        intent_id: "focus-live-1",
        revision: 1,
        lifecycle: "active",
        bbox: [10, 10, 15, 15],
        label: "Focus 1",
      },
    },
  }));

  await page.goto("/");
  const pending = drawFocusArea(page, tracker, {
    bbox: [10, 10, 15, 15],
    label: "Focus 1",
    durationMin: 360,
    timeoutMs: 5000,
  });
  await expect.poll(() => submitted?.bbox).toEqual([10, 10, 15, 15]);
  expect(submitted).toMatchObject({ mode: "search_priority", valid_duration_min: 360 });
  await expect(page.locator(".command-status")).toContainText("已应用");

  await page.evaluate((frame) => window.__pushFrame(frame), {
    ...fixture,
    frame_id: 2,
    intents: [{ ...submitted, intent_id: "focus-live-1", revision: 1, lifecycle: "active" }],
    intent_statuses: [],
  });
  tracker.observe({
    ...fixture,
    frame_id: 2,
    intents: [{ ...submitted, intent_id: "focus-live-1", revision: 1, lifecycle: "active" }],
    intent_statuses: [],
  });

  const result = await pending;
  expect(result.commandId).toBe(submitted.command_id);
  expect(result.state.intent.bbox).toEqual([10, 10, 15, 15]);
  expect(result.frameId).toBe(2);
});

test("consecutive focus draws keep the box-selection tool enabled and submit each new bbox", async ({ page }) => {
  const fixture = frameFixture();
  await installFrameSocket(page, fixture);
  const tracker = createFrameTracker(fixture);
  const submitted = [];
  await page.route("**/api/intents", async (route) => {
    const body = route.request().postDataJSON();
    submitted.push(body);
    return route.fulfill({ json: { command_id: body.command_id, status: "queued" } });
  });
  await page.route("**/api/intent-commands/*", async (route) => {
    const commandId = new URL(route.request().url()).pathname.split("/").at(-1);
    const body = submitted.find((item) => item.command_id === commandId);
    const index = submitted.indexOf(body) + 1;
    return route.fulfill({
      json: {
        command_id: commandId,
        status: "applied",
        error_code: null,
        intent: { ...body, intent_id: `focus-live-${index}`, revision: 1, lifecycle: "active" },
      },
    });
  });

  await page.goto("/");
  const firstBbox = [10, 10, 15, 15];
  const secondBbox = [16, 10, 21, 15];
  const firstPending = drawFocusArea(page, tracker, {
    bbox: firstBbox, label: "Focus 1", timeoutMs: 5000,
  });
  await expect.poll(() => submitted.length).toBe(1);
  const firstFrame = {
    ...fixture,
    frame_id: 2,
    intents: [{ ...submitted[0], intent_id: "focus-live-1", revision: 1, lifecycle: "active" }],
  };
  await page.evaluate((frame) => window.__pushFrame(frame), firstFrame);
  tracker.observe(firstFrame);
  await firstPending;

  const secondPending = drawFocusArea(page, tracker, {
    bbox: secondBbox, label: "Focus 2", timeoutMs: 5000,
  });
  await expect.poll(() => submitted.length).toBe(2);
  expect(submitted[1].bbox).toEqual(secondBbox);
  const secondFrame = {
    ...firstFrame,
    frame_id: 3,
    intents: [
      firstFrame.intents[0],
      { ...submitted[1], intent_id: "focus-live-2", revision: 1, lifecycle: "active" },
    ],
  };
  await page.evaluate((frame) => window.__pushFrame(frame), secondFrame);
  tracker.observe(secondFrame);
  const result = await secondPending;

  expect(result.state.intent.bbox).toEqual(secondBbox);
});

test("vessel placement uses the palette and waits for its matching live entity", async ({ page }) => {
  const fixture = frameFixture("live", { scenario_vessels: [typeI] });
  await installFrameSocket(page, fixture);
  const tracker = createFrameTracker(fixture);
  let submitted;
  await page.route("**/api/vessels", async (route) => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ status: 202, json: { command_id: submitted.command_id, status: "queued" } });
  });
  await page.route("**/api/vessel-commands/*", async (route) => route.fulfill({
    json: { command_id: submitted.command_id, status: "applied", vessel_id: "ship-new", revision: 1 },
  }));

  await page.goto("/");
  const pending = selectVesselPlacement(page, tracker, "type_ii", [22, 20], { timeoutMs: 5000 });
  await expect.poll(() => submitted?.vessel_class).toBe("type_ii");
  expect(submitted.position_cells).toEqual([22.5, 20.5]);
  const confirmed = { ...fixture, frame_id: 2, scenario_vessels: [typeI, {
    ...typeII, scenario_entity_id: "ship-new", revision: 1, position: [22.5, 20.5],
  }] };
  await page.evaluate((frame) => window.__pushFrame(frame), confirmed);
  tracker.observe(confirmed);
  const result = await pending;
  expect(result.state.scenario_vessels.at(-1).scenario_entity_id).toBe("ship-new");
});

test("delete waits until the selected vessel is absent from a live frame", async ({ page }) => {
  const fixture = frameFixture("live", { scenario_vessels: [typeI, typeII] });
  await installFrameSocket(page, fixture);
  const tracker = createFrameTracker(fixture);
  let submitted;
  await page.route("**/api/vessels/ship-ii", async (route) => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ status: 202, json: { command_id: submitted.command_id, status: "queued" } });
  });
  await page.route("**/api/vessel-commands/*", async (route) => route.fulfill({
    json: { command_id: submitted.command_id, status: "applied", vessel_id: "ship-ii", revision: 5 },
  }));

  await page.goto("/");
  const pending = deleteVessel(page, tracker, "ship-ii", { timeoutMs: 5000 });
  await expect.poll(() => submitted?.expected_revision).toBe(4);
  const confirmed = { ...fixture, frame_id: 2, scenario_vessels: [typeI] };
  await page.evaluate((frame) => window.__pushFrame(frame), confirmed);
  tracker.observe(confirmed);
  const result = await pending;
  expect(result.state.scenario_vessels.some((vessel) => vessel.scenario_entity_id === "ship-ii")).toBe(false);
});

test("AIS control waits for the requested state and revision in a live frame", async ({ page }) => {
  const fixture = frameFixture("live", { scenario_vessels: [typeI, typeII] });
  await installFrameSocket(page, fixture);
  const tracker = createFrameTracker(fixture);
  let submitted;
  await page.route("**/api/vessels/ship-ii/ais", async (route) => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ status: 202, json: { command_id: submitted.command_id, status: "queued" } });
  });
  await page.route("**/api/vessel-commands/*", async (route) => route.fulfill({
    json: { command_id: submitted.command_id, status: "applied", vessel_id: "ship-ii", revision: 5 },
  }));

  await page.goto("/");
  const pending = setVesselAis(page, tracker, "ship-ii", false, { timeoutMs: 5000 });
  await expect.poll(() => submitted?.ais_enabled).toBe(false);
  expect(submitted.expected_revision).toBe(4);
  const confirmed = { ...fixture, frame_id: 2, scenario_vessels: [typeI, {
    ...typeII, revision: 5, ais_enabled: false,
  }] };
  await page.evaluate((frame) => window.__pushFrame(frame), confirmed);
  tracker.observe(confirmed);
  const result = await pending;
  expect(result.state.scenario_vessels.find((vessel) => vessel.scenario_entity_id === "ship-ii").ais_enabled).toBe(false);
});

test("model pause retry uses the visible control and waits for resumed live state", async ({ page }) => {
  const fixture = frameFixture("live", {
    runtime_status: "paused_model",
    blocked_role: "decision_maker",
  });
  await installFrameSocket(page, fixture);
  const tracker = createFrameTracker(fixture);
  let submitted;
  await page.route("**/api/runtime/retry", async (route) => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ status: 202, json: { command_id: submitted.command_id, status: "queued" } });
  });
  await page.route("**/api/intent-commands/*", async (route) => route.fulfill({
    json: { command_id: submitted.command_id, status: "applied", error_code: null },
  }));

  await page.goto("/");
  const pending = retryPausedRuntime(page, tracker, { timeoutMs: 5000 });
  await expect.poll(() => submitted?.episode_id).toBe(fixture.episode_id);
  const resumed = { ...fixture, frame_id: 2, runtime_status: "running", blocked_role: null };
  await page.evaluate((frame) => window.__pushFrame(frame), resumed);
  tracker.observe(resumed);

  const result = await pending;
  expect(result.receipt.status).toBe("applied");
  expect(result.state.runtime_status).toBe("running");
});

test("a UI action error does not leak a rejected request waiter", async ({ page }) => {
  const fixture = frameFixture();
  await installFrameSocket(page, fixture);
  const tracker = createFrameTracker(fixture);
  await page.goto("/");
  await page.locator(".canvas-area canvas").evaluate((canvas) => canvas.remove());
  page.setDefaultTimeout(100);

  const pending = drawFocusArea(page, tracker, {
    bbox: [10, 10, 15, 15],
    label: "Broken map",
    timeoutMs: 100,
  });
  await expect(pending).rejects.toThrow();
  await page.waitForTimeout(150);
});
