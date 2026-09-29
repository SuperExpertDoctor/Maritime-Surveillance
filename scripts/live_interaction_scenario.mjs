export function operatorSchedule() {
  return [
    ["focus", 3600000],
    ["fleet", 5400000],
    ["focus", 7200000],
    ["focus", 10800000],
    ["fleet", 10800000],
    ["focus", 14400000],
    ["fleet", 16200000],
    ["focus", 18000000],
  ].map(([kind, atMs], order) => ({ kind, atMs, order }));
}

export function seededRandom(seed) {
  if (!Number.isInteger(seed) || seed < 0 || seed > 0xffffffff) {
    throw new RangeError("seed must be an unsigned 32-bit integer");
  }
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6D2B79F5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

export function chooseFocusBBox(frame, rng, previousBBoxes = []) {
  const cells = frame?.search_domain?.searchable_cells;
  if (!Array.isArray(cells) || cells.length === 0) {
    throw new Error("no legal 5x5 focus area in the live searchable domain");
  }
  const searchable = new Set();
  let maxX = -1;
  let maxY = -1;
  for (const cell of cells) {
    if (!Array.isArray(cell) || cell.length < 2) continue;
    const [x, y] = cell;
    if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0) continue;
    searchable.add(`${x},${y}`);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  }

  const candidates = [];
  for (let x = 0; x <= maxX - 4; x += 1) {
    for (let y = 0; y <= maxY - 4; y += 1) {
      const bbox = [x, y, x + 5, y + 5];
      let eligible = true;
      for (let col = x; eligible && col < x + 5; col += 1) {
        for (let row = y; row < y + 5; row += 1) {
          if (!searchable.has(`${col},${row}`)) {
            eligible = false;
            break;
          }
        }
      }
      if (eligible && !previousBBoxes.some((previous) => overlaps(bbox, previous))) {
        candidates.push(bbox);
      }
    }
  }
  if (!candidates.length) {
    throw new Error("no legal 5x5 focus area in the live searchable domain");
  }
  const activeSearchBBoxes = (frame.search_regions || [])
    .filter((region) => region.status === "active"
      && typeof region.id === "string" && region.id.startsWith("search:")
      && Array.isArray(region.bbox) && region.bbox.length === 4)
    .map((region) => region.bbox);
  const taskBackedCandidates = candidates.filter((bbox) =>
    activeSearchBBoxes.some((regionBBox) => overlaps(bbox, regionBBox)));
  const selectionPool = taskBackedCandidates.length ? taskBackedCandidates : candidates;
  shuffle(selectionPool, rng);
  return selectionPool[0];
}

export function chooseFleetTargets(frame, rng) {
  const vessels = Array.isArray(frame?.scenario_vessels) ? frame.scenario_vessels : [];
  const typeI = vessels.filter((vessel) => vessel.vessel_class === "type_i");
  const typeII = vessels.filter((vessel) => vessel.vessel_class === "type_ii");
  if (!typeI.length || !typeII.length) {
    throw new Error("active Type I and Type II vessels are required for fleet changes");
  }
  return {
    replaceTypeIId: typeI[Math.floor(rng() * typeI.length)].scenario_entity_id,
    replaceTypeIIId: typeII[Math.floor(rng() * typeII.length)].scenario_entity_id,
  };
}

export function chooseAisTargets(frame, rng) {
  const vessels = Array.isArray(frame?.scenario_vessels) ? frame.scenario_vessels : [];
  const typeIIIds = vessels
    .filter((vessel) => vessel.vessel_class === "type_ii"
      && vessel.ais_controllable === true
      && typeof vessel.scenario_entity_id === "string")
    .map((vessel) => vessel.scenario_entity_id);
  if (!typeIIIds.length) throw new Error("no controllable Type II vessels are active");
  shuffle(typeIIIds, rng);
  return typeIIIds.slice(0, Math.max(1, Math.ceil(typeIIIds.length / 2)));
}

export function auditAcceptance({ report, operatorEvents, frames, requiredWallSeconds = 21600 }) {
  const failures = [];
  const wallSeconds = Number(report?.summary?.wall_seconds);
  if (!Number.isFinite(requiredWallSeconds) || requiredWallSeconds <= 0) {
    throw new RangeError("requiredWallSeconds must be finite and positive");
  }
  if (!Number.isFinite(wallSeconds) || wallSeconds < requiredWallSeconds) {
    failures.push(`main.py wall runtime was ${wallSeconds || 0}s; at least ${requiredWallSeconds}s is required`);
  }

  const frameById = new Map();
  const frameByCaptureId = new Map();
  const allFrames = [];
  for (const frame of Array.isArray(frames) ? frames : []) {
    if (!Number.isInteger(frame?.frame_id)) continue;
    allFrames.push(frame);
    frameById.set(frame.frame_id, frame);
    if (typeof frame.capture_id === "string") frameByCaptureId.set(frame.capture_id, frame);
  }
  const evidenceFrame = (captureId, frameId) =>
    (typeof captureId === "string" ? frameByCaptureId.get(captureId) : null)
      || frameById.get(frameId);
  const events = Array.isArray(operatorEvents) ? operatorEvents : [];
  const focusEvents = events.filter((event) => event.kind === "focus");
  const fleetEvents = events.filter((event) => event.kind === "fleet");
  if (focusEvents.length !== 5) failures.push(`expected 5 focus events; found ${focusEvents.length}`);
  if (fleetEvents.length !== 3) failures.push(`expected 3 fleet events; found ${fleetEvents.length}`);
  const expectedSchedule = operatorSchedule().map((event) => `${event.kind}:${event.atMs}`).sort();
  const actualSchedule = events.map((event) => `${event.kind}:${event.atMs}`).sort();
  if (JSON.stringify(actualSchedule) !== JSON.stringify(expectedSchedule)) {
    failures.push("operator events do not match the required scheduled offsets");
  }
  let maxDispatchDriftMs = 0;
  for (const event of events) {
    const drift = Number(event.issuedElapsedMs) - Number(event.atMs);
    if (!Number.isFinite(drift) || Math.abs(drift) > 60000) {
      failures.push(`${event.kind} event at ${event.atMs}ms was dispatched over one minute outside its scheduled offset`);
    } else {
      maxDispatchDriftMs = Math.max(maxDispatchDriftMs, Math.abs(drift));
    }
  }

  let focusAreasWithRedTasks = 0;
  let confirmedFocusAreas = 0;
  let eligibleFocusCells = 0;
  for (const event of focusEvents) {
    const intentId = event.intentId || "unknown";
    if (event.status !== "confirmed") {
      failures.push(`focus ${intentId} was not confirmed (status=${event.status || "missing"})`);
    }
    const bbox = event.bbox;
    if (!isFiveByFive(bbox)) {
      failures.push(`focus ${intentId} does not have an integer 5x5 bbox`);
      continue;
    }
    const selectedFrame = evidenceFrame(event.selectedFrameCaptureId, event.selectedFrameId);
    if (!selectedFrame) {
      failures.push(`focus ${intentId} has no source frame ${event.selectedFrameId}`);
    } else if (countSearchableCells(selectedFrame, bbox) !== 25) {
      failures.push(`focus ${intentId} did not select 25 searchable cells`);
    } else {
      eligibleFocusCells += 25;
    }

    const confirmationFrame = evidenceFrame(event.confirmedFrameCaptureId, event.confirmedFrameId);
    const confirmedIntent = confirmationFrame?.intents?.find((intent) =>
      intent.intent_id === intentId && sameArray(intent.bbox, bbox));
    if (!confirmedIntent) {
      failures.push(`focus ${intentId} has no matching authoritative intent frame`);
      continue;
    }
    if (event.status === "confirmed") confirmedFocusAreas += 1;
    const confirmationOrder = evidenceFrameOrder(confirmationFrame);
    const taskAssigned = allFrames.some((frame) =>
      evidenceFrameOrder(frame) > confirmationOrder
      && frame.intents?.some((intent) => intent.intent_id === intentId)
      && frame.intent_statuses?.some((status) => status.intent_id === intentId
        && Array.isArray(status.assigned_task_ids) && status.assigned_task_ids.length > 0));
    if (taskAssigned) focusAreasWithRedTasks += 1;
    else failures.push(`focus ${intentId} has no subsequent red task assignment`);
  }

  let confirmedFleetRounds = 0;
  let confirmedAisChanges = 0;
  for (const event of fleetEvents) {
    const eventFailureCount = failures.length;
    const operations = Array.isArray(event.operations) ? event.operations : [];
    const deletes = operations.filter((operation) => operation.kind === "delete");
    const creates = operations.filter((operation) => operation.kind === "create");
    const aisChanges = operations.filter((operation) => operation.kind === "ais");
    const round = event.round ?? "unknown";
    if (event.status !== "confirmed") failures.push(`fleet round ${round} was not confirmed`);
    if (deletes.length !== 2 || new Set(deletes.map((operation) => operation.vesselClass)).size !== 2) {
      failures.push(`fleet round ${round} must confirm one Type I and one Type II deletion`);
    }
    if (creates.length !== 2 || new Set(creates.map((operation) => operation.vesselClass)).size !== 2) {
      failures.push(`fleet round ${round} must confirm one same-class Type I and Type II replacement`);
    }
    if (!aisChanges.length) failures.push(`fleet round ${round} has no Type II AIS toggle`);

    const selectedFrame = evidenceFrame(event.selectionFrameCaptureId, event.selectionFrameId);
    if (!selectedFrame) failures.push(`fleet round ${round} has no vessel-selection frame`);
    const selectedVessels = selectedFrame?.scenario_vessels || [];
    const selectedById = new Map(selectedVessels.map((vessel) => [vessel.scenario_entity_id, vessel]));

    for (const operation of deletes) {
      if (operation.status !== "confirmed") {
        failures.push(`fleet round ${round} deletion ${operation.vesselId} was not confirmed`);
      }
      const original = selectedById.get(operation.vesselId);
      if (!original || original.vessel_class !== operation.vesselClass) {
        failures.push(`fleet round ${round} did not select active ${operation.vesselClass} vessel ${operation.vesselId}`);
      }
      const state = evidenceFrame(operation.frameCaptureId, operation.frameId)?.scenario_vessels;
      if (!Array.isArray(state) || state.some((vessel) => vessel.scenario_entity_id === operation.vesselId)) {
        failures.push(`fleet round ${round} deletion ${operation.vesselId} is absent from no authoritative frame`);
      }
    }

    for (const operation of creates) {
      if (operation.status !== "confirmed") {
        failures.push(`fleet round ${round} replacement ${operation.vesselId} was not confirmed`);
      }
      const original = selectedById.get(operation.replacementFor);
      const state = evidenceFrame(operation.frameCaptureId, operation.frameId)?.scenario_vessels;
      const replacement = state?.find((vessel) => vessel.scenario_entity_id === operation.vesselId);
      if (!original || original.vessel_class !== operation.vesselClass
        || !deletes.some((deletion) => deletion.vesselId === operation.replacementFor
          && deletion.vesselClass === operation.vesselClass)) {
        failures.push(`fleet round ${round} replacement ${operation.vesselId} does not match a deleted vessel class`);
      }
      if (!replacement || replacement.vessel_class !== operation.vesselClass
        || !positionMatches(replacement.position, operation.position)
        || sameArray(original?.position, operation.position)) {
        failures.push(`fleet round ${round} replacement ${operation.vesselId} lacks a confirmed new-class position`);
      }
    }

    const activeTypeIIs = selectedVessels.filter((vessel) =>
      vessel.vessel_class === "type_ii" && vessel.ais_controllable === true);
    const expectedAisCount = activeTypeIIs.length
      ? Math.max(1, Math.ceil(activeTypeIIs.length / 2)) : 0;
    if (aisChanges.length !== expectedAisCount) {
      failures.push(`fleet round ${round} expected ${expectedAisCount} randomized AIS toggles; found ${aisChanges.length}`);
    }
    const changedIds = new Set();
    let frameConfirmedAisChanges = 0;
    for (const operation of aisChanges) {
      const priorFrame = evidenceFrame(operation.selectionFrameCaptureId, operation.selectionFrameId);
      const prior = priorFrame?.scenario_vessels?.find((vessel) =>
        vessel.scenario_entity_id === operation.vesselId);
      const current = evidenceFrame(operation.frameCaptureId, operation.frameId)?.scenario_vessels?.find((vessel) =>
        vessel.scenario_entity_id === operation.vesselId);
      const stateChanged = Boolean(prior && prior.vessel_class === "type_ii"
        && prior.ais_controllable === true
        && prior.ais_enabled !== operation.aisEnabled && current
        && current.vessel_class === "type_ii" && current.ais_enabled === operation.aisEnabled);
      if (operation.status !== "confirmed") {
        failures.push(`fleet round ${round} AIS toggle ${operation.vesselId} was not confirmed`);
      }
      if (!stateChanged) {
        failures.push(`fleet round ${round} AIS toggle ${operation.vesselId} lacks a confirmed Type II state change`);
      }
      if (operation.status === "confirmed" && stateChanged) frameConfirmedAisChanges += 1;
      if (changedIds.has(operation.vesselId)) {
        failures.push(`fleet round ${round} toggles ${operation.vesselId} more than once`);
      }
      changedIds.add(operation.vesselId);
    }
    if (event.status === "confirmed" && operations.every((operation) => operation.status === "confirmed")
      && failures.length === eventFailureCount) {
      confirmedFleetRounds += 1;
    }
    confirmedAisChanges += frameConfirmedAisChanges;
  }

  return {
    passed: failures.length === 0,
    failures,
    metrics: {
      wallSeconds,
      maxDispatchDriftMs,
      focusAreasConfirmed: confirmedFocusAreas,
      focusAreasWithRedTasks,
      eligibleFocusCells,
      confirmedFleetRounds,
      confirmedAisChanges,
    },
  };
}

function overlaps(a, b) {
  return a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
}

function shuffle(values, rng) {
  for (let index = values.length - 1; index > 0; index -= 1) {
    const other = Math.floor(rng() * (index + 1));
    [values[index], values[other]] = [values[other], values[index]];
  }
}

function isFiveByFive(bbox) {
  return Array.isArray(bbox) && bbox.length === 4
    && bbox.every(Number.isInteger)
    && bbox[2] - bbox[0] === 5 && bbox[3] - bbox[1] === 5;
}

function countSearchableCells(frame, bbox) {
  const searchable = new Set((frame.search_domain?.searchable_cells || [])
    .map((cell) => `${cell[0]},${cell[1]}`));
  let count = 0;
  for (let x = bbox[0]; x < bbox[2]; x += 1) {
    for (let y = bbox[1]; y < bbox[3]; y += 1) {
      if (searchable.has(`${x},${y}`)) count += 1;
    }
  }
  return count;
}

function sameArray(a, b) {
  return Array.isArray(a) && Array.isArray(b)
    && a.length === b.length && a.every((value, index) => value === b[index]);
}

function positionMatches(actual, requested) {
  return Array.isArray(actual) && Array.isArray(requested) && actual.length === 2
    && actual.every((value, index) => Number.isFinite(value)
      && Number.isFinite(requested[index]) && Math.abs(value - requested[index]) <= 0.15);
}

function evidenceFrameOrder(frame) {
  return Number.isFinite(frame?.capture_order) ? frame.capture_order : frame?.frame_id;
}
