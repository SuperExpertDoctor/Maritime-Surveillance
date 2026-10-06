import { expect, test } from '@playwright/test';
import { ensureDrawerOpen, frameFixture, installFrameSocket } from './helpers/frameSocket.js';

test('episode reset clears decision rows, runtime logs, and model call details', async ({ page }) => {
  await page.route('**/api/export/capabilities', route => route.fulfill({ json: { mp4: false } }));
  await page.route('**/api/model-calls?*', route => route.fulfill({ json: {
    episode_id: new URL(route.request().url()).searchParams.get('episode_id'), calls: [],
  } }));
  await page.route('**/api/runtime/decisions?*', route => route.fulfill({ json: {
    decisions: new URL(route.request().url()).searchParams.get('episode_id') === 'episode-browser'
      ? [{ event_id: 'episode-browser:1', time: 1, type: 'allocation_decision', data: {
        time_min: 1, trigger_source: 'event', reason_content: 'OLD EPISODE DECISION',
        assignments: [], involved_uav_ids: [], status: 'failed',
      } }] : [],
  } }));
  await page.route('**/api/runtime/logs?*', route => {
    const params = new URL(route.request().url()).searchParams;
    const old = params.get('episode_id') === 'episode-browser' && params.get('after') === '0';
    return route.fulfill({ json: {
      entries: old ? [{ id: 1, source: 'llm', level: 'warning', status: 'retry', sim_time_min: 1 }] : [],
      cursor: 1,
    } });
  });
  await installFrameSocket(page, frameFixture('live', {
    llm_cycle: { model: 'FIXTURE', success: true, decision_summary: 'OLD CALL' },
  }));
  await page.goto('/');
  await ensureDrawerOpen(page);
  const drawer = page.getByRole('region', { name: '任务详情' });
  await expect(drawer.locator('.decision-table tbody tr')).toContainText('OLD EPISODE DECISION');
  await drawer.getByRole('tab', { name: '日志', exact: true }).click();
  await expect(drawer.locator('.runtime-log-row')).toHaveCount(1);
  await drawer.getByText('模型调用详情').click();
  await expect(drawer.locator('.llm-log')).toContainText('OLD CALL');

  await page.evaluate(frame => window.__pushFrame(frame), frameFixture('live', {
    episode_id: 'FIXTURE-new', frame_id: 2, llm_cycle: null,
  }));
  await drawer.getByRole('tab', { name: '决策' }).click();
  await expect(drawer.locator('.decision-table tbody tr')).toHaveCount(0);
  await drawer.getByRole('tab', { name: '日志', exact: true }).click();
  await expect(drawer.locator('.runtime-log-row')).toHaveCount(0);
  await drawer.getByText('模型调用详情').click();
  await expect(drawer.locator('.llm-log')).toHaveCount(0);
});
