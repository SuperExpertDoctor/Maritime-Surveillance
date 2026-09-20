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

function screenshotPath(testInfo, name) {
  return path.join(evidenceDirectory(testInfo), `${name}.png`);
}

async function firstChunk(page, file) {
  const response = await page.request.get(
    `/api/replay?file=${encodeURIComponent(file)}&offset=0&limit=120`,
  );
  expect(response.ok()).toBe(true);
  return response.json();
}

function regionAt(frame, taskId) {
  return (frame.search_regions || []).find((region) => region.id === taskId);
}

test("legacy pending search keeps identity through replay and coverage panel", async ({ page }, testInfo) => {
  test.setTimeout(180_000);
  const listResponse = await page.request.get("/api/replay/list");
  expect(listResponse.ok()).toBe(true);
  const { files } = await listResponse.json();
  const replayFile = files.find((file) => file === "frames.jsonl") || files.find((file) => /frames\.jsonl$/i.test(file));
  expect(replayFile).toBeTruthy();

  const chunk = await firstChunk(page, replayFile);
  expect(chunk.total).toBeGreaterThanOrEqual(6);
  const frames = chunk.frames;
  const reassignmentEvents = frames.flatMap((frame) => frame.events || [])
    .filter((event) => event.type === "pending_search_reassigned");
  expect(reassignmentEvents).toHaveLength(1);
  expect(reassignmentEvents[0].data.assignments).toHaveLength(1);
  const candidates = new Map();
  for (const frame of frames) {
    for (const region of frame.search_regions || []) {
      if (region.type !== "search") continue;
      const history = candidates.get(region.id) || [];
      history.push({ index: frames.indexOf(frame), region });
      candidates.set(region.id, history);
    }
  }
  const transition = [...candidates.entries()].map(([taskId, history]) => {
    const pending = history.find((item) => item.region.status === "active" && item.region.assigned_uav_id == null);
    if (!pending) return null;
    const before = [...history].reverse().find((item) => item.index < pending.index && item.region.assigned_uav_id);
    const after = history.find((item) => item.index > pending.index && item.region.assigned_uav_id);
    return before && after ? { taskId, pending, before, after } : null;
  }).find(Boolean);
  expect(transition).toBeTruthy();
  expect(transition.pending.region.bbox).toEqual(transition.before.region.bbox);
  expect(transition.after.region.bbox).toEqual(transition.before.region.bbox);
  expect(transition.after.region.assigned_uav_id).toBeTruthy();

  await page.goto("/");
  await page.locator(".mode-switch button").nth(1).click();
  const fileSelect = page.locator(".file-select");
  await expect(fileSelect.locator(`option[value="${replayFile}"]`)).toHaveCount(1);
  await fileSelect.selectOption(replayFile);
  await expect(page.locator(".playback-readout").first()).toContainText(`/ ${chunk.total}`);
  await expect(page.locator("canvas")).toBeVisible();
  await expect(page.locator(".coverage-panel")).toBeVisible();
  await expect(page.locator('[data-testid="coverage-primary-value"]')).toBeVisible();

  const canvasPixels = await page.locator("canvas").evaluate((canvas) => {
    const context = canvas.getContext("2d");
    if (!context || !canvas.width || !canvas.height) return 0;
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let nonTransparent = 0;
    for (let index = 3; index < pixels.length; index += 4) {
      if (pixels[index] > 0) nonTransparent += 1;
    }
    return nonTransparent;
  });
  expect(canvasPixels).toBeGreaterThan(1000);

  await page.getByRole("button", { name: "切换任务详情面板" }).click();
  await page.getByRole("tab", { name: "区域" }).click();
  const regionRow = page.locator(".region-table tbody tr").filter({ hasText: transition.taskId });
  await expect(regionRow).toContainText(transition.before.region.assigned_uav_id);
  await page.screenshot({ path: screenshotPath(testInfo, "assigned-before") });

  const timeline = page.locator(".timeline-control input");
  await timeline.fill(String(transition.pending.index));
  await expect(regionRow).toContainText("待分配");
  await expect(regionRow).toContainText(transition.pending.region.bbox.join(", "));
  await page.screenshot({ path: screenshotPath(testInfo, "pending-search") });

  await timeline.fill(String(transition.after.index));
  await expect(regionRow).toContainText(transition.after.region.assigned_uav_id);
  await expect(regionRow).toContainText(transition.after.region.bbox.join(", "));
  await page.screenshot({ path: screenshotPath(testInfo, "assigned-after") });

  await page.getByRole("tab", { name: "时间线" }).click();
  await expect(page.locator(".timeline-list")).toBeVisible();
  await expect(page.locator(".timeline-item")).not.toHaveCount(0);
});
