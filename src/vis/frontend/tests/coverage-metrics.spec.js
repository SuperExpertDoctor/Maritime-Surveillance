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
