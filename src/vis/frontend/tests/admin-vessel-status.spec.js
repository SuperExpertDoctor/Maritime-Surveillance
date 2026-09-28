import { expect, test } from '@playwright/test';
import { frameFixture, installFrameSocket } from './helpers/frameSocket.js';

const typeI = { scenario_entity_id: 'ship-i', revision: 2, vessel_class: 'type_i',
  position: [4.5, 7.5], ais_enabled: true, ais_controllable: false,
  speed_kn: 9, heading_deg: 40 };
const typeII = { scenario_entity_id: 'ship-ii', revision: 4, vessel_class: 'type_ii',
  position: [12.5, 8.5], ais_enabled: true, ais_controllable: true,
  speed_kn: 13, heading_deg: 72, motion_parameters: { speed_kn: 13, heading_offset_deg: 12,
    zigzag_heading_deg: 8, zigzag_period_min: 4, phase_deg: 90 },
  motion_reason_content: 'Avoid nearby UAV', motion_decision_time_min: 5 };

test.beforeEach(async ({ page }) => {
  await page.route('**/api/export/capabilities', route => route.fulfill({ json: { mp4: false } }));
  await page.route('**/api/runtime/decisions?*', route => route.fulfill({ json: { decisions: [] } }));
  await page.route('**/api/runtime/logs?*', route => route.fulfill({ json: { entries: [], cursor: 0 } }));
  await page.route('**/api/model-calls?*', route => route.fulfill({ json: { episode_id: 'episode-browser', calls: [] } }));
});

test('admin table controls each ship by its own ID and revision', async ({ page }, testInfo) => {
  const fixture = frameFixture('live', { scenario_vessels: [typeI, typeII] });
  await installFrameSocket(page, fixture);
  const requests = [];
  await page.route('**/api/vessels/*/ais', route => {
    requests.push({ url: route.request().url(), body: route.request().postDataJSON() });
    return route.fulfill({ json: { status: 'applied', command_id: requests.at(-1).body.command_id,
      vessel_id: 'ship-ii', revision: 5 } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  await page.getByRole('tab', { name: '船舶状态' }).click();
  const table = page.getByRole('table', { name: '船舶状态' });
  const rowI = table.getByRole('row', { name: /ship-i\s/ });
  const rowII = table.getByRole('row', { name: /ship-ii\s/ });
  await expect(table.getByRole('row')).toHaveCount(3);
  await expect(rowI.getByRole('switch')).toBeDisabled();
  await expect(rowII).toContainText('Avoid nearby UAV');
  await expect(rowII).toContainText('12.5, 8.5');
  await expect(page.locator('.scenario-vessel-list')).toHaveCount(0);
  await expect(page.locator('.selected-vessel-detail')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('vessels-desktop.png') });
  await expect(rowII.getByRole('switch')).toBeEnabled();
  await rowII.getByRole('switch').click();
  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0].url).toContain('/api/vessels/ship-ii/ais');
  expect(requests[0].body).toMatchObject({ expected_revision: 4, ais_enabled: false });
  await expect(rowII.getByRole('switch')).toBeChecked();
  await page.evaluate(frame => window.__pushFrame(frame), {
    ...fixture, frame_id: 2, scenario_vessels: [typeI, { ...typeII, revision: 5, ais_enabled: false }],
  });
  await expect(rowII.getByRole('switch')).not.toBeChecked();
});

test('row deletion waits for authoritative absence', async ({ page }) => {
  const fixture = frameFixture('live', { scenario_vessels: [typeI, typeII] });
  await installFrameSocket(page, fixture);
  let deleted;
  await page.route('**/api/vessels/ship-ii', route => {
    deleted = route.request().postDataJSON();
    return route.fulfill({ json: { command_id: deleted.command_id, status: 'applied',
      vessel_id: 'ship-ii', revision: 5 } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  await page.getByRole('tab', { name: '船舶状态' }).click();
  const row = page.getByRole('table', { name: '船舶状态' }).getByRole('row', { name: /ship-ii\s/ });
  await row.getByRole('button', { name: '删除 ship-ii' }).click();
  await expect.poll(() => deleted?.expected_revision).toBe(4);
  await expect(row).toHaveCount(1);
  await expect(row.getByRole('button', { name: '删除 ship-ii' })).toBeDisabled();
  await page.evaluate(frame => window.__pushFrame(frame), {
    ...fixture, frame_id: 2, scenario_vessels: [typeI],
  });
  await expect(row).toHaveCount(0);
});

test('mobile ship table scrolls inside the drawer', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installFrameSocket(page, frameFixture('live', { scenario_vessels: [typeI, typeII] }));
  await page.goto('/');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  await page.getByRole('tab', { name: '船舶状态' }).click();
  const dimensions = await page.evaluate(() => {
    const wrap = document.querySelector('.vessel-table-wrap');
    return { page: document.documentElement.scrollWidth, viewport: innerWidth,
      table: wrap.scrollWidth, container: wrap.clientWidth };
  });
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport);
  expect(dimensions.table).toBeGreaterThan(dimensions.container);
  await page.screenshot({ path: testInfo.outputPath('vessels-mobile.png') });
});

test('rejected AIS command shows row error without changing confirmed state', async ({ page }) => {
  await installFrameSocket(page, frameFixture('live', { scenario_vessels: [typeI, typeII] }));
  await page.route('**/api/vessels/*/ais', route => route.fulfill({
    status: 409, json: { error_code: 'revision_conflict' },
  }));
  await page.goto('/');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  await page.getByRole('tab', { name: '船舶状态' }).click();
  const row = page.getByRole('table', { name: '船舶状态' }).getByRole('row', { name: /ship-ii\s/ });
  await row.getByRole('switch').click();
  await expect(row).toContainText('revision_conflict');
  await expect(row.getByRole('switch')).toBeChecked();
  await expect(row.getByRole('switch')).toBeEnabled();
});

test('legacy replay table remains read-only without motion fields', async ({ page }) => {
  await installFrameSocket(page, frameFixture());
  await page.route('**/api/replay/list', route => route.fulfill({ json: { files: ['old-run.jsonl'] } }));
  await page.route('**/api/replay?*', route => route.fulfill({ json: {
    total: 1, frames: [frameFixture('replay', { scenario_vessels: [
      { scenario_entity_id: 'old-ii', revision: 1, vessel_class: 'type_ii',
        position: [9, 12], speed_kn: null, heading_deg: null,
        ais_enabled: false, ais_controllable: true },
    ] })],
  } }));
  await page.goto('/');
  await page.locator('.mode-switch button').nth(1).click();
  await page.locator('.file-select').selectOption('old-run.jsonl');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  await page.getByRole('tab', { name: '船舶状态' }).click();
  const row = page.getByRole('table', { name: '船舶状态' }).getByRole('row', { name: /old-ii\s/ });
  await expect(row).toContainText('无当前机动指令');
  await expect(row).toContainText('- kn · -°');
  await expect(row.getByRole('switch')).toBeDisabled();
  await expect(row.getByRole('button', { name: '删除 old-ii' })).toBeDisabled();
});

test('long adjustment reasons expand without enlarging every row', async ({ page }) => {
  const reason = 'Adjust course after detection. '.repeat(38);
  await installFrameSocket(page, frameFixture('live', { scenario_vessels: [
    { ...typeII, motion_reason_content: reason },
  ] }));
  await page.goto('/');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  await page.getByRole('tab', { name: '船舶状态' }).click();
  const row = page.getByRole('table', { name: '船舶状态' }).getByRole('row', { name: /ship-ii\s/ });
  expect((await row.boundingBox()).height).toBeLessThan(100);
  await row.locator('summary[aria-label="展开 ship-ii 调整原因"]').click();
  await expect(row.locator('details')).toHaveAttribute('open', '');
  await expect(row).toContainText(reason);
});
