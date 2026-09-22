import { test, expect } from '@playwright/test';
// Production read-only smoke: no mocks. Empty regions/calls are valid and reported.
test('production WS/HTTP and five drawer tabs', async ({ page, request }, testInfo) => {
  let frame;
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('websocket', socket => socket.on('framereceived', ({ payload }) => {
    try { const next = JSON.parse(String(payload)); if (next.frame_id != null) frame = next; } catch {}
  }));
  await page.goto('/');
  await expect.poll(() => frame?.episode_id).toBeTruthy();
  const configResponse = await request.get('/api/config');
  expect(configResponse.ok()).toBeTruthy();
  const callsResponse = await request.get(`/api/model-calls?episode_id=${encodeURIComponent(frame.episode_id)}`);
  expect(callsResponse.ok()).toBeTruthy();
  const calls = (await callsResponse.json()).calls;
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  const drawer = page.getByRole('region', { name: '任务详情' });
  await expect(drawer.getByTestId('drawer-context')).toContainText(frame.episode_id);
  const observations = {};
  for (const label of ['时间线', '区域', '模型日志', '参数', 'AIS']) {
    await drawer.getByRole('tab', { name: label, exact: true }).click();
    await expect(drawer.locator('.drawer-content')).toBeVisible();
    if (label === '参数') await expect(drawer).toContainText('sea_area_km');
    if (label === '模型日志' && calls.length) {
      await expect(drawer.getByLabel('Model call')).toBeVisible();
      await drawer.getByLabel('Model call').selectOption(calls.at(-1).call_id);
      await expect(drawer).toContainText('External provider reasoning (think)');
      await expect(drawer).toContainText(calls.at(-1).call_id);
    }
    observations[label] = await drawer.locator('.drawer-content').innerText();
    await page.screenshot({ path: testInfo.outputPath(`tab-${label}.png`), fullPage: true });
  }
  await testInfo.attach('production-observations', { contentType: 'application/json', body: JSON.stringify({
    episode_id: frame.episode_id, frame_id: frame.frame_id, runtime_status: frame.runtime_status,
    modelCallsSeen: calls.length, regions: frame.search_regions?.length, contacts: frame.contacts?.length,
    observations, errors,
  }, null, 2) });
  expect(errors).toEqual([]);
});
