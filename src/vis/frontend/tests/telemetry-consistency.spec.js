import { expect, test } from '@playwright/test';
import { frameFixture, installFrameSocket } from './helpers/frameSocket.js';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/export/capabilities', route => route.fulfill({ json: { mp4: false } }));
});

test('contact observation count uses the total rather than the live sample tail', async ({ page }) => {
  const fixture = frameFixture();
  const contact = { ...fixture.contacts[0], sample_count: 57 };
  contact.samples = Array.from({ length: 12 }, (_, i) => ({ ...contact.samples[0], sample_id: `S${i}` }));
  await installFrameSocket(page, { ...fixture, contacts: [contact] });
  await page.goto('/');
  await expect(page.locator('.contact-seen')).toHaveText('57');
  await expect(page.locator('.contact-detail dl div').filter({ hasText: '观测次数' })).toContainText('57');
  await page.evaluate(c => window.__pushFrame({ ...window.__lastFixture, frame_id: 2, contacts: [c] }), { ...contact, sample_count: 65 });
  await expect(page.locator('.contact-seen')).toHaveText('65');
});

test('stale model log HTTP response cannot hide newer pushed calls', async ({ page }) => {
  const oldCall = { call_id: 'old-call', role: 'decision_maker', sim_time_min: 1, success: true, decision_summary: 'OLD CALL' };
  const newCall = { call_id: 'new-call', role: 'decision_maker', sim_time_min: 2, success: true, decision_summary: 'NEW CALL' };
  await installFrameSocket(page, frameFixture('live', { model_calls: [oldCall] }));
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/model-calls?*', async route => {
    await gate;
    await route.fulfill({ json: { episode_id: 'episode-browser', calls: [oldCall] } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  const requested = page.waitForRequest('**/api/model-calls?*');
  await page.getByRole('tab', { name: '模型日志' }).click();
  await requested;
  await page.evaluate(call => window.__pushFrame({ ...window.__lastFixture, frame_id: 2, model_calls: [call] }), newCall);
  await expect(page.locator('.llm-log')).toContainText('NEW CALL');
  const response = page.waitForResponse('**/api/model-calls?*');
  release();
  await response;
  await expect(page.getByLabel('Model call').locator('option')).toHaveCount(2);
  await expect(page.locator('.llm-log')).toContainText('NEW CALL');
});

test('completed polled model call is not reverted by a pending frame snapshot', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const pending = { call_id: 'same-call', role: 'decision_maker', sim_time_min: 1, success: false, attempts: [] };
  await installFrameSocket(page, frameFixture('live', { model_calls: [pending] }));
  await page.route('**/api/model-calls?*', route => route.fulfill({ json: {
    episode_id: 'episode-browser', calls: [{ ...pending, success: true, decision_summary: 'COMPLETED RESULT' }],
  } }));
  await page.goto('/');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  await page.getByRole('tab', { name: '模型日志' }).click();
  await expect(page.locator('.llm-log')).toContainText('COMPLETED RESULT');
  await page.evaluate(() => window.__pushFrame({ ...window.__lastFixture, frame_id: 2 }));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.locator('.llm-log')).toContainText('COMPLETED RESULT');
  await page.route('**/api/replay/list', route => route.fulfill({ json: { files: [] } }));
  await page.getByRole('button', { name: '回放', exact: true }).click();
  await expect(page.getByRole('region', { name: '任务详情' })).toContainText('not provided');
  await page.getByRole('button', { name: '直播', exact: true }).click();
  await expect(page.locator('.connection-state')).toContainText('实时连接');
  await page.getByRole('tab', { name: '模型日志' }).click();
  await expect(page.locator('.llm-log')).toContainText('COMPLETED RESULT');
  expect(errors).toEqual([]);
});

test('disconnection disables focus area and runtime mutations', async ({ page }) => {
  await installFrameSocket(page, frameFixture('live', {
    runtime_status: 'paused_model',
    intents: [{ intent_id: 'I1', revision: 1, lifecycle: 'active', label: 'Focus', bbox: [5, 5, 10, 10], expires_at_min: 120 }],
  }));
  await page.goto('/');
  await page.getByRole('button', { name: '编辑 Focus' }).click();
  await expect(page.getByRole('button', { name: '提交修改' })).toBeEnabled();
  await page.evaluate(() => window.__disconnect());
  for (const name of ['提交修改', '编辑 Focus', '取消 Focus', '重试', '结束回合']) {
    await expect(page.getByRole('button', { name, exact: true })).toBeDisabled();
  }
});

test('queued runtime commands prevent duplicate submissions', async ({ page }) => {
  await installFrameSocket(page, frameFixture('live', { runtime_status: 'paused_model' }));
  await page.route('**/api/runtime/retry', route => route.fulfill({ json: { command_id: 'retry-once', status: 'queued' } }));
  await page.route('**/api/intent-commands/*', route => route.fulfill({ json: { command_id: 'retry-once', status: 'queued' } }));
  await page.goto('/');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(page.locator('.command-note')).toContainText('待应用');
  await expect(page.getByRole('button', { name: '重试', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '结束回合', exact: true })).toBeDisabled();
});

for (const operation of ['retry', 'cancel']) {
  test(`lost ${operation} response queries the original command and keeps controls locked`, async ({ page }) => {
    await installFrameSocket(page, frameFixture('live', {
      runtime_status: 'paused_model',
      intents: [{ intent_id: 'I1', revision: 1, lifecycle: 'active', label: 'Focus', bbox: [5, 5, 10, 10] }],
    }));
    let commandId;
    let writes = 0;
    const endpoint = operation === 'retry' ? '**/api/runtime/retry' : '**/api/intents/I1';
    await page.route(endpoint, route => {
      writes += 1;
      commandId = route.request().postDataJSON().command_id;
      return route.abort('failed');
    });
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    await page.route('**/api/intent-commands/*', async route => {
      expect(route.request().url()).toContain(commandId);
      await gate;
      await route.fulfill({ json: { command_id: commandId, status: 'applied' } });
    });
    await page.goto('/');
    const button = page.getByRole('button', { name: operation === 'retry' ? '重试' : '取消 Focus', exact: true });
    await button.click();
    const status = page.locator(operation === 'retry' ? '.command-note' : '.command-status');
    await expect(status).toContainText('结果未知');
    await expect(button).toBeDisabled();
    release();
    await expect(status).toContainText('已应用');
    expect(writes).toBe(1);
  });
}

test('late telemetry cannot roll back time or restore a retired episode', async ({ page }) => {
  const fixture = frameFixture('live', { sim_time_min: 10, timestamp: '00:10:00', coverage_pct: 10 });
  await installFrameSocket(page, fixture);
  await page.goto('/');
  const coverage = page.locator('.metric').filter({ hasText: '累计观测覆盖' });
  await expect(coverage).toContainText('10.0%');
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, sim_time_min: 9, timestamp: '00:09:00', coverage_pct: 9 });
  // Synchronize with the animation frame that would render the stale payload.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(coverage).toContainText('10.0%');
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, coverage_pct: 11 });
  await expect(coverage).toContainText('11.0%');
  await page.evaluate(f => window.__pushFrame(f), { ...fixture, episode_id: 'next-episode', reset_generation: 1, sim_time_min: 0, coverage_pct: 0 });
  await expect(coverage).toContainText('0.0%');
  await page.evaluate(f => window.__pushFrame(f), fixture);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(coverage).toContainText('0.0%');
  await page.evaluate(() => {
    window.__disconnect();
    window.__pushFrame({ ...window.__lastFixture, episode_id: 'closed-socket', sim_time_min: 99, coverage_pct: 99 });
  });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(coverage).toContainText('0.0%');
});

test('replay waits for a slow chunk without skipping its first frame', async ({ page }) => {
  await installFrameSocket(page, frameFixture());
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/replay?*', async route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'));
    if (offset === 120) await gate;
    await route.fulfill({ json: { total: 240, frames: Array.from({ length: 120 }, (_, i) => frameFixture('replay', { frame_id: offset + i, sim_time_min: offset + i })) } });
  });
  await page.route('**/api/replay/list', route => route.fulfill({ json: { files: ['slow.jsonl'] } }));
  await page.goto('/');
  await page.getByRole('button', { name: '回放', exact: true }).click();
  await page.getByLabel('选择回放文件').selectOption('slow.jsonl');
  await page.getByLabel('回放时间轴').fill('119');
  await page.getByLabel('回放速度').selectOption('10');
  const chunkRequest = page.waitForRequest(request => request.url().includes('offset=120'));
  await page.getByRole('button', { name: '播放', exact: true }).click();
  await chunkRequest;
  await page.waitForTimeout(450); // More than four playback ticks while the chunk is blocked.
  await expect(page.locator('.playback-readout').first()).toHaveText('帧 120 / 240');
  await page.getByLabel('回放速度').selectOption('0.5');
  release();
  await expect(page.locator('.playback-readout').first()).toHaveText('帧 121 / 240');
  await page.getByRole('button', { name: '暂停', exact: true }).click();
});

test('map and sidebar use the frame information thresholds', async ({ page }) => {
  const matrix = Array.from({ length: 30 }, () => Array(30).fill(0.7));
  await installFrameSocket(page, frameFixture('live', { info_matrix: matrix }));
  await page.goto('/');
  await expect(page.locator('.situation.white strong')).toHaveText('900');
  await page.evaluate(() => window.__pushFrame({ ...window.__lastFixture, frame_id: 2, config_snapshot: { grid: { white_threshold: 0.8, gray_threshold: 0.3 } } }));
  await expect(page.locator('.situation.gray strong')).toHaveText('900');
  const color = await page.evaluate(async () => {
    const { drawHeatmap } = await import('/src/renderer/layers.js');
    let fill;
    const ctx = { set fillStyle(value) { fill = value; }, fillRect() {} };
    drawHeatmap(ctx, window.__lastFixture.info_matrix, [], 10, 0, 0, { white_threshold: 0.6, gray_threshold: 0.2 });
    return fill;
  });
  expect(color).toContain('13, 148, 136');
});
