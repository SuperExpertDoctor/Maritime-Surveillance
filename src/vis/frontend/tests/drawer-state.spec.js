import { expect, test } from '@playwright/test';
import { frameFixture, installFrameSocket } from './helpers/frameSocket.js';
// Explicit browser fixtures for asynchronous boundaries; production smoke uses no mocks.
test('episode reset clears decision, events and AIS; WS bursts retain events', async ({ page }) => {
  await page.route('**/api/export/capabilities', route => route.fulfill({ json: { mp4: false } }));
  await page.route('**/api/model-calls?*', route => route.fulfill({ json: { episode_id: 'episode-browser', calls: [] } }));
  await installFrameSocket(page, frameFixture('live', {
    llm_cycle: { model: 'FIXTURE', success: true, decision_summary: 'OLD EPISODE DECISION' },
    events: [{ time: 0, type: 'OLD_EVENT', data: {} }],
  }));
  await page.goto('/');
  await page.getByRole('button', { name: '切换任务详情面板' }).click();
  const drawer = page.getByRole('region', { name: '任务详情' });
  await drawer.getByRole('tab', { name: '模型日志' }).click();
  await expect(drawer).toContainText('OLD EPISODE DECISION');
  const next = frameFixture('live', { episode_id: 'FIXTURE-new', contacts: [], llm_cycle: null, events: [] });
  await page.evaluate(frame => window.__pushFrame(frame), next);
  await drawer.getByRole('tab', { name: '模型日志' }).click();
  await expect(drawer).not.toContainText('OLD EPISODE DECISION');
  await drawer.getByRole('tab', { name: '时间线' }).click();
  await expect(drawer).not.toContainText('OLD_EVENT');
  await page.evaluate(frame => {
    window.__pushFrame({ ...frame, frame_id: 2, events: [{ time: 2, type: 'BURST_A', data: {} }] });
    window.__pushFrame({ ...frame, frame_id: 3, events: [{ time: 3, type: 'BURST_B', data: {} }] });
  }, next);
  await expect(drawer).toContainText('BURST_A');
  await expect(drawer).toContainText('BURST_B');
  await drawer.getByRole('tab', { name: 'AIS', exact: true }).click();
  await expect(drawer).toContainText('No AIS contacts in this frame');
});
