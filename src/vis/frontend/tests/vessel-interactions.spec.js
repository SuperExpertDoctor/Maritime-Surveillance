import { test, expect } from '@playwright/test';
import { ensureDrawerOpen, frameFixture, installFrameSocket } from './helpers/frameSocket.js';
import { drawPassiveEvidence } from '../src/renderer/layers.js';

test('passive bearing envelope uses configured capability rather than a measured range', () => {
  const arcs = [];
  const ctx = new Proxy({ arc: (...args) => arcs.push(args) }, {
    get: (target, key) => target[key] || (() => {}),
  });
  drawPassiveEvidence(ctx, [{ kind: 'passive_bearing', observer_position: [2, 3], bearing_deg: 0 }], 10, 0, 0, 0, 4);
  expect(arcs[0][2]).toBe(40);
});

test.beforeEach(async ({ page }) => {
  await page.route('**/api/export/capabilities', route => route.fulfill({ json: { mp4: false } }));
});

const vessel = { scenario_entity_id: 'vessel-ii', revision: 4, position: [12, 8], vessel_class: 'type_ii', ais_enabled: true, ais_controllable: true };

async function vesselRow(page) {
  if (!(await page.getByRole('tab', { name: '船舶状态' }).count())) {
    await ensureDrawerOpen(page);
  }
  await page.getByRole('tab', { name: '船舶状态' }).click();
  return page.getByRole('row').filter({ hasText: 'vessel-ii' });
}

test('delete receipt retains identity and locks writes until authoritative absence', async ({ page }) => {
  const fixture = frameFixture('live', { scenario_vessels: [vessel] });
  await installFrameSocket(page, fixture);
  await page.route('**/api/vessels/vessel-ii', route => route.fulfill({ json: {
    command_id: route.request().postDataJSON().command_id, status: 'applied',
    vessel_id: vessel.scenario_entity_id, revision: 5,
  } }));
  await page.goto('/');
  const row = await vesselRow(page);
  await row.getByRole('button', { name: '删除 vessel-ii' }).click();
  await expect(row.locator('.vessel-row-feedback')).toContainText('船舶已删除');
  await expect(row).toBeVisible();
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeDisabled();
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, frame_id: 2, scenario_vessels: [{ ...vessel, revision: 5 }] });
  await expect(row).toBeVisible();
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeDisabled();
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, frame_id: 3, scenario_vessels: [] });
  await expect(row).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
});

test('AIS confirmation requires requested state as well as entity and revision', async ({ page }) => {
  const fixture = frameFixture('live', { scenario_vessels: [vessel] });
  await installFrameSocket(page, fixture);
  await page.route('**/api/vessels/*/ais', route => route.fulfill({ json: {
    command_id: route.request().postDataJSON().command_id, status: 'applied',
    vessel_id: vessel.scenario_entity_id, revision: 5,
  } }));
  await page.goto('/');
  const row = await vesselRow(page);
  const ais = row.getByRole('switch');
  await ais.click();
  await expect(row.locator('.vessel-row-feedback')).toContainText('AIS 已关闭');
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, frame_id: 2, scenario_vessels: [{ ...vessel, revision: 5 }] });
  await expect(ais).toBeChecked();
  await expect(ais).toBeDisabled();
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, frame_id: 3, scenario_vessels: [{ ...vessel, revision: 5, ais_enabled: false }] });
  await expect(ais).not.toBeChecked();
  await expect(ais).toBeEnabled();
});

test('create receipt stays locked through wrong identity and stale revision frames', async ({ page }) => {
  const fixture = frameFixture('live', { scenario_vessels: [] });
  await installFrameSocket(page, fixture);
  await page.route('**/api/vessels', route => route.fulfill({ json: {
    command_id: route.request().postDataJSON().command_id, status: 'applied',
    vessel_id: 'created-vessel', revision: 1,
  } }));
  await page.goto('/');
  await page.getByRole('button', { name: 'II 类船舶', exact: true }).click();
  const point = await page.evaluate(async () => {
    const { computeLayout } = await import('/src/renderer/geometry.js');
    const rect = document.querySelector('.canvas-area canvas').getBoundingClientRect();
    const layout = computeLayout(rect.width, rect.height);
    return { x: rect.x + layout.offsetX + 12.5 * layout.cellSize, y: rect.y + layout.offsetY + 8.5 * layout.cellSize };
  });
  await page.mouse.click(point.x, point.y);
  await expect(page.locator('.vessel-command-status')).toContainText('船舶已加入场景');
  for (const candidate of [vessel, { ...vessel, scenario_entity_id: 'created-vessel', revision: 0 }]) {
    await page.evaluate(f => window.__pushFrame(f), { ...fixture, scenario_vessels: [candidate] });
    await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeDisabled();
  }
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, scenario_vessels: [{ ...vessel, scenario_entity_id: 'created-vessel', revision: 1 }] });
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
});

test('reset generation cancels old poll and stale receipt cannot unlock new command', async ({ page }) => {
  const fixture = frameFixture('live', { reset_generation: 0, scenario_vessels: [vessel] });
  await installFrameSocket(page, fixture);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let posts = 0;
  await page.route('**/api/vessels/*/ais', route => route.fulfill({ json: { status: 'queued', command_id: ++posts === 1 ? 'old-reset' : 'new-reset' } }));
  await page.route('**/api/vessel-commands/old-reset', async route => {
    await gate;
    await route.fulfill({ json: { status: 'applied', command_id: 'old-reset', vessel_id: vessel.scenario_entity_id, revision: 5 } });
  });
  await page.route('**/api/vessel-commands/new-reset', route => route.fulfill({ json: { status: 'queued', command_id: 'new-reset' } }));
  await page.goto('/');
  const row = await vesselRow(page);
  const poll = page.waitForRequest('**/api/vessel-commands/old-reset');
  await row.getByRole('switch').click();
  await poll;
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, reset_generation: 1 });
  await expect(page.locator('.vessel-row-feedback')).toHaveCount(0);
  await row.getByRole('switch').click();
  release();
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, scenario_vessels: [{ ...vessel, revision: 5, ais_enabled: false }] });
  await expect(row.locator('.vessel-row-feedback')).toContainText('排队');
  await expect(row.getByRole('switch')).toBeDisabled();
});

for (const viewport of [{ width: 1440, height: 1000 }, { width: 375, height: 812 }]) {
  test(`5x5 canvas focus exposes authoritative status and owner at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const fixture = frameFixture();
    await installFrameSocket(page, fixture);
    let submitted;
    await page.route('**/api/intents', route => {
      submitted = route.request().postDataJSON();
      return route.fulfill({ json: { command_id: submitted.command_id, status: 'queued' } });
    });
    await page.route('**/api/intent-commands/*', route => route.fulfill({ json: {
      command_id: submitted.command_id, status: 'applied', intent: null, error_code: null,
    } }));
    await page.goto('/');
    await expect(page.locator('.connection-state')).toHaveClass(/connected/);
    await page.getByRole('button', { name: '框选重点区', exact: true }).click();
    const geometry = await page.evaluate(async () => {
      const { computeLayout } = await import('/src/renderer/geometry.js');
      const rect = document.querySelector('.canvas-area canvas').getBoundingClientRect();
      return { rect, layout: computeLayout(rect.width, rect.height) };
    });
    const { rect, layout } = geometry;
    await page.mouse.move(rect.x + layout.offsetX + 10.25 * layout.cellSize, rect.y + layout.offsetY + 10.25 * layout.cellSize);
    await page.mouse.down();
    await page.mouse.move(rect.x + layout.offsetX + 14.75 * layout.cellSize, rect.y + layout.offsetY + 14.75 * layout.cellSize);
    await page.mouse.up();
    await expect(page.locator('.selection-summary')).toContainText('[10, 10, 15, 15]');
    await page.getByPlaceholder('例如：东南航道').fill('5x5 focus');
    await page.getByRole('button', { name: '提交重点区' }).click();
    await expect(page.locator('.command-status')).toContainText('已应用');
    expect(submitted.bbox).toEqual([10, 10, 15, 15]);
    const intent = { ...submitted, intent_id: 'focus-5x5', revision: 1, lifecycle: 'active', expires_at_min: 121 };
    const status = { intent_id: intent.intent_id, coverage_ratio: 0, freshness_ratio: 0, assigned_task_ids: [] };
    for (const [reason, label] of [['awaiting_planning', '等待规划'], ['resource_blocked', '资源受限'], ['waiting_assignment', '等待分配']]) {
      await page.evaluate(f => window.__pushFrame(f), { ...fixture, intents: [intent], intent_statuses: [{ ...status, unmet_reason: reason }] });
      await expect(page.locator('.intent-row')).toContainText(label);
      await expect(page.locator('.intent-row')).toContainText('暂无执行任务');
    }
    await page.evaluate(f => window.__pushFrame(f), { ...fixture, intents: [intent], intent_statuses: [{
      ...status, unmet_reason: 'coverage_below_target', assigned_task_ids: [
        'search:focus:sector-alpha-001', 'search:focus:sector-bravo-002',
        'search:focus:sector-charlie-003', 'search:focus:sector-delta-004',
      ], coverage_ratio: 0.2,
    }] });
    const row = page.locator('.intent-row');
    await expect(row).toContainText('覆盖未达标');
    await expect(row).toContainText('search:focus:sector-delta-004');
    await row.scrollIntoViewIfNeeded();
    const layoutCheck = await row.evaluate(node => {
      const box = node.getBoundingClientRect();
      const labels = [...node.querySelectorAll('.intent-row-meta > span')].map(item => item.getBoundingClientRect());
      return {
        fits: node.scrollWidth <= node.clientWidth + 1,
        withinViewport: box.left >= 0 && box.right <= innerWidth,
        overlaps: labels.some((a, i) => labels.slice(i + 1).some(b => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom)),
      };
    });
    expect(layoutCheck).toEqual({ fits: true, withinViewport: true, overlaps: false });
    await page.screenshot({ path: testInfo.outputPath(`offline-focus-${viewport.width}.png`), fullPage: true });
    await page.screenshot({ path: testInfo.outputPath(`offline-focus-${viewport.width}-viewport.png`) });
  });
}

for (const operation of ['create', 'delete', 'ais']) {
  test(`${operation} frame arriving before its receipt does not unlock queued writes`, async ({ page }) => {
    const fixture = frameFixture('live', { scenario_vessels: operation === 'create' ? [] : [vessel] });
    await installFrameSocket(page, fixture);
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const endpoint = operation === 'create' ? '**/api/vessels' : operation === 'ais' ? '**/api/vessels/*/ais' : '**/api/vessels/vessel-ii';
    await page.route(endpoint, async route => {
      await gate;
      await route.fulfill({ json: {
        command_id: route.request().postDataJSON().command_id, status: 'applied',
        vessel_id: vessel.scenario_entity_id, revision: operation === 'create' ? 1 : 5,
      } });
    });
    await page.goto('/');
    const request = page.waitForRequest(endpoint);
    if (operation === 'create') {
      await page.getByRole('button', { name: 'II 类船舶', exact: true }).click();
      const point = await page.evaluate(async () => {
        const { computeLayout } = await import('/src/renderer/geometry.js');
        const rect = document.querySelector('.canvas-area canvas').getBoundingClientRect();
        const layout = computeLayout(rect.width, rect.height);
        return { x: rect.x + layout.offsetX + 12.5 * layout.cellSize, y: rect.y + layout.offsetY + 8.5 * layout.cellSize };
      });
      await page.mouse.click(point.x, point.y);
    } else {
      const row = await vesselRow(page);
      await (operation === 'ais' ? row.getByRole('switch') : row.getByRole('button', { name: '删除 vessel-ii' })).click();
    }
    await request;
    await page.evaluate(f => window.__pushFrame(f), { ...fixture, scenario_vessels: operation === 'delete' ? [] : [{
      ...vessel, revision: operation === 'create' ? 1 : 5, ais_enabled: operation !== 'ais',
    }] });
    if (operation !== 'create') {
      await expect(page.getByRole('row').filter({ hasText: 'vessel-ii' })).toHaveCount(operation === 'delete' ? 0 : 1);
    }
    if (operation === 'ais') await expect(page.getByRole('switch', { name: 'vessel-ii AIS' })).not.toBeChecked();
    await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeDisabled();
    await expect(operation === 'create' ? page.locator('.vessel-command-status') : page.locator('.vessel-row-feedback')).toContainText('排队');
    release();
    await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
  });
}

test('disconnect disables vessel writes despite a retained editable frame', async ({ page }) => {
  await installFrameSocket(page, frameFixture());
  await page.goto('/');
  const tool = page.getByRole('button', { name: 'II 类船舶', exact: true });
  await expect(tool).toBeEnabled();
  await page.evaluate(() => window.__disconnect());
  await expect(tool).toBeDisabled({ timeout: 500 });
});

test('AIS applied acknowledgement keeps writes locked until its revision arrives', async ({ page }) => {
  const fixture = frameFixture('live', { scenario_vessels: [vessel] });
  await installFrameSocket(page, fixture);
  await page.route('**/api/vessels/*/ais', route => route.fulfill({ json: { command_id: 'ais', status: 'queued' } }));
  await page.route('**/api/vessel-commands/ais', route => route.fulfill({ json: { command_id: 'ais', status: 'applied', vessel_id: vessel.scenario_entity_id, revision: 5 } }));
  await page.goto('/');
  const row = await vesselRow(page);
  await row.getByRole('switch').click();
  await expect(row.locator('.vessel-row-feedback')).toContainText('AIS 已关闭');
  await expect(row.getByRole('switch')).toBeDisabled();
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, frame_id: 2, scenario_vessels: [{ ...vessel, revision: 5, ais_enabled: false }] });
  await expect(row.getByRole('switch')).toBeEnabled();
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, frame_id: 3, scenario_vessels: [] });
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
});

test('late AIS result does not reappear after switching to replay', async ({ page }) => {
  await installFrameSocket(page, frameFixture('live', { scenario_vessels: [vessel] }));
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/replay**', route => route.fulfill({ json: { total: 1, frames: [frameFixture('replay', { scenario_vessels: [vessel] })] } }));
  await page.route('**/api/replay/list', route => route.fulfill({ json: { files: ['read-only.jsonl'] } }));
  await page.route('**/api/vessels/*/ais', async route => { await gate; await route.fulfill({ json: { command_id: 'late', status: 'queued' } }); });
  await page.route('**/api/vessel-commands/late', route => route.fulfill({ json: { status: 'applied' } }));
  await page.goto('/');
  await (await vesselRow(page)).getByRole('switch').click();
  const aborted = page.waitForEvent('requestfailed', request => request.url().endsWith('/ais'));
  await page.getByRole('button', { name: '回放', exact: true }).click();
  await aborted;
  await page.getByLabel('选择回放文件').selectOption('read-only.jsonl');
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeDisabled();
  release();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator('.vessel-row-feedback')).toHaveCount(0);
  await page.getByRole('button', { name: '直播', exact: true }).click();
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
  await expect(page.locator('.vessel-row-feedback')).toHaveCount(0);
});

test('search domain uses authoritative mask and labels missing legacy domain', async ({ page }, testInfo) => {
  await installFrameSocket(page, frameFixture());
  await page.goto('/');
  await expect(page.getByLabel('搜索域说明')).toContainText('域数据缺失');
  const domain = { cols: 30, rows: 30, cell_size_km: 10, searchable_cells: [[5, 0], [5, 1]], excluded_cells: [[4, 0]], area_km2: 200 };
  await page.evaluate(d => window.__pushFrame({ ...window.__lastFixture, frame_id: 2, search_domain: d }), domain);
  await expect(page.getByLabel('搜索域说明')).toContainText('200 km²');
  const drawing = await page.evaluate(async d => {
    const { drawSearchDomain } = await import('/src/renderer/layers.js');
    const fills = [], lines = [];
    const ctx = new Proxy({}, { get: (_, key) => (...args) => { if (key === 'fillRect') fills.push(args); if (key === 'lineTo') lines.push(args); }, set: () => true });
    drawSearchDomain(ctx, d, 10, 0, 0);
    return { fills, lines };
  }, domain);
  expect(drawing.fills).toContainEqual([40, 0, 10, 10]);
  expect(drawing.lines.length).toBe(7); // Six outer edges plus one excluded-cell hatch.
  const included = [], excluded = [];
  for (let col = 0; col < 30; col += 1) for (let row = 0; row < 30; row += 1) {
    (col < 5 ? excluded : included).push([col, row]);
  }
  await page.evaluate(d => window.__pushFrame({ ...window.__lastFixture, frame_id: 3, search_domain: d }),
    { ...domain, searchable_cells: included, excluded_cells: excluded, area_km2: 75000 });
  await expect(page.getByLabel('搜索域说明')).toContainText('75000 km²');
  await expect(page.getByText('750 CELLS', { exact: true })).toBeVisible();
  await expect(page.locator('.situation-strip')).toContainText('750');
  await page.screenshot({ path: testInfo.outputPath('search-domain.png') });
  await page.evaluate(() => window.__pushFrame({ ...window.__lastFixture, frame_id: 4, search_domain: null }));
  await expect(page.getByLabel('搜索域说明')).toContainText('域数据缺失');
});

test('full task area has no excluded shading and counts all 900 cells', async ({ page }, testInfo) => {
  const cells = Array.from({ length: 900 }, (_, i) => [Math.floor(i / 30), i % 30]);
  const domain = { cols: 30, rows: 30, cell_size_km: 10, searchable_cells: cells, excluded_cells: [], area_km2: 90000 };
  await installFrameSocket(page, frameFixture('live', { search_domain: domain }));
  await page.goto('/');
  await expect(page.getByLabel('搜索域说明')).toHaveText('全任务区域 90000 km² · 全部网格计入侦察覆盖');
  await expect(page.getByText('900 CELLS', { exact: true })).toBeVisible();
  const fills = await page.evaluate(async d => {
    const { drawSearchDomain } = await import('/src/renderer/layers.js');
    const fills = [];
    const ctx = new Proxy({}, { get: (_, key) => (...args) => { if (key === 'fillRect') fills.push(args); }, set: () => true });
    drawSearchDomain(ctx, d, 10, 0, 0);
    return fills;
  }, domain);
  expect(fills).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('full-task-area.png') });
});


test('palette drop sends spawn coordinates and selection mode cannot swallow placement', async ({ page }) => {
  await installFrameSocket(page, frameFixture());
  const commands = [];
  await page.route('**/api/vessels', route => {
    commands.push(route.request().postDataJSON());
    return route.fulfill({ json: { status: 'applied', command_id: commands.at(-1).command_id, vessel_id: 'dropped-vessel', revision: 1 } });
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
  await page.evaluate(async () => {
    const { computeLayout } = await import('/src/renderer/geometry.js');
    const canvas = document.querySelector('.canvas-area canvas');
    const rect = canvas.getBoundingClientRect();
    const layout = computeLayout(rect.width, rect.height);
    const transfer = new DataTransfer();
    document.querySelector('[aria-label="II 类船舶"]').dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: transfer }));
    canvas.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: transfer,
      clientX: rect.x + layout.offsetX + layout.cellSize * 12.5,
      clientY: rect.y + layout.offsetY + layout.cellSize * 8.5 }));
  });
  await expect.poll(() => commands.length).toBe(1);
  expect(commands[0]).toMatchObject({ episode_id: 'episode-browser', vessel_class: 'type_ii', position_cells: [12.5, 8.5] });
  await expect(page.locator('.vessel-command-status')).toContainText('船舶已加入场景');
  await page.evaluate(() => window.__pushFrame({ ...window.__lastFixture, scenario_vessels: [{
    scenario_entity_id: 'dropped-vessel', revision: 1, position: [12.5, 8.5],
    vessel_class: 'type_ii', ais_enabled: true, ais_controllable: true,
  }] }));
  await page.getByRole('button', { name: '框选重点区', exact: true }).click();
  await page.getByRole('button', { name: 'II 类船舶', exact: true }).click();
  await expect(page.getByRole('button', { name: '框选重点区', exact: true })).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.uav-section')).toContainText('红方');
  await expect(page.locator('.vessel-editor')).toContainText('蓝方');
});

test('queued AIS remains locked beyond three seconds until a terminal result', async ({ page }) => {
  await installFrameSocket(page, frameFixture('live', { scenario_vessels: [vessel] }));
  let submittedAt;
  let finish = false;
  let polls = 0;
  await page.route('**/api/vessels/*/ais', route => {
    submittedAt = Date.now();
    return route.fulfill({ json: { status: 'queued', command_id: 'slow' } });
  });
  await page.route('**/api/vessel-commands/slow', route => {
    polls += 1;
    return route.fulfill({ json: { status: finish ? 'applied' : 'queued', command_id: 'slow' } });
  });
  await page.goto('/');
  const row = await vesselRow(page);
  await row.getByRole('switch').click();
  await expect.poll(() => Date.now() - submittedAt, { timeout: 6000 }).toBeGreaterThan(3300);
  await expect(row.locator('.vessel-row-feedback')).toContainText('排队');
  await expect(row.getByRole('switch')).toBeDisabled();
  finish = true;
  await expect(row.locator('.vessel-row-feedback')).toContainText('AIS 已关闭');
  expect(polls).toBeGreaterThan(1);
});

test('poll network failure is unknown and locked, then recovers to terminal status', async ({ page }) => {
  await installFrameSocket(page, frameFixture('live', { scenario_vessels: [vessel] }));
  let recover = false;
  await page.route('**/api/vessels/*/ais', route => route.fulfill({ json: { status: 'queued', command_id: 'network' } }));
  await page.route('**/api/vessel-commands/network', route => recover
    ? route.fulfill({ json: { status: 'rejected', command_id: 'network', error_code: 'revision_conflict' } })
    : route.abort('failed'));
  await page.goto('/');
  const row = await vesselRow(page);
  await row.getByRole('switch').click();
  await expect(row.locator('.vessel-row-feedback')).toContainText('结果未知');
  await expect(row.getByRole('switch')).toBeDisabled();
  recover = true;
  await expect(row.locator('.vessel-row-feedback')).toContainText('revision_conflict');
  await expect(row.getByRole('switch')).toBeEnabled();
});


test('lost POST response queries the same command without resubmitting', async ({ page }) => {
  await installFrameSocket(page, frameFixture('live', { scenario_vessels: [vessel] }));
  let postedId;
  let posts = 0;
  let recover = false;
  const queried = [];
  await page.route('**/api/vessels/*/ais', route => {
    posts += 1;
    postedId = route.request().postDataJSON().command_id;
    return route.abort('failed');
  });
  await page.route('**/api/vessel-commands/*', route => {
    queried.push(route.request().url().split('/').at(-1));
    return recover ? route.fulfill({ json: { status: 'applied', command_id: postedId } }) : route.abort('failed');
  });
  await page.goto('/');
  const row = await vesselRow(page);
  await row.getByRole('switch').click();
  await expect(row.locator('.vessel-row-feedback')).toContainText('结果未知');
  await expect(row.getByRole('switch')).toBeDisabled();
  recover = true;
  await expect(row.locator('.vessel-row-feedback')).toContainText('AIS 已关闭');
  expect(posts).toBe(1);
  expect(new Set(queried)).toEqual(new Set([postedId]));
});

test('episode change aborts an in-flight command poll and clears its lock', async ({ page }) => {
  const fixture = frameFixture('live', { scenario_vessels: [vessel] });
  await installFrameSocket(page, fixture);
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/vessels/*/ais', route => route.fulfill({ json: { status: 'queued', command_id: 'old-episode' } }));
  await page.route('**/api/vessel-commands/old-episode', async route => {
    await gate;
    await route.fulfill({ json: { status: 'applied' } });
  });
  await page.goto('/');
  const row = await vesselRow(page);
  const poll = page.waitForRequest('**/api/vessel-commands/old-episode');
  await row.getByRole('switch').click();
  await poll;
  const aborted = page.waitForEvent('requestfailed', request => request.url().endsWith('/old-episode'));
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, episode_id: 'new-episode', frame_id: 1 });
  await aborted;
  release();
  await expect(page.locator('.vessel-row-feedback')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
});
