import { expect, test } from "@playwright/test";
import { frameFixture, installFrameSocket } from "./helpers/frameSocket.js";

test.beforeEach(async ({ page }) => {
  await page.route('**/api/export/capabilities', route => route.fulfill({ json: { mp4: false } }));
});

function coverageFrame({
  minute = 120,
  cells60 = 8,
  domainCells = 100,
  episodeId = "episode-browser",
  overrides = {},
} = {}) {
  const cells30 = Math.min(cells60, 5);
  const cells120 = Math.min(domainCells, Math.max(cells60, 20));
  const cumulativeCells = Math.min(domainCells, Math.max(20, cells60));
  const pct = (count) => domainCells ? count / domainCells * 100 : null;
  const makeWindow = (minutes, count) => ({
    minutes,
    covered_cells: count,
    covered_area_km2: count * 100,
    coverage_pct: pct(count),
    currently_searchable_coverage_pct: pct(count),
    window_complete: minute >= minutes,
  });
  const metrics = {
    schema_version: "persistent-coverage/v1",
    episode_id: episodeId,
    as_of_min: minute,
    status: domainCells ? "ok" : "no_searchable_area",
    source: "sar",
    denominator: "fixed_searchable_sea",
    primary_window_min: 60,
    fixed_searchable_cells: domainCells,
    fixed_searchable_area_km2: domainCells * 100,
    currently_searchable_cells: domainCells,
    weather_blocked_cells: 0,
    ever_scanned_cells: cumulativeCells,
    cumulative_pct: pct(cumulativeCells),
    unseen_pct: domainCells ? 100 - pct(cumulativeCells) : null,
    overdue_seen_pct: domainCells ? pct(cumulativeCells) - pct(cells60) : null,
    windows: [makeWindow(30, cells30), makeWindow(60, cells60), makeWindow(120, cells120)],
  };
  return frameFixture("live", {
    episode_id: episodeId,
    frame_id: minute,
    sim_time_min: minute,
    coverage_metrics: metrics,
    ...overrides,
  });
}

test("coverage follows pushed frames without reload", async ({ page }) => {
  const initial = coverageFrame({ minute: 120, cells60: 8, domainCells: 100 });
  await installFrameSocket(page, initial);
  await page.goto("/");
  const panel = page.getByRole("region", { name: "持续搜索覆盖" });
  await expect(panel.getByTestId("coverage-primary-value")).toHaveText("8.00%");

  const updated = coverageFrame({ minute: 121, cells60: 12, domainCells: 100 });
  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), updated);
  await expect(panel.getByTestId("coverage-primary-value")).toHaveText("12.00%");
  await panel.getByRole("button", { name: "最近 30 分钟" }).click();
  await expect(panel.getByRole("button", { name: "最近 30 分钟" })).toHaveAttribute("aria-pressed", "true");
  await expect(panel.getByTestId("coverage-primary-value")).toHaveText("5.00%");
  await expect(panel.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "5");
});

test("sidebar replaces window metadata with a live information heatmap", async ({ page }) => {
  const info = Array.from({ length: 30 }, () => Array(30).fill(0));
  info[0][0] = -2;
  info[1][2] = 2;
  await installFrameSocket(page, coverageFrame({ minute: 56, overrides: { info_matrix: info } }));
  await page.goto("/");

  const heatmap = page.getByTestId("information-heatmap");
  await expect(heatmap).toBeVisible();
  await expect(page.locator(".coverage-meta")).toHaveCount(0);
  const sample = (col, row) => heatmap.evaluate((canvas, [x, y]) =>
    [...canvas.getContext("2d").getImageData(x * 6 + 2, y * 6 + 2, 1, 1).data], [col, row]);
  const low = await sample(0, 0);
  const high = await sample(1, 2);
  expect(high[0]).toBeLessThan(low[0]);
  const bounds = await heatmap.boundingBox();
  await page.mouse.move(bounds.x + bounds.width * 1.5 / 30, bounds.y + bounds.height * 2.5 / 30);
  await expect(page.locator(".information-heatmap-heading small")).toHaveText("2, 3 · 1.00");

  const updated = Array.from({ length: 30 }, () => Array(30).fill(0));
  updated[0][0] = 1;
  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), coverageFrame({
    minute: 57, overrides: { info_matrix: updated },
  }));
  await expect.poll(() => sample(0, 0)).toEqual(high);
  await expect.poll(() => sample(1, 2)).toEqual(low);
  await expect(page.locator(".information-heatmap-heading small")).toHaveText("2, 3 · 0.00");

  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), coverageFrame({
    minute: 58, overrides: { info_matrix: null },
  }));
  await expect(heatmap).toHaveCount(0);
  await expect(page.locator(".information-heatmap-empty")).toBeVisible();
});

test("information heatmap stays framed in desktop and mobile sidebars", async ({ page }, testInfo) => {
  const info = Array.from({ length: 30 }, (_, col) =>
    Array.from({ length: 30 }, (_, row) => (col + row) / 58));
  await installFrameSocket(page, coverageFrame({ minute: 56, overrides: { info_matrix: info } }));

  for (const [width, height, label] of [[1365, 900, "desktop"], [390, 844, "mobile"]]) {
    await page.setViewportSize({ width, height });
    if (label === "desktop") {
      await page.goto("/");
    } else {
      await page.getByRole("button", { name: "切换编队状态面板" }).click();
    }
    const canvas = page.getByTestId("information-heatmap");
    await canvas.scrollIntoViewIfNeeded();
    await expect(canvas).toBeVisible();
    const geometry = await canvas.evaluate((element) => {
      const heatmap = element.getBoundingClientRect();
      const panel = element.closest(".coverage-panel").getBoundingClientRect();
      return { width: heatmap.width, height: heatmap.height,
        inside: heatmap.left >= panel.left && heatmap.right <= panel.right,
        ratio: heatmap.width / heatmap.height };
    });
    expect(geometry.inside).toBe(true);
    expect(geometry.width).toBeGreaterThan(150);
    expect(geometry.width).toBeLessThanOrEqual(200);
    expect(geometry.ratio).toBeCloseTo(1, 2);
    await page.screenshot({ path: testInfo.outputPath(`information-${label}.png`) });
  }
});

test("window values, areas, and overdue percentage use the selected frame window", async ({ page }) => {
  await installFrameSocket(page, coverageFrame({ minute: 120, cells60: 12, domainCells: 100 }));
  await page.goto("/");
  const panel = page.getByRole("region", { name: "持续搜索覆盖" });

  await expect(panel).toContainText("1,200 / 10,000 km²");
  await expect(panel).toContainText("超时未重访");
  await expect(panel.locator(".coverage-stat-grid dd").nth(1)).toHaveText("8.00%");
  await panel.getByRole("button", { name: "最近 120 分钟" }).click();
  await expect(panel.getByTestId("coverage-primary-value")).toHaveText("20.00%");
  await expect(panel).toContainText("2,000 / 10,000 km²");
  await expect(panel.locator(".coverage-stat-grid dd").nth(1)).toHaveText("0.00%");
});

test("missing, unsupported, empty, paused, and disconnected states stay explicit", async ({ page }) => {
  await installFrameSocket(page, frameFixture("live", { coverage_metrics: null }));
  await page.goto("/");
  let panel = page.getByRole("region", { name: "持续搜索覆盖" });
  await expect(panel).toContainText("等待覆盖指标");
  await expect(panel.getByTestId("coverage-primary-value")).toHaveText("—");
  await expect(panel.getByRole("progressbar")).toHaveCount(0);

  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), coverageFrame({
    minute: 3.4,
    cells60: 0,
    overrides: {
      runtime_status: "paused_model",
      coverage_metrics: { schema_version: "persistent-coverage/v0" },
    },
  }));
  await expect(panel).toContainText("不支持的指标版本");

  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), coverageFrame({
    minute: 3.4,
    cells60: 0,
    domainCells: 0,
    overrides: { runtime_status: "paused_model" },
  }));
  await expect(panel).toContainText("无可搜索海域");
  await expect(panel).toContainText("模型暂停 · 指标停留在仿真 3.40 min");

  await page.evaluate(() => window.__disconnect());
  await expect(panel).toContainText("连接中断，非实时");
});

test("new episodes reset the window to 60 minutes and replay frames are read-only", async ({ page }) => {
  await installFrameSocket(page, coverageFrame({ minute: 120, cells60: 12, domainCells: 100 }));
  await page.goto("/");
  const panel = page.getByRole("region", { name: "持续搜索覆盖" });
  await panel.getByRole("button", { name: "最近 30 分钟" }).click();
  await expect(panel.getByTestId("coverage-primary-value")).toHaveText("5.00%");

  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), coverageFrame({
    minute: 1,
    cells60: 2,
    episodeId: "episode-next",
  }));
  await expect(panel.getByRole("button", { name: "最近 60 分钟" })).toHaveAttribute("aria-pressed", "true");
  await expect(panel.getByTestId("coverage-primary-value")).toHaveText("2.00%");

  const replayFrame = coverageFrame({ minute: 2, cells60: 3, episodeId: "episode-replay" });
  replayFrame.mode = "replay";
  await page.route("**/api/replay**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ total: 1, frames: [replayFrame] }),
    });
  });
  await page.route("**/api/replay/list", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ files: ["coverage.jsonl"] }),
    });
  });
  await page.getByRole("button", { name: "回放" }).click();
  await page.getByLabel("选择回放文件").selectOption("coverage.jsonl");
  await expect(panel).toContainText("回放数据");
});
