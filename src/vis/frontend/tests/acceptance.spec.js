import { expect, test } from "@playwright/test";

const REPLAY_FILE = "simulation_20260801_002817.jsonl";
const VIEWPORTS = [
  { name: "desktop-1440", width: 1440, height: 900 },
  { name: "desktop-1280", width: 1280, height: 720 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
];

test("live and replay dashboard acceptance", async ({ page }) => {
  const runtimeErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(message.text());
  });
  page.on("pageerror", (error) => runtimeErrors.push(error.message));

  await page.setViewportSize(VIEWPORTS[0]);
  await page.goto("/");
  await expect(page.locator(".connection-state")).toHaveClass(/connected/);
  await expect(page.locator("canvas")).toBeVisible();
  const config = await page.evaluate(() => fetch("/api/config").then((response) => response.json()));
  expect(config.environment.base_count).toBe(2);
  expect(config.environment.base_land_margin).toBe(0);
  expect(config.environment.mainland_width_cells).toBe(5);
  expect(config.environment.island_count_min).toBe(0);
  expect(config.environment.island_count_max).toBe(2);
  expect(config.environment.base_task_min_distance_cells).toBe(3);
  expect(config.environment.base_obstacle_clearance_cells).toBe(4);
  expect(config.environment.thunderstorm_count_min).toBe(2);
  expect(config.environment.thunderstorm_count_max).toBe(3);
  const assetStatuses = await page.evaluate(() => Promise.all([
    "/assets/background.png",
    "/assets/rainbow-uav.png",
    "/assets/carrier.png",
    "/assets/destroyer.png",
  ].map(async (path) => ({ path, status: (await fetch(path)).status }))));
  expect(assetStatuses.every(({ status }) => status === 200)).toBe(true);
  const assetDimensions = await page.evaluate(() => Promise.all([
    ["/assets/rainbow-uav.png?v=20260801", 1536, 1024],
    ["/assets/carrier.png?v=20260801", 1536, 1024],
    ["/assets/destroyer.png?v=20260801", 1024, 1536],
  ].map(([source, width, height]) => new Promise((resolve) => {
    const image = new Image();
    image.onload = () => resolve({ source, width: image.naturalWidth, height: image.naturalHeight, expected: [width, height] });
    image.src = source;
  }))));
  for (const asset of assetDimensions) {
    expect([asset.width, asset.height]).toEqual(asset.expected);
  }
  const offshoreLayout = await page.evaluate(async () => {
    const { computeLayout } = await import("/src/renderer/geometry.js");
    return computeLayout(1100, 650);
  });
  const taskSize = offshoreLayout.cellSize * 30;
  expect(offshoreLayout.taskBounds.width).toBe(offshoreLayout.taskBounds.height);
  expect(offshoreLayout.taskBounds.width).toBeCloseTo(taskSize, 5);
  expect(offshoreLayout.offsetX).toBeGreaterThan(offshoreLayout.mapBounds.x + offshoreLayout.mapBounds.width * 0.4);
  expect(offshoreLayout.offsetX + taskSize).toBeLessThanOrEqual(
    offshoreLayout.mapBounds.x + offshoreLayout.mapBounds.width,
  );

  const canvasEvidence = await page.locator("canvas").evaluate((canvas) => {
    const context = canvas.getContext("2d");
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    const colors = new Set();
    let opaque = 0;
    const stride = Math.max(4, Math.floor((canvas.width * canvas.height) / 20_000) * 4);
    for (let index = 0; index < pixels.length; index += stride) {
      const alpha = pixels[index + 3];
      if (alpha > 0) opaque += 1;
      colors.add(`${pixels[index]},${pixels[index + 1]},${pixels[index + 2]},${alpha}`);
    }
    return { opaque, colors: colors.size, width: canvas.width, height: canvas.height };
  });
  expect(canvasEvidence.opaque).toBeGreaterThan(100);
  expect(canvasEvidence.colors).toBeGreaterThan(4);
  expect(canvasEvidence.width).toBeGreaterThan(500);
  await page.locator(".trail-mode-switch button").nth(0).click();
  await expect(page.locator(".trail-mode-switch button").nth(0)).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({ path: "test-results/trajectory-full.png", fullPage: true });
  await page.locator(".trail-mode-switch button").nth(1).click();
  await expect(page.locator(".trail-mode-switch button").nth(1)).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({ path: "test-results/trajectory-tail.png", fullPage: true });
  await page.screenshot({
    path: "test-results/acceptance-live-desktop.png",
    fullPage: true,
  });

  await page.locator(".mode-switch button").nth(1).click();
  const fileSelect = page.locator(".file-select");
  await expect(fileSelect.locator(`option[value="${REPLAY_FILE}"]`)).toHaveCount(1);
  await fileSelect.selectOption(REPLAY_FILE);
  await expect(page.locator(".playback-readout").first()).toContainText("480");

  const readout = page.locator(".playback-readout").first();
  await page.locator(".transport-btn.primary").click();
  await expect.poll(async () => readout.textContent()).not.toContain("1 / 480");
  await page.locator(".transport-btn.primary").click();

  await page.locator(".timeline-control input").fill("239");
  await expect(readout).toContainText("240 / 480");
  await page.locator(".trail-mode-switch button").nth(0).click();
  await page.screenshot({ path: "test-results/trajectory-replay-full.png", fullPage: true });
  await page.locator(".trail-mode-switch button").nth(1).click();
  await page.screenshot({ path: "test-results/trajectory-replay-tail.png", fullPage: true });
  await page.locator(".canvas-area").click({ position: { x: 18, y: 18 } });
  await page.keyboard.press("Digit5");
  await expect(readout).toContainText("241 / 480");
  await page.keyboard.press("ArrowRight");
  await expect(readout).toContainText("242 / 480");
  await page.keyboard.press("Space");
  await expect(page.locator(".transport-btn.primary")).toHaveAttribute("title", "暂停");
  await page.keyboard.press("Space");

  await page.locator(".top-actions .icon-btn").nth(1).click();
  await expect(page.locator(".bottom-drawer")).toBeVisible();
  await page.locator(".drawer-tabs > button").nth(1).click();
  await expect(page.locator(".region-table")).toBeVisible();
  await page.locator(".drawer-tabs > button").nth(2).click();
  await expect(page.locator(".llm-log")).toBeVisible();
  await page.locator(".drawer-tabs > button").nth(3).click();
  await expect(page.locator(".ais-table")).toBeVisible();
  await page.locator(".drawer-tabs > button").nth(4).click();
  await expect(page.locator(".params-grid")).toBeVisible();
  await page.locator(".drawer-close").click();
  await expect(page.locator(".bottom-drawer")).toHaveCount(0);

  for (const viewport of VIEWPORTS) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.waitForTimeout(150);
    const overflow = await page.evaluate(() => ({
      document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      body: document.body.scrollWidth - document.body.clientWidth,
    }));
    expect(overflow.document).toBeLessThanOrEqual(1);
    expect(overflow.body).toBeLessThanOrEqual(1);
    await expect(page.locator("canvas")).toBeVisible();
    await page.screenshot({
      path: `test-results/acceptance-${viewport.name}.png`,
      fullPage: true,
    });
  }

  await page.locator(".top-actions .mobile-only").click();
  await expect(page.locator(".sidebar")).toHaveClass(/open/);
  await expect(page.locator(".sidebar-header")).toBeVisible();

  expect(runtimeErrors).toEqual([]);
});
