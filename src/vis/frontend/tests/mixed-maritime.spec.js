import { expect, test } from "@playwright/test";

function frameFixture(mode = "live") {
  const matrix = Array.from({ length: 30 }, () => Array(30).fill(0));
  return {
    schema_version: "mission-frame/v2",
    frame_id: 1,
    cycle: 0,
    mode,
    episode_id: "episode-browser",
    timestamp: "00:01:00",
    sim_time_min: 1,
    runtime_status: "running",
    blocked_role: null,
    memory_version: "baseline",
    total_steps: 10,
    coverage_pct: 0,
    searchable_cells: 840,
    scanned_searchable_cells: 0,
    info_matrix: matrix,
    value_matrix: matrix,
    uavs: [{
      id: "UAV-1",
      status: "idle",
      position: [2, 12],
      heading_deg: 0,
      fuel_remaining_pct: 1,
      remaining_range_km: 1000,
      assigned_region_id: null,
      target_group_id: null,
      time_to_available_min: 0,
      sensor_mode: "idle",
      control_mode: "heuristic",
      control_owner: "system",
      operation_mode: "idle",
      controller_generation: 0,
      safety_intervened: false,
      planned_path: [],
      mission_route: [],
      home_base_grid: [2, 12],
      trail: [],
      sar_look_direction: null,
      sar_footprint: [],
      sar_beam: null,
      sar_imaging: false,
      sar_standby: false,
      eo_fov: null,
      avoidance_level: 0,
      avoidance_path: [],
    }],
    contacts: [{
      contact_id: "C0001",
      revision: 1,
      state: "pending",
      identity: "unknown",
      ais_mmsi: "123456789",
      first_seen_min: 0,
      last_seen_min: 1,
      estimated_position: [15, 12],
      estimated_velocity: [0, 0],
      uncertainty_cells: 0.05,
      assigned_uav_id: null,
      active_probe_id: null,
      last_assessment: null,
      cleared_at_min: null,
      next_probe_not_before_min: 0,
      samples: [{
        sample_id: "AIS:123456789:0x0.0p+0",
        observed_at_min: 0,
        source: "ais",
        source_id: "123456789",
        position: [15, 12],
        velocity: [0, 0],
        uncertainty_cells: 0.05,
        observer_position: null,
        measured_range_cells: null,
        navigation_context: "unknown",
      }],
    }],
    intents: [],
    intent_statuses: [],
    intent_events: [],
    search_regions: [],
    track_regions: [],
    markers: [],
    ships: [],
    obstacles: [],
    bases: [{ id: "Base-1", number: 1, position: [2, 12], occupancy: 0, capacity: 3, busy: false, refueling_uav_ids: [] }],
    base_position: [2, 12],
    support_base_positions: [],
    events: [],
    llm_cycle: null,
    task_area: { width_km: 300, height_km: 300, cell_size_km: 10 },
  };
}

async function installFrameSocket(page, fixture) {
  await page.addInitScript((nextFrame) => {
    class MockWebSocket {
      static OPEN = 1;

      constructor() {
        this.readyState = 0;
        window.setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          window.setTimeout(() => this.onmessage?.({ data: JSON.stringify(nextFrame) }), 0);
        }, 0);
      }

      send() {}

      close() {
        this.readyState = 3;
        this.onclose?.();
      }
    }
    window.WebSocket = MockWebSocket;
  }, fixture);
}

test("drag geometry is direction-independent, clamped, and ignores clicks", async ({ page }) => {
  await page.goto("/");
  const result = await page.evaluate(async () => {
    const { computeLayout, dragToBBox } = await import("/src/renderer/geometry.js");
    const layout = computeLayout(800, 600);
    const point = (col, row) => ({
      x: layout.offsetX + col * layout.cellSize,
      y: layout.offsetY + row * layout.cellSize,
    });
    return {
      forward: dragToBBox(point(3.2, 4.2), point(9.1, 11.1), layout),
      reverse: dragToBBox(point(9.1, 11.1), point(3.2, 4.2), layout),
      click: dragToBBox(point(5, 5), { x: point(5, 5).x + 2, y: point(5, 5).y + 2 }, layout),
      edge: dragToBBox({ x: layout.taskBounds.x - 50, y: layout.taskBounds.y - 50 }, { x: layout.taskBounds.x + layout.taskBounds.width + 50, y: layout.taskBounds.y + layout.taskBounds.height + 50 }, layout),
    };
  });
  expect(result.forward).toEqual([3, 4, 10, 12]);
  expect(result.reverse).toEqual(result.forward);
  expect(result.click).toBeNull();
  expect(result.edge).toEqual([0, 0, 30, 30]);
});

test("operator can draw a focus area and observe queued then applied command", async ({ page }) => {
  const fixture = frameFixture();
  await installFrameSocket(page, fixture);
  let commandId = null;
  let pollCount = 0;
  await page.route("**/api/intents", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON();
    commandId = body.command_id;
    await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ command_id: commandId, status: "queued" }) });
  });
  await page.route("**/api/intent-commands/*", async (route) => {
    pollCount += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(pollCount === 1
        ? { command_id: commandId, status: "queued", intent: null, error_code: null }
        : { command_id: commandId, status: "applied", intent: null, error_code: null }),
    });
  });
  await page.goto("/");
  await expect(page.locator(".connection-state")).toHaveClass(/connected/);

  await page.locator('[aria-label="框选重点区"]').click();
  const canvas = page.locator(".canvas-area canvas");
  const geometry = await page.evaluate(async () => {
    const { computeLayout } = await import("/src/renderer/geometry.js");
    const canvasElement = document.querySelector(".canvas-area canvas");
    const rect = canvasElement.getBoundingClientRect();
    const layout = computeLayout(rect.width, rect.height);
    return { rect, layout };
  });
  await canvas.hover({ position: { x: geometry.layout.offsetX + geometry.layout.cellSize * 4.25, y: geometry.layout.offsetY + geometry.layout.cellSize * 5.25 } });
  await page.mouse.down();
  await page.mouse.move(geometry.rect.x + geometry.layout.offsetX + geometry.layout.cellSize * 10.75, geometry.rect.y + geometry.layout.offsetY + geometry.layout.cellSize * 12.75);
  await page.mouse.up();
  await expect(page.locator(".selection-summary")).toContainText("[4, 5, 11, 13]");

  await page.locator('input[placeholder="例如：东南航道"]').fill("东南航道");
  await page.locator(".intent-panel .primary-action").click();
  await expect(page.locator(".command-status")).toContainText("待应用");
  await expect.poll(() => page.locator(".command-status").textContent()).toContain("已应用");
  expect(commandId).toBeTruthy();
});

test("replay renders intent controls as read-only", async ({ page }) => {
  const fixture = frameFixture("replay");
  await installFrameSocket(page, frameFixture());
  await page.route("**/api/replay**", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ total: 1, frames: [fixture] }) });
  });
  await page.route("**/api/replay/list", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ files: ["mixed.jsonl"] }) });
  });
  await page.goto("/");
  await page.locator(".mode-switch button").nth(1).click();
  const fileSelect = page.locator(".file-select");
  await expect(fileSelect.locator('option[value="mixed.jsonl"]')).toHaveCount(1);
  await fileSelect.selectOption("mixed.jsonl");
  await expect(page.locator(".connection-state")).toContainText("1 帧");
  await expect(page.locator(".intent-panel")).toContainText("回放只读");
  await expect(page.locator(".intent-panel .intent-form")).toHaveCount(0);
  await expect(page.locator('[aria-label="框选重点区"]')).toBeDisabled();
});
