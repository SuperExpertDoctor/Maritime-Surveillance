import { test, expect } from '@playwright/test';
import { frameFixture, installFrameSocket } from './helpers/frameSocket.js';
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
  await page.getByRole('button', { name: /vessel-ii/ }).click();
  await page.getByRole('button', { name: '关闭 AIS' }).click();
  await expect(page.locator('.vessel-command-status')).toContainText('AIS 已关闭');
  await expect(page.getByRole('button', { name: '关闭 AIS' })).toBeDisabled();
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, frame_id: 2, scenario_vessels: [{ ...vessel, revision: 5, ais_enabled: false }] });
  await expect(page.getByRole('button', { name: '开启 AIS' })).toBeEnabled();
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
  await page.getByRole('button', { name: /vessel-ii/ }).click();
  await page.getByRole('button', { name: '关闭 AIS' }).click();
  const aborted = page.waitForEvent('requestfailed', request => request.url().endsWith('/ais'));
  await page.getByRole('button', { name: '回放', exact: true }).click();
  await aborted;
  await page.getByLabel('选择回放文件').selectOption('read-only.jsonl');
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeDisabled();
  release();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator('.vessel-command-status')).toHaveCount(0);
  await page.getByRole('button', { name: '直播', exact: true }).click();
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
  await expect(page.locator('.vessel-command-status')).toHaveCount(0);
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


test('palette drop sends spawn coordinates and selection mode cannot swallow placement', async ({ page }) => {
  await installFrameSocket(page, frameFixture());
  const commands = [];
  await page.route('**/api/vessels', route => {
    commands.push(route.request().postDataJSON());
    return route.fulfill({ json: { status: 'applied', command_id: commands.at(-1).command_id } });
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
  await page.getByRole('button', { name: /vessel-ii/ }).click();
  await page.getByRole('button', { name: '关闭 AIS' }).click();
  await expect.poll(() => Date.now() - submittedAt, { timeout: 6000 }).toBeGreaterThan(3300);
  await expect(page.locator('.vessel-command-status')).toContainText('排队');
  await expect(page.getByRole('button', { name: '关闭 AIS' })).toBeDisabled();
  finish = true;
  await expect(page.locator('.vessel-command-status')).toContainText('AIS 已关闭');
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
  await page.getByRole('button', { name: /vessel-ii/ }).click();
  await page.getByRole('button', { name: '关闭 AIS' }).click();
  await expect(page.locator('.vessel-command-status')).toContainText('结果未知');
  await expect(page.getByRole('button', { name: '关闭 AIS' })).toBeDisabled();
  recover = true;
  await expect(page.locator('.vessel-command-status')).toContainText('revision_conflict');
  await expect(page.getByRole('button', { name: '关闭 AIS' })).toBeEnabled();
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
  await page.getByRole('button', { name: /vessel-ii/ }).click();
  await page.getByRole('button', { name: '关闭 AIS' }).click();
  await expect(page.locator('.vessel-command-status')).toContainText('结果未知');
  await expect(page.getByRole('button', { name: '关闭 AIS' })).toBeDisabled();
  recover = true;
  await expect(page.locator('.vessel-command-status')).toContainText('AIS 已关闭');
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
  await page.getByRole('button', { name: /vessel-ii/ }).click();
  const poll = page.waitForRequest('**/api/vessel-commands/old-episode');
  await page.getByRole('button', { name: '关闭 AIS' }).click();
  await poll;
  const aborted = page.waitForEvent('requestfailed', request => request.url().endsWith('/old-episode'));
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, episode_id: 'new-episode', frame_id: 1 });
  await aborted;
  release();
  await expect(page.locator('.vessel-command-status')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'II 类船舶', exact: true })).toBeEnabled();
});
