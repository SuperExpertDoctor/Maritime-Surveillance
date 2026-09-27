// Real HTTP/WebSocket/UI smoke against the explicitly offline main.py preview.
const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(__dirname, '../..');
const { chromium, expect } = require(path.join(root, 'src/vis/frontend/node_modules/@playwright/test'));
const output = path.join(__dirname, `offline-smoke-${Date.now()}`);
fs.mkdirSync(output);
const frames = [];
const operations = [];
const errors = [];
const latest = () => frames.at(-1)?.frame;
const labels = {
  typeI: 'I \u7c7b\u8239\u8236', typeII: 'II \u7c7b\u8239\u8236',
  focus: '\u6846\u9009\u91cd\u70b9\u533a', submit: '\u63d0\u4ea4\u91cd\u70b9\u533a',
  aisOn: '\u5f00\u542f AIS', aisOff: '\u5173\u95ed AIS',
  remove: '\u5220\u9664\u9009\u4e2d\u8239\u8236',
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    baseURL: process.env.PREVIEW_URL || 'http://127.0.0.1:5195',
    viewport: { width: 1440, height: 1000 },
  });
  page.on('pageerror', error => errors.push(error.message));
  page.on('websocket', socket => socket.on('framereceived', event => {
    try {
      const frame = JSON.parse(String(event.payload));
      if (frame.frame_id != null) frames.push({ receivedAt: Date.now(), frame });
    } catch { /* The live socket also receives pong messages. */ }
  }));
  const receipt = async (kind, id) => {
    let result;
    await expect.poll(async () => {
      const response = await page.request.get(`/api/${kind}-commands/${encodeURIComponent(id)}`);
      expect(response.ok()).toBeTruthy();
      result = await response.json();
      return result.status;
    }, { timeout: 120000, intervals: [100, 250, 500] }).not.toBe('queued');
    expect(result, JSON.stringify(result)).toHaveProperty('status', 'applied');
    return result;
  };
  const geometry = () => page.evaluate(async () => {
    const { computeLayout } = await import('/src/renderer/geometry.js');
    const rect = document.querySelector('.canvas-area canvas').getBoundingClientRect();
    return { rect: { x: rect.x, y: rect.y }, layout: computeLayout(rect.width, rect.height) };
  });
  try {
    await page.goto(process.env.PREVIEW_URL || 'http://127.0.0.1:5195');
    await expect.poll(() => latest()?.episode_id, { timeout: 120000 }).toBeTruthy();
    const episode = latest().episode_id;
    const cleanupId = process.env.CLEANUP_VESSEL_ID;
    if (cleanupId && latest().scenario_vessels.some(v => v.scenario_entity_id === cleanupId)) {
      await page.locator('.scenario-vessel-row').filter({ hasText: cleanupId }).click();
      const responsePromise = page.waitForResponse(response => response.url().endsWith(`/api/vessels/${cleanupId}`)
        && response.request().method() === 'DELETE');
      await page.getByRole('button', { name: labels.remove, exact: true }).click();
      const queued = await (await responsePromise).json();
      await receipt('vessel', queued.command_id);
      await expect.poll(() => latest().scenario_vessels.some(v => v.scenario_entity_id === cleanupId),
        { timeout: 120000 }).toBe(false);
    }
    const create = async (name, point) => {
      const startedAt = Date.now();
      await page.getByRole('button', { name, exact: true }).click();
      const { layout } = await geometry();
      const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/vessels') && response.request().method() === 'POST');
      await page.locator('.canvas-area canvas').click({ position: {
        x: layout.offsetX + point[0] * layout.cellSize,
        y: layout.offsetY + point[1] * layout.cellSize,
      } });
      const queued = await (await responsePromise).json();
      const applied = await receipt('vessel', queued.command_id);
      await expect.poll(() => latest()?.scenario_vessels?.find(v => v.scenario_entity_id === applied.vessel_id)?.revision,
        { timeout: 120000 }).toBeGreaterThanOrEqual(applied.revision);
      await expect(page.getByRole('button', { name, exact: true })).toBeEnabled();
      operations.push({ kind: 'create', name, startedAt, confirmedAt: Date.now(), ...applied });
      return applied.vessel_id;
    };
    const first = await create(labels.typeI, [8.5, 2.5]);
    const second = await create(labels.typeII, [23.5, 22.5]);
    await page.locator('.scenario-vessel-row').filter({ hasText: second }).click();
    await expect(page.locator('.scenario-vessel-row').filter({ hasText: second })).toHaveAttribute('aria-pressed', 'true');
    const enabled = latest().scenario_vessels.find(v => v.scenario_entity_id === second).ais_enabled;
    const aisStarted = Date.now();
    const aisResponse = page.waitForResponse(response => response.url().endsWith(`/api/vessels/${second}/ais`) && response.request().method() === 'PATCH');
    await page.getByRole('button', { name: enabled ? labels.aisOff : labels.aisOn, exact: true }).click();
    const aisQueued = await (await aisResponse).json();
    const aisApplied = await receipt('vessel', aisQueued.command_id);
    await expect.poll(() => latest()?.scenario_vessels?.find(v => v.scenario_entity_id === second)?.ais_enabled,
      { timeout: 120000 }).toBe(!enabled);
    await expect(page.getByRole('button', { name: labels.typeII, exact: true })).toBeEnabled();
    operations.push({ kind: 'ais', startedAt: aisStarted, confirmedAt: Date.now(), ...aisApplied });
    for (const id of [second, first]) {
      const row = page.locator('.scenario-vessel-row').filter({ hasText: id });
      if (await row.getAttribute('aria-pressed') !== 'true') await row.click();
      await expect(row).toHaveAttribute('aria-pressed', 'true');
      const startedAt = Date.now();
      const responsePromise = page.waitForResponse(response => response.url().endsWith(`/api/vessels/${id}`) && response.request().method() === 'DELETE');
      await page.getByRole('button', { name: labels.remove, exact: true }).click();
      const queued = await (await responsePromise).json();
      const applied = await receipt('vessel', queued.command_id);
      await expect.poll(() => latest()?.scenario_vessels?.some(v => v.scenario_entity_id === id), { timeout: 120000 }).toBe(false);
      await expect(row).toHaveCount(0);
      await expect(page.getByRole('button', { name: labels.typeII, exact: true })).toBeEnabled();
      operations.push({ kind: 'delete', startedAt, confirmedAt: Date.now(), ...applied });
    }
    const region = latest().search_regions?.find(region => Array.isArray(region.bbox));
    const col = Math.max(0, Math.min(25, region?.bbox[0] ?? 10));
    const row = Math.max(0, Math.min(25, region?.bbox[1] ?? 10));
    const bbox = [col, row, col + 5, row + 5];
    await page.getByRole('button', { name: labels.focus, exact: true }).click();
    const { rect, layout } = await geometry();
    await page.mouse.move(rect.x + layout.offsetX + (col + 0.25) * layout.cellSize, rect.y + layout.offsetY + (row + 0.25) * layout.cellSize);
    await page.mouse.down();
    await page.mouse.move(rect.x + layout.offsetX + (col + 4.75) * layout.cellSize, rect.y + layout.offsetY + (row + 4.75) * layout.cellSize);
    await page.mouse.up();
    await expect(page.locator('.selection-summary')).toContainText(`[${bbox.join(', ')}]`);
    await page.getByPlaceholder('\u4f8b\u5982\uff1a\u4e1c\u5357\u822a\u9053').fill('offline-smoke-5x5');
    const focusStarted = Date.now();
    const focusResponse = page.waitForResponse(response => response.url().endsWith('/api/intents') && response.request().method() === 'POST');
    await page.getByRole('button', { name: labels.submit, exact: true }).click();
    const focusQueued = await (await focusResponse).json();
    const focusApplied = await receipt('intent', focusQueued.command_id);
    const intentId = focusApplied.intent.intent_id;
    await expect(page.locator('.intent-row').filter({ hasText: intentId })).toContainText('offline-smoke-5x5');
    let reviewed;
    await expect.poll(() => {
      const requests = fs.readFileSync(process.env.PREVIEW_REQUEST_LOG, 'utf8').trim().split('\n');
      reviewed = requests.flatMap(line => { try { return [JSON.parse(line)]; } catch { return []; } })
        .find(call => call.episode_id === episode && call.role === 'decision_maker' && call.success
          && call.user_payload.snapshot?.pending_intent_reviews?.some(intent => intent.intent_id === intentId));
      return Boolean(reviewed && frames.some(({ frame }) => frame.llm_cycle?.call_id === reviewed.call_id
        && frame.llm_cycle.success === true));
    }, { timeout: 180000, intervals: [250, 500, 1000] }).toBe(true);
    operations.push({ kind: 'focus', bbox, startedAt: focusStarted, confirmedAt: Date.now(), ...focusApplied });
    expect(new Set(frames.map(({ frame }) => frame.episode_id))).toEqual(new Set([episode]));
    expect(errors).toEqual([]);
    const pixels = await page.evaluate(() => {
      const canvas = document.querySelector('.canvas-area canvas');
      const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      const colors = new Set();
      for (let i = 0; i < data.length; i += 4096) colors.add(`${data[i]},${data[i + 1]},${data[i + 2]},${data[i + 3]}`);
      return { width: canvas.width, height: canvas.height, sampledColors: colors.size };
    });
    expect(pixels.sampledColors).toBeGreaterThan(10);
    await page.locator('.intent-row').filter({ hasText: intentId }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'desktop.png') });
    await page.setViewportSize({ width: 375, height: 812 });
    await page.locator('.intent-row').filter({ hasText: intentId }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'mobile.png') });
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ mode: 'offline-fixture-real-main-ui', episode, operations, pixels, modelReview: reviewed, frameCount: frames.length, errors }, null, 2));
    console.log(JSON.stringify({ output, episode, operations: operations.length, pixels, errors }));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png') }).catch(() => {});
    fs.writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ error: String(error), operations, latestFrame: latest(), errors }, null, 2));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
