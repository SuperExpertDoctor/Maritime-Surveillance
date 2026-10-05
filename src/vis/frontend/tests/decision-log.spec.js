import { expect, test } from "@playwright/test";
import { ensureDrawerOpen, frameFixture, installFrameSocket } from "./helpers/frameSocket.js";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/export/capabilities", (route) => route.fulfill({ json: { mp4: false } }));
  await page.route("**/api/model-calls?**", (route) => route.fulfill({ json: { episode_id: "episode-browser", calls: [] } }));
  await page.route("**/api/runtime/decisions?**", (route) => route.fulfill({ json: { decisions: [] } }));
  await page.route("**/api/runtime/logs?**", (route) => route.fulfill({ json: { entries: [], cursor: 0 } }));
});

const decision = (time, id) => ({
  event_id: `episode-browser:${id}`,
  type: "allocation_decision",
  time,
  data: {
    call_id: `call-${id}`,
    time_min: time,
    trigger_source: "event",
    reason_content: "基于目标信息调整搜索优先级",
    selected_task_ids: ["T-1"],
    assignments: [{ task_id: "T-1", uav_id: "UAV-1" }],
    involved_uav_ids: ["UAV-1"],
    status: "committed",
  },
});

test("live decision table appends records and logs show retries and timeouts", async ({ page }, testInfo) => {
  let decisions = [];
  await installFrameSocket(page, frameFixture());
  await page.route("**/api/runtime/decisions?**", async (route) => {
    await route.fulfill({ json: { decisions } });
  });
  await page.route("**/api/runtime/logs?**", async (route) => {
    const after = Number(new URL(route.request().url()).searchParams.get("after") || 0);
    const entries = [
      { id: 1, source: "llm", level: "warning", status: "retry", sim_time_min: 24, role: "decision_maker", attempt: 2 },
      { id: 2, source: "llm", level: "error", status: "timeout", sim_time_min: 24, role: "decision_maker", attempt: 3 },
    ].filter((entry) => entry.id > after);
    await route.fulfill({ json: { entries, cursor: entries.at(-1)?.id ?? after } });
  });
  await page.goto("/");
  await ensureDrawerOpen(page);
  await expect(page.getByRole("columnheader")).toHaveText([
    "决策时间", "触发类型", "决策原因", "决策内容", "参与调度的 UAV",
  ]);
  decisions = [decision(24, 1)];
  await expect(page.locator(".decision-table tbody tr")).toHaveCount(1);
  await expect(page.locator(".decision-table tbody tr")).toContainText("基于目标信息调整搜索优先级");
  await expect(page.locator(".decision-table tbody tr")).toContainText("UAV-1");
  decisions = [...decisions, {
    ...decision(31, 2),
    data: { ...decision(31, 2).data, trigger_source: "periodic", reason_content: "", assignments: [], status: "failed" },
  }];
  await expect(page.locator(".decision-table tbody tr")).toHaveCount(2);
  await expect(page.locator(".decision-table tbody tr").nth(1)).toContainText("周期性主动触发");
  await expect(page.locator(".decision-table tbody tr").nth(1)).toContainText("未完成分配");
  await expect(page.locator(".decision-table tbody tr").nth(1)).toContainText("T-1");
  await page.screenshot({ path: testInfo.outputPath("decision-desktop.png") });
  await page.getByRole("tab", { name: "日志" }).click();
  await expect(page.locator(".runtime-log-row")).toHaveCount(2);
  await expect(page.locator(".runtime-log-list")).toContainText("重试");
  await expect(page.locator(".runtime-log-list")).toContainText("超时");
});

test("mobile decision table scrolls within the drawer", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installFrameSocket(page, frameFixture());
  await page.route("**/api/runtime/decisions?**", (route) => route.fulfill({ json: { decisions: [decision(24, 3)] } }));
  await page.goto("/");
  await ensureDrawerOpen(page);
  await expect(page.locator(".decision-table tbody tr")).toHaveCount(1);
  const dimensions = await page.evaluate(() => ({
    pageWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
    tableWidth: document.querySelector(".decision-table-wrap").scrollWidth,
    containerWidth: document.querySelector(".decision-table-wrap").clientWidth,
  }));
  expect(dimensions.pageWidth).toBeLessThanOrEqual(dimensions.viewportWidth);
  expect(dimensions.tableWidth).toBeGreaterThan(dimensions.containerWidth);
  await page.screenshot({ path: testInfo.outputPath("decision-mobile.png") });
});

test("episode reset drops prior decision and runtime log rows", async ({ page }) => {
  await installFrameSocket(page, frameFixture());
  await page.route("**/api/runtime/decisions?**", (route) => {
    const oldEpisode = new URL(route.request().url()).searchParams.get("episode_id") === "episode-browser";
    return route.fulfill({ json: { decisions: oldEpisode ? [decision(24, 3)] : [] } });
  });
  await page.route("**/api/runtime/logs?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const entries = params.get("after") === "0"
      ? [params.get("episode_id") === "episode-browser"
        ? { id: 1, source: "llm", level: "error", status: "timeout", sim_time_min: 24 }
        : { id: 2, source: "algorithm", level: "info", status: "task_completed", sim_time_min: 0 }]
      : [];
    return route.fulfill({ json: { entries, cursor: entries.at(-1)?.id ?? 2 } });
  });
  await page.goto("/");
  await ensureDrawerOpen(page);
  await expect(page.locator(".decision-table tbody tr")).toHaveCount(1);
  await page.getByRole("tab", { name: "日志" }).click();
  await expect(page.locator(".runtime-log-row")).toHaveCount(1);
  const freshPoll = page.waitForResponse((response) => response.url().includes("/api/runtime/logs?")
    && response.url().includes("episode_id=new-episode"));
  await page.evaluate(() => window.__pushFrame({ ...window.__lastFixture, frame_id: 2, episode_id: "new-episode", reset_generation: 1 }));
  await freshPoll;
  await page.getByRole("tab", { name: "日志" }).click();
  await expect(page.locator(".runtime-log-row")).toHaveCount(1);
  await expect(page.locator(".runtime-log-list")).not.toContainText("超时");
  await page.getByRole("tab", { name: "决策" }).click();
  await expect(page.locator(".decision-table tbody tr")).toHaveCount(0);
});

test("replay decisions respect playback position", async ({ page }) => {
  await installFrameSocket(page, frameFixture());
  const frames = [frameFixture("replay", { frame_id: 0, events: [] }),
    frameFixture("replay", { frame_id: 1, events: [decision(24, 2)] })];
  await page.route("**/api/replay/list", async (route) => route.fulfill({ json: { files: ["decisions.jsonl"] } }));
  await page.route("**/api/replay?**", async (route) => route.fulfill({ json: { total: 2, frames } }));
  await page.goto("/");
  await page.locator(".mode-switch button").nth(1).click();
  await page.locator(".file-select").selectOption("decisions.jsonl");
  await ensureDrawerOpen(page);
  await expect(page.locator(".decision-table tbody tr")).toHaveCount(0);
  await page.getByRole("button", { name: "下一帧" }).click();
  await expect(page.locator(".decision-table tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "上一帧" }).click();
  await expect(page.locator(".decision-table tbody tr")).toHaveCount(0);
});
