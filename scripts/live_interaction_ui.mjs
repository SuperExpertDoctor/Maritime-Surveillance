import { computeLayout } from "../src/vis/frontend/src/renderer/geometry.js";

const DEFAULT_TIMEOUT_MS = 300000;

export function createFrameTracker(initialFrame = null, { onFrame } = {}) {
  let latestFrame = initialFrame;
  let latestCapture = null;
  let captureOrder = 0;
  const captures = new WeakMap();
  const waiters = new Set();

  return {
    get latest() {
      return latestFrame;
    },
    get latestCapture() {
      return latestCapture;
    },
    captureFor(frame = latestFrame) {
      if (!frame || typeof frame !== "object") return null;
      return captures.get(frame) || null;
    },
    observe(frame) {
      if (!frame || !Number.isInteger(frame.frame_id)) return;
      latestFrame = frame;
      const capture = {
        id: `${frame.episode_id ?? "episode"}:${frame.frame_id}:${++captureOrder}`,
        order: captureOrder,
      };
      captures.set(frame, capture);
      latestCapture = capture;
      try {
        const result = onFrame?.(frame, capture);
        if (result && typeof result.catch === "function") result.catch(() => {});
      } catch {
        // Evidence persistence errors are reported by the owning runner.
      }
      for (const waiter of [...waiters]) {
        let matched = false;
        try {
          matched = waiter.predicate(frame);
        } catch (error) {
          clearTimeout(waiter.timer);
          waiters.delete(waiter);
          waiter.reject(error);
          continue;
        }
        if (matched) {
          clearTimeout(waiter.timer);
          waiters.delete(waiter);
          waiter.resolve(frame);
        }
      }
    },
    waitFor(predicate, timeoutMs = DEFAULT_TIMEOUT_MS) {
      if (latestFrame && predicate(latestFrame)) return Promise.resolve(latestFrame);
      return new Promise((resolve, reject) => {
        const waiter = { predicate, resolve, reject, timer: null };
        waiter.timer = setTimeout(() => {
          waiters.delete(waiter);
          reject(new Error(`timed out waiting for authoritative frame after ${timeoutMs}ms`));
        }, timeoutMs);
        waiters.add(waiter);
      });
    },
  };
}

export function attachLiveFrames(page, tracker) {
  const onWebSocket = (socket) => {
    let path;
    try {
      path = new URL(socket.url()).pathname;
    } catch {
      return;
    }
    if (path !== "/ws/live") return;
    socket.on("framereceived", (raw) => {
      const payload = raw && typeof raw === "object" && "payload" in raw ? raw.payload : raw;
      const text = Buffer.isBuffer(payload) ? payload.toString("utf8") : payload;
      if (typeof text !== "string" || text === "pong") return;
      try {
        tracker.observe(JSON.parse(text));
      } catch {
        // The live endpoint can also send non-frame control payloads.
      }
    });
  };
  page.on("websocket", onWebSocket);
  return () => page.off("websocket", onWebSocket);
}

export async function drawFocusArea(page, tracker, {
  bbox,
  label,
  durationMin = 360,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  record,
} = {}) {
  assertFiveByFive(bbox);
  if (typeof label !== "string" || !label.trim()) throw new TypeError("focus label is required");
  const sourceFrame = await tracker.waitFor(Boolean, timeoutMs);
  const sourceCapture = tracker.captureFor(sourceFrame);
  const responseObserver = observeCommandReceipts(page, "intent");
  try {
    const command = await performUiCommand(page, responseObserver, {
      resource: "intent",
      method: "POST",
      requestPath: (path) => path === "/api/intents",
      timeoutMs,
      record,
      requestData: (body) => ({
        label: body.label,
        bbox: body.bbox,
        mode: body.mode,
        validDurationMin: body.valid_duration_min,
      }),
      action: async () => {
        const selectionButton = page.getByRole("button", { name: "框选重点区", exact: true });
        if (await selectionButton.getAttribute("aria-pressed") !== "true") {
          await selectionButton.click();
        }
        const pointData = await mapGeometry(page);
        const [x1, y1, x2, y2] = bbox;
        await page.mouse.move(
          pointData.rect.x + pointData.layout.offsetX + (x1 + 0.25) * pointData.layout.cellSize,
          pointData.rect.y + pointData.layout.offsetY + (y1 + 0.25) * pointData.layout.cellSize,
        );
        await page.mouse.down();
        await page.mouse.move(
          pointData.rect.x + pointData.layout.offsetX + (x2 - 0.25) * pointData.layout.cellSize,
          pointData.rect.y + pointData.layout.offsetY + (y2 - 0.25) * pointData.layout.cellSize,
        );
        await page.mouse.up();
        await page.getByPlaceholder("例如：东南航道").fill(label);
        const form = page.locator(".intent-form");
        await form.locator("label").filter({ hasText: /^模式/ }).locator("select")
          .selectOption("search_priority");
        await form.locator("label").filter({ hasText: /^优先级/ }).locator("select")
          .selectOption("high");
        await form.locator("label").filter({ hasText: /^有效期/ }).locator("input")
          .fill(String(durationMin));
        await page.getByRole("button", { name: "提交重点区" }).click();
      },
    });
    const intentId = command.receipt.intent?.intent_id;
    const confirmed = await tracker.waitFor((frame) => frame.intents?.some((intent) =>
      (intentId ? intent.intent_id === intentId : intent.label === label)
      && sameArray(intent.bbox, command.body.bbox)), timeoutMs);
    const intent = confirmed.intents.find((candidate) =>
      (intentId ? candidate.intent_id === intentId : candidate.label === label)
      && sameArray(candidate.bbox, command.body.bbox));
    await recordEvent(record, {
      action: "focus", stage: "confirmed", commandId: command.commandId,
      frameId: confirmed.frame_id, intentId: intent.intent_id, bbox,
      sourceFrameId: sourceFrame.frame_id,
      sourceFrameCaptureId: sourceCapture?.id,
      sourceFrameCaptureOrder: sourceCapture?.order,
      frameCaptureId: tracker.captureFor(confirmed)?.id,
      frameCaptureOrder: tracker.captureFor(confirmed)?.order,
      state: { intent }, ...stamp(),
    });
    return {
      commandId: command.commandId,
      queuedAt: command.queuedAt,
      appliedAt: command.appliedAt,
      confirmedAt: stamp(),
      frameId: confirmed.frame_id,
      state: { intent, frame: confirmed },
      sourceFrameId: sourceFrame.frame_id,
      sourceCaptureId: sourceCapture?.id,
      sourceCaptureOrder: sourceCapture?.order,
      confirmedCaptureId: tracker.captureFor(confirmed)?.id,
      confirmedCaptureOrder: tracker.captureFor(confirmed)?.order,
      body: command.body,
      receipt: command.receipt,
    };
  } finally {
    responseObserver.dispose();
  }
}

export async function selectVesselPlacement(page, tracker, vesselClass, cell, {
  timeoutMs = DEFAULT_TIMEOUT_MS,
  record,
} = {}) {
  if (!new Set(["type_i", "type_ii"]).has(vesselClass)) {
    throw new TypeError("vessel class must be type_i or type_ii");
  }
  if (!Array.isArray(cell) || cell.length !== 2 || !cell.every(Number.isInteger)) {
    throw new TypeError("placement cell must be two integer coordinates");
  }
  const sourceFrame = await tracker.waitFor(Boolean, timeoutMs);
  const sourceCapture = tracker.captureFor(sourceFrame);
  const existingIds = new Set((sourceFrame.scenario_vessels || [])
    .map((vessel) => vessel.scenario_entity_id));
  const position = [cell[0] + 0.5, cell[1] + 0.5];
  const responseObserver = observeCommandReceipts(page, "vessel");
  try {
    const command = await performUiCommand(page, responseObserver, {
      resource: "vessel",
      method: "POST",
      requestPath: (path) => path === "/api/vessels",
      timeoutMs,
      record,
      action: async () => {
        const accessibleName = vesselClass === "type_i" ? "I 类船舶" : "II 类船舶";
        await page.getByRole("button", { name: accessibleName, exact: true }).click();
        const pointData = await mapGeometry(page);
        await page.mouse.click(
          pointData.rect.x + pointData.layout.offsetX + position[0] * pointData.layout.cellSize,
          pointData.rect.y + pointData.layout.offsetY + position[1] * pointData.layout.cellSize,
        );
      },
      requestData: (body) => ({ vesselClass: body.vessel_class, position: body.position_cells }),
    });
    const confirmed = await tracker.waitFor((frame) =>
      (frame.scenario_vessels || []).some((vessel) =>
        !existingIds.has(vessel.scenario_entity_id)
        && vessel.vessel_class === vesselClass
        && positionMatches(vessel.position, position)), timeoutMs);
    const vessel = confirmed.scenario_vessels.find((candidate) =>
      !existingIds.has(candidate.scenario_entity_id)
      && candidate.vessel_class === vesselClass
      && positionMatches(candidate.position, position));
    await recordEvent(record, {
      action: "create", stage: "confirmed", commandId: command.commandId,
      frameId: confirmed.frame_id, vesselId: vessel.scenario_entity_id,
      vesselClass, position, sourceFrameId: sourceFrame.frame_id,
      sourceFrameCaptureId: sourceCapture?.id,
      sourceFrameCaptureOrder: sourceCapture?.order,
      frameCaptureId: tracker.captureFor(confirmed)?.id,
      frameCaptureOrder: tracker.captureFor(confirmed)?.order,
      state: { vessel }, ...stamp(),
    });
    return {
      commandId: command.commandId,
      queuedAt: command.queuedAt,
      appliedAt: command.appliedAt,
      confirmedAt: stamp(),
      frameId: confirmed.frame_id,
      state: confirmed,
      vessel,
      sourceCaptureId: sourceCapture?.id,
      sourceCaptureOrder: sourceCapture?.order,
      confirmedCaptureId: tracker.captureFor(confirmed)?.id,
      confirmedCaptureOrder: tracker.captureFor(confirmed)?.order,
      body: command.body,
      receipt: command.receipt,
    };
  } finally {
    responseObserver.dispose();
  }
}

export async function deleteVessel(page, tracker, vesselId, {
  timeoutMs = DEFAULT_TIMEOUT_MS,
  record,
} = {}) {
  const sourceFrame = await tracker.waitFor(Boolean, timeoutMs);
  const sourceCapture = tracker.captureFor(sourceFrame);
  const vessel = (sourceFrame.scenario_vessels || []).find((item) =>
    item.scenario_entity_id === vesselId);
  if (!vessel) throw new Error(`active vessel not found: ${vesselId}`);
  const responseObserver = observeCommandReceipts(page, "vessel");
  try {
    const command = await performUiCommand(page, responseObserver, {
      resource: "vessel",
      method: "DELETE",
      requestPath: (path) => path === `/api/vessels/${encodeURIComponent(vesselId)}`,
      timeoutMs,
      record,
      action: async () => {
        await selectVesselStatusTab(page);
        const row = vesselRow(page, vesselId);
        await row.getByRole("button", { name: `删除 ${vesselId}` }).click();
      },
      requestData: (body) => ({ vesselId, expectedRevision: body.expected_revision }),
    });
    const confirmed = await tracker.waitFor((frame) =>
      !(frame.scenario_vessels || []).some((item) => item.scenario_entity_id === vesselId), timeoutMs);
    await recordEvent(record, {
      action: "delete", stage: "confirmed", commandId: command.commandId,
      frameId: confirmed.frame_id, vesselId, vesselClass: vessel.vessel_class,
      sourceFrameId: sourceFrame.frame_id,
      sourceFrameCaptureId: sourceCapture?.id,
      sourceFrameCaptureOrder: sourceCapture?.order,
      frameCaptureId: tracker.captureFor(confirmed)?.id,
      frameCaptureOrder: tracker.captureFor(confirmed)?.order,
      state: { absent: true }, ...stamp(),
    });
    return {
      commandId: command.commandId,
      queuedAt: command.queuedAt,
      appliedAt: command.appliedAt,
      confirmedAt: stamp(),
      frameId: confirmed.frame_id,
      state: confirmed,
      vessel,
      sourceCaptureId: sourceCapture?.id,
      sourceCaptureOrder: sourceCapture?.order,
      confirmedCaptureId: tracker.captureFor(confirmed)?.id,
      confirmedCaptureOrder: tracker.captureFor(confirmed)?.order,
      body: command.body,
      receipt: command.receipt,
    };
  } finally {
    responseObserver.dispose();
  }
}

export async function setVesselAis(page, tracker, vesselId, enabled, {
  timeoutMs = DEFAULT_TIMEOUT_MS,
  record,
} = {}) {
  if (typeof enabled !== "boolean") throw new TypeError("AIS state must be boolean");
  const sourceFrame = await tracker.waitFor(Boolean, timeoutMs);
  const sourceCapture = tracker.captureFor(sourceFrame);
  const vessel = (sourceFrame.scenario_vessels || []).find((item) =>
    item.scenario_entity_id === vesselId);
  if (!vessel || vessel.vessel_class !== "type_ii" || vessel.ais_controllable !== true) {
    throw new Error(`controllable Type II vessel not found: ${vesselId}`);
  }
  if (vessel.ais_enabled === enabled) throw new Error(`AIS is already ${enabled ? "enabled" : "disabled"}: ${vesselId}`);
  const responseObserver = observeCommandReceipts(page, "vessel");
  try {
    const command = await performUiCommand(page, responseObserver, {
      resource: "vessel",
      method: "PATCH",
      requestPath: (path) => path === `/api/vessels/${encodeURIComponent(vesselId)}/ais`,
      timeoutMs,
      record,
      action: async () => {
        await selectVesselStatusTab(page);
        const toggle = vesselRow(page, vesselId).getByRole("switch", { name: `${vesselId} AIS` });
        await toggle.click();
      },
      requestData: (body) => ({ vesselId, expectedRevision: body.expected_revision, aisEnabled: body.ais_enabled }),
    });
    const confirmed = await tracker.waitFor((frame) =>
      (frame.scenario_vessels || []).some((item) => item.scenario_entity_id === vesselId
        && item.vessel_class === "type_ii" && item.ais_enabled === enabled), timeoutMs);
    await recordEvent(record, {
      action: "ais", stage: "confirmed", commandId: command.commandId,
      frameId: confirmed.frame_id, vesselId, vesselClass: "type_ii",
      aisEnabled: enabled, previousAisEnabled: vessel.ais_enabled,
      sourceFrameId: sourceFrame.frame_id,
      sourceFrameCaptureId: sourceCapture?.id,
      sourceFrameCaptureOrder: sourceCapture?.order,
      frameCaptureId: tracker.captureFor(confirmed)?.id,
      frameCaptureOrder: tracker.captureFor(confirmed)?.order,
      state: { aisEnabled: enabled }, ...stamp(),
    });
    return {
      commandId: command.commandId,
      queuedAt: command.queuedAt,
      appliedAt: command.appliedAt,
      confirmedAt: stamp(),
      frameId: confirmed.frame_id,
      state: confirmed,
      vessel: confirmed.scenario_vessels.find((item) => item.scenario_entity_id === vesselId),
      sourceCaptureId: sourceCapture?.id,
      sourceCaptureOrder: sourceCapture?.order,
      confirmedCaptureId: tracker.captureFor(confirmed)?.id,
      confirmedCaptureOrder: tracker.captureFor(confirmed)?.order,
      body: command.body,
      receipt: command.receipt,
    };
  } finally {
    responseObserver.dispose();
  }
}

export async function retryPausedRuntime(page, tracker, {
  timeoutMs = DEFAULT_TIMEOUT_MS,
  record,
} = {}) {
  const sourceFrame = await tracker.waitFor((frame) => frame.runtime_status === "paused_model", timeoutMs);
  const sourceCapture = tracker.captureFor(sourceFrame);
  const responseObserver = observeCommandReceipts(page, "runtime");
  try {
    const command = await performUiCommand(page, responseObserver, {
      resource: "runtime",
      method: "POST",
      requestPath: (path) => path === "/api/runtime/retry",
      timeoutMs,
      record,
      action: async () => {
        await page.getByRole("button", { name: "\u91cd\u8bd5", exact: true }).click();
      },
      requestData: (body) => ({ episodeId: body.episode_id }),
    });
    const confirmed = await tracker.waitFor((frame) =>
      frame.episode_id === sourceFrame.episode_id
      && frame.frame_id > sourceFrame.frame_id
      && frame.runtime_status !== "paused_model", timeoutMs);
    await recordEvent(record, {
      action: "retry", stage: "confirmed", commandId: command.commandId,
      frameId: confirmed.frame_id, sourceFrameId: sourceFrame.frame_id,
      sourceFrameCaptureId: sourceCapture?.id,
      sourceFrameCaptureOrder: sourceCapture?.order,
      frameCaptureId: tracker.captureFor(confirmed)?.id,
      frameCaptureOrder: tracker.captureFor(confirmed)?.order,
      state: { runtimeStatus: confirmed.runtime_status }, ...stamp(),
    });
    return {
      commandId: command.commandId,
      queuedAt: command.queuedAt,
      appliedAt: command.appliedAt,
      confirmedAt: stamp(),
      frameId: confirmed.frame_id,
      state: confirmed,
      sourceCaptureId: sourceCapture?.id,
      sourceCaptureOrder: sourceCapture?.order,
      confirmedCaptureId: tracker.captureFor(confirmed)?.id,
      confirmedCaptureOrder: tracker.captureFor(confirmed)?.order,
      body: command.body,
      receipt: command.receipt,
    };
  } finally {
    responseObserver.dispose();
  }
}

async function performUiCommand(page, responseObserver, {
  resource,
  method,
  requestPath,
  action,
  requestData = () => ({}),
  timeoutMs,
  record,
}) {
  const requestWaiter = page.waitForRequest((request) => {
    if (request.method() !== method) return false;
    try {
      return requestPath(new URL(request.url()).pathname);
    } catch {
      return false;
    }
  }, { timeout: timeoutMs });
  requestWaiter.catch(() => {});
  await action();
  const request = await requestWaiter;
  const body = request.postDataJSON() || {};
  const commandIdFromBody = body.command_id;
  const response = await request.response();
  if (!response) throw new Error(`${resource} UI command did not receive an HTTP response`);
  const initialReceipt = await response.json();
  const commandId = commandIdFromBody || initialReceipt.command_id;
  if (!commandId) throw new Error(`${resource} UI command response omitted command_id`);
  const queuedAt = stamp();
  await recordEvent(record, {
    action: resourceAction(resource, method), stage: "queued", commandId,
    request: requestData(body), status: initialReceipt.status,
    errorCode: initialReceipt.error_code || null, ...queuedAt,
  });
  if (!response.ok() || initialReceipt.status === "rejected") {
    throw new Error(`${resource} UI command rejected: ${initialReceipt.error_code || response.status()}`);
  }

  let receipt = initialReceipt;
  if (!isTerminal(receipt.status)) {
    receipt = await responseObserver.waitFor(commandId, timeoutMs);
  }
  const appliedAt = stamp();
  await recordEvent(record, {
    action: resourceAction(resource, method), stage: receipt.status,
    commandId, receipt, ...appliedAt,
  });
  if (receipt.status !== "applied") {
    throw new Error(`${resource} UI command ${commandId} ended with ${receipt.status}`);
  }
  return { commandId, body, receipt, queuedAt, appliedAt };
}

function observeCommandReceipts(page, resource) {
  const commandPath = resource === "intent" || resource === "runtime"
    ? "intent-commands" : "vessel-commands";
  const receipts = new Map();
  const waiters = new Map();
  const onResponse = (response) => {
    let match;
    try {
      match = new URL(response.url()).pathname.match(new RegExp(`/api/${commandPath}/([^/]+)$`));
    } catch {
      return;
    }
    if (!match) return;
    const commandId = decodeURIComponent(match[1]);
    response.json().then((receipt) => {
      const entries = receipts.get(commandId) || [];
      entries.push(receipt);
      receipts.set(commandId, entries);
      for (const wake of waiters.get(commandId) || []) wake();
    }).catch(() => {});
  };
  page.on("response", onResponse);

  return {
    waitFor(commandId, timeoutMs) {
      const terminal = () => (receipts.get(commandId) || []).find((item) => isTerminal(item.status));
      const found = terminal();
      if (found) return Promise.resolve(found);
      return new Promise((resolve, reject) => {
        const wake = () => {
          const receipt = terminal();
          if (!receipt) return;
          clearTimeout(timer);
          waiters.get(commandId)?.delete(wake);
          resolve(receipt);
        };
        const timer = setTimeout(() => {
          waiters.get(commandId)?.delete(wake);
          reject(new Error(`timed out waiting for ${resource} command receipt ${commandId}`));
        }, timeoutMs);
        const commandWaiters = waiters.get(commandId) || new Set();
        commandWaiters.add(wake);
        waiters.set(commandId, commandWaiters);
      });
    },
    dispose() {
      page.off("response", onResponse);
      for (const commandWaiters of waiters.values()) {
        for (const wake of commandWaiters) wake();
      }
      waiters.clear();
    },
  };
}

async function selectVesselStatusTab(page) {
  const drawer = page.locator(".bottom-drawer");
  if (!(await drawer.isVisible())) {
    await page.getByRole("button", { name: "切换任务详情面板" }).click();
  }
  await page.getByRole("tab", { name: "船舶状态" }).click();
}

function vesselRow(page, vesselId) {
  return page.getByRole("table", { name: "船舶状态" }).locator("tbody tr")
    .filter({ has: page.getByText(vesselId, { exact: true }) });
}

async function mapGeometry(page) {
  const rect = await page.locator(".canvas-area canvas").evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height };
  });
  return {
    rect: { x: rect.x, y: rect.y },
    layout: computeLayout(rect.width, rect.height),
  };
}

async function recordEvent(record, event) {
  if (typeof record === "function") await record({ ...event, ...stamp() });
}

function stamp() {
  return { wallTime: new Date().toISOString(), monotonicNs: process.hrtime.bigint().toString() };
}

function resourceAction(resource, method) {
  if (resource === "intent") return "focus";
  if (resource === "runtime") return "retry";
  if (method === "POST") return "create";
  if (method === "DELETE") return "delete";
  return "ais";
}

function isTerminal(status) {
  return status === "applied" || status === "rejected";
}

function assertFiveByFive(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(Number.isInteger)
    || bbox[2] - bbox[0] !== 5 || bbox[3] - bbox[1] !== 5) {
    throw new TypeError("focus bbox must be an integer 5x5 rectangle");
  }
}

function sameArray(a, b) {
  return Array.isArray(a) && Array.isArray(b)
    && a.length === b.length && a.every((value, index) => value === b[index]);
}

function positionMatches(actual, expected) {
  return Array.isArray(actual) && Array.isArray(expected) && actual.length === 2
    && actual.every((value, index) => Number.isFinite(value)
      && Math.abs(value - expected[index]) <= 0.15);
}
