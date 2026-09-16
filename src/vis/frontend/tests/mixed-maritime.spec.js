import { expect, test } from "@playwright/test";

function frameFixture(mode = "live", overrides = {}) {
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
    vessel_mutation_allowed: true,
    initial_vessel_count: 8,
    actual_vessel_count: 8,
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
      vessel_class: "unknown",
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
    ...overrides,
  };
}

async function installFrameSocket(page, fixture) {
  await page.addInitScript((nextFrame) => {
    let activeSocket = null;
    class MockWebSocket {
      static OPEN = 1;

      constructor() {
        activeSocket = this;
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
    window.__lastFixture = nextFrame;
    window.__pushFrame = (frame) => {
      window.__lastFixture = frame;
      activeSocket?.onmessage?.({ data: JSON.stringify(frame) });
    };
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
  const fixture = frameFixture("replay", {
    vessel_mutation_allowed: true,
    scenario_vessels: [{
      scenario_entity_id: "scenario-vessel-replay",
      revision: 2,
      position: [12, 8],
      vessel_class: "type_ii",
      ais_enabled: true,
      ais_controllable: true,
      surveillance_stage: "detected",
    }],
  });
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
  await expect(page.getByRole("button", { name: "II 类船舶" })).toBeDisabled();
  await page.getByRole("button", { name: /scenario-vessel-replay/ }).click();
  await expect(page.getByRole("button", { name: "关闭 AIS" })).toBeDisabled();
});

test("replay retries a failed chunk and surfaces the recovered frame", async ({ page }) => {
  await installFrameSocket(page, frameFixture());
  let chunkRequests = 0;
  await page.route("**/api/replay**", async (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get("offset") || 0);
    if (offset === 120) {
      chunkRequests += 1;
      if (chunkRequests === 1) {
        await route.fulfill({ status: 503, body: "temporarily unavailable" });
        return;
      }
    }
    const frames = Array.from({ length: 120 }, (_, index) => frameFixture("replay", {
      frame_id: offset + index + 1,
      sim_time_min: offset + index,
    }));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ total: 240, frames }),
    });
  });
  await page.route("**/api/replay/list", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ files: ["retry.jsonl"] }),
    });
  });

  await page.goto("/");
  await page.locator(".mode-switch button").nth(1).click();
  const fileSelect = page.locator(".file-select");
  await fileSelect.selectOption("retry.jsonl");
  const readout = page.locator(".playback-readout").first();
  await expect(readout).toContainText("1 / 240");

  await page.locator(".timeline-control input").fill("150");
  await expect(page.locator(".connection-state")).toContainText("回放加载失败");
  await page.locator(".timeline-control input").fill("149");
  await expect(readout).toContainText("150 / 240");
  await page.locator(".timeline-control input").fill("150");
  await expect(readout).toContainText("151 / 240");
  expect(chunkRequests).toBe(2);
});

test("replay can jump over unloaded chunks without a page error", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await installFrameSocket(page, frameFixture());
  await page.route("**/api/replay**", async (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get("offset") || 0);
    const frames = Array.from({ length: 120 }, (_, index) => frameFixture("replay", {
      frame_id: offset + index + 1,
      sim_time_min: offset + index,
    }));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ total: 480, frames }),
    });
  });
  await page.route("**/api/replay/list", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ files: ["sparse.jsonl"] }),
    });
  });

  await page.goto("/");
  await page.locator(".mode-switch button").nth(1).click();
  await page.locator(".file-select").selectOption("sparse.jsonl");
  await expect(page.locator(".playback-readout").first()).toContainText("1 / 480");
  await page.locator(".timeline-control input").fill("360");
  await expect(page.locator(".playback-readout").first()).toContainText("361 / 480");
  expect(pageErrors).toEqual([]);
});

test("operator places a type-II vessel by click and deletes the selected scenario vessel", async ({ page }) => {
  const fixture = frameFixture("live", {
    vessel_mutation_allowed: true,
    initial_vessel_count: 8,
    actual_vessel_count: 8,
    scenario_vessels: [{
      scenario_entity_id: "scenario-vessel-9",
      revision: 4,
      position: [12, 8],
      vessel_class: "type_ii",
      ais_enabled: true,
      ais_controllable: true,
      surveillance_stage: "undetected",
    }],
  });
  await installFrameSocket(page, fixture);
  const posted = [];
  const deleted = [];
  await page.route("**/api/vessels", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    posted.push(route.request().postDataJSON());
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ command_id: posted.at(-1).command_id, status: "queued" }),
    });
  });
  await page.route("**/api/vessels/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    deleted.push(route.request().url().split("/").at(-1));
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ command_id: "delete-command", status: "queued" }),
    });
  });
  await page.route("**/api/vessel-commands/*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ command_id: route.request().url().split("/").at(-1), status: "applied", error_code: null }),
    });
  });
  await page.goto("/");

  await expect(page.getByRole("button", { name: "II 类船舶" })).toBeEnabled();
  await page.getByRole("button", { name: "II 类船舶" }).click();
  const canvas = page.locator(".canvas-area canvas");
  const geometry = await page.evaluate(async () => {
    const { computeLayout } = await import("/src/renderer/geometry.js");
    const canvasElement = document.querySelector(".canvas-area canvas");
    const rect = canvasElement.getBoundingClientRect();
    return { rect, layout: computeLayout(rect.width, rect.height) };
  });
  await canvas.click({
    position: {
      x: geometry.layout.offsetX + geometry.layout.cellSize * 12.5,
      y: geometry.layout.offsetY + geometry.layout.cellSize * 8.5,
    },
  });
  await expect.poll(() => posted).toHaveLength(1);
  expect(posted[0].vessel_class).toBe("type_ii");
  expect(posted[0].position_cells).toEqual([12.5, 8.5]);

  await page.getByRole("button", { name: /scenario-vessel-9/ }).click();
  await page.getByRole("button", { name: "删除选中船舶" }).click();
  await expect.poll(() => deleted).toContain("scenario-vessel-9");
});

test("vessel editing is disabled outside the initialization window", async ({ page }) => {
  await installFrameSocket(page, frameFixture("live", { vessel_mutation_allowed: false }));
  await page.goto("/");
  await expect(page.getByRole("button", { name: "II 类船舶" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "I 类船舶", exact: true })).toBeDisabled();
});

test("type-II AIS control sends the current revision and waits for an authoritative frame", async ({ page }) => {
  const fixture = frameFixture("live", {
    scenario_vessels: [{
      scenario_entity_id: "scenario-vessel-ii",
      revision: 4,
      position: [12, 8],
      vessel_class: "type_ii",
      ais_enabled: true,
      ais_controllable: true,
      surveillance_stage: "detected",
    }],
  });
  await installFrameSocket(page, fixture);
  const patches = [];
  await page.route("**/api/vessels/*/ais", async (route) => {
    patches.push(route.request().postDataJSON());
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ command_id: "ais-command", status: "queued" }),
    });
  });
  await page.route("**/api/vessel-commands/ais-command", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ command_id: "ais-command", status: "applied", error_code: null }),
    });
  });
  await page.goto("/");

  await page.getByRole("button", { name: /scenario-vessel-ii/ }).click();
  const disable = page.getByRole("button", { name: "关闭 AIS" });
  await expect(disable).toHaveAttribute("aria-pressed", "false");
  await disable.click();
  await expect.poll(() => patches).toHaveLength(1);
  expect(patches[0]).toMatchObject({
    episode_id: "episode-browser",
    expected_revision: 4,
    ais_enabled: false,
  });
  await expect(disable).toHaveAttribute("aria-pressed", "false");

  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), {
    ...fixture,
    frame_id: 2,
    scenario_vessels: [{
      ...fixture.scenario_vessels[0],
      revision: 5,
      ais_enabled: false,
    }],
  });
  await expect(page.getByRole("button", { name: "关闭 AIS" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "开启 AIS" })).toHaveAttribute("aria-pressed", "false");
});

test("runtime vessel palette stays enabled and the count follows authoritative frames", async ({ page }) => {
  const fixture = frameFixture("live", {
    sim_time_min: 10,
    vessel_mutation_allowed: true,
    actual_vessel_count: 8,
  });
  await installFrameSocket(page, fixture);
  await page.goto("/");
  await expect(page.getByRole("button", { name: "I 类船舶", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "II 类船舶" })).toBeEnabled();
  await expect(page.locator(".vessel-editor .section-heading small")).toHaveText("8/8");

  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), {
    ...fixture,
    frame_id: 2,
    actual_vessel_count: 9,
    scenario_vessels: [{
      scenario_entity_id: "scenario-vessel-9",
      revision: 1,
      position: [12, 8],
      vessel_class: "type_i",
      ais_enabled: true,
      ais_controllable: false,
      surveillance_stage: "undetected",
    }],
  });
  await expect(page.locator(".vessel-editor .section-heading small")).toHaveText("9/8");

  await page.evaluate((nextFrame) => window.__pushFrame(nextFrame), {
    ...fixture,
    frame_id: 3,
    actual_vessel_count: 8,
    scenario_vessels: [],
  });
  await expect(page.locator(".vessel-editor .section-heading small")).toHaveText("8/8");
});

test("AIS revision conflict is visible and does not change the selected frame state", async ({ page }) => {
  const fixture = frameFixture("live", {
    scenario_vessels: [{
      scenario_entity_id: "scenario-vessel-conflict",
      revision: 7,
      position: [12, 8],
      vessel_class: "type_ii",
      ais_enabled: true,
      ais_controllable: true,
      surveillance_stage: "detected",
    }],
  });
  await installFrameSocket(page, fixture);
  await page.route("**/api/vessels/*/ais", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error_code: "revision_conflict" }),
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: /scenario-vessel-conflict/ }).click();
  await page.getByRole("button", { name: "关闭 AIS" }).click();
  await expect(page.locator(".vessel-command-status")).toContainText("revision_conflict");
  await expect(page.getByRole("button", { name: "关闭 AIS" })).toHaveAttribute("aria-pressed", "false");
});

test("operator sees runtime command transition from queued to applied", async ({ page }) => {
  await installFrameSocket(page, frameFixture("live", {
    runtime_status: "paused_model",
    blocked_role: "red_commander",
  }));
  let commandId = null;
  let pollCount = 0;
  await page.route("**/api/runtime/retry", async (route) => {
    commandId = route.request().postDataJSON().command_id;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ command_id: commandId, status: "queued" }),
    });
  });
  await page.route("**/api/intent-commands/*", async (route) => {
    pollCount += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        command_id: commandId,
        status: pollCount === 1 ? "queued" : "applied",
        intent: null,
        error_code: null,
      }),
    });
  });

  await page.goto("/");
  await expect(page.locator(".connection-state")).toHaveClass(/connected/);
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.locator(".command-note")).toContainText("待应用");
  await expect.poll(() => page.locator(".command-note").textContent()).toContain("已应用");
  expect(commandId).toBeTruthy();
  expect(pollCount).toBeGreaterThan(1);
});
