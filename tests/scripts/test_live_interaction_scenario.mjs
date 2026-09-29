import test from "node:test";
import assert from "node:assert/strict";
import {
  operatorSchedule,
  seededRandom,
  chooseFocusBBox,
  chooseFleetTargets,
  chooseAisTargets,
  auditAcceptance,
} from "../../scripts/live_interaction_scenario.mjs";

test("operator schedule has five hourly focuses and three 90-minute fleet rounds", () => {
  const events = operatorSchedule();
  assert.deepEqual(events.map(({ kind, atMs }) => [kind, atMs]), [
    ["focus", 3600000], ["fleet", 5400000], ["focus", 7200000],
    ["focus", 10800000], ["fleet", 10800000], ["focus", 14400000],
    ["fleet", 16200000], ["focus", 18000000],
  ]);
});

test("focus boxes use 25 searchable cells and avoid previous boxes", () => {
  const cells = Array.from({ length: 30 }, (_, x) => Array.from({ length: 30 }, (_, y) => [x, y]))
    .flat().filter(([x, y]) => !(x >= 10 && x < 15 && y >= 10 && y < 15));
  const frame = { search_domain: { searchable_cells: cells } };
  const first = chooseFocusBBox(frame, seededRandom(8), []);
  const second = chooseFocusBBox(frame, seededRandom(8), [first]);
  const searchable = new Set(cells.map(([x, y]) => `${x},${y}`));
  for (let x = first[0]; x < first[2]; x += 1) {
    for (let y = first[1]; y < first[3]; y += 1) assert.ok(searchable.has(`${x},${y}`));
  }
  assert.equal(first[2] - first[0], 5);
  assert.equal(first[3] - first[1], 5);
  assert.ok(second[2] <= first[0] || second[0] >= first[2]
    || second[3] <= first[1] || second[1] >= first[3]);
});

test("focus selection prefers a legal box that intersects an active search region", () => {
  const cells = Array.from({ length: 30 }, (_, x) => Array.from({ length: 30 }, (_, y) => [x, y]))
    .flat();
  const frame = {
    search_domain: { searchable_cells: cells },
    search_regions: [{ id: "search:20:10:26:18", type: "search", status: "active", bbox: [20, 10, 26, 18] }],
  };
  const bbox = chooseFocusBBox(frame, seededRandom(17), []);

  assert.ok(bbox[0] < 26 && bbox[2] > 20 && bbox[1] < 18 && bbox[3] > 10);
});

test("same seed selects the same fleet and AIS changes", () => {
  const vessels = [
    { scenario_entity_id: "i-1", vessel_class: "type_i" },
    { scenario_entity_id: "i-2", vessel_class: "type_i" },
    { scenario_entity_id: "ii-1", vessel_class: "type_ii", ais_controllable: true },
    { scenario_entity_id: "ii-2", vessel_class: "type_ii", ais_controllable: true },
    { scenario_entity_id: "ii-3", vessel_class: "type_ii", ais_controllable: true },
    { scenario_entity_id: "ii-locked", vessel_class: "type_ii", ais_controllable: false },
  ];
  const frame = { scenario_vessels: vessels };
  const fleet = chooseFleetTargets(frame, seededRandom(3));
  const ais = chooseAisTargets(frame, seededRandom(12));

  assert.deepEqual(fleet, chooseFleetTargets(frame, seededRandom(3)));
  assert.ok(["i-1", "i-2"].includes(fleet.replaceTypeIId));
  assert.ok(["ii-1", "ii-2", "ii-3", "ii-locked"].includes(fleet.replaceTypeIIId));
  assert.deepEqual(ais, chooseAisTargets(frame, seededRandom(12)));
  assert.equal(ais.length, 2);
  assert.equal(new Set(ais).size, 2);
  assert.ok(ais.every((id) => id.startsWith("ii-") && id !== "ii-locked"));
});

test("focus selection rejects frames without any legal 5x5 area", () => {
  const frame = { search_domain: { searchable_cells: [[0, 0], [1, 0], [0, 1], [1, 1]] } };
  assert.throws(() => chooseFocusBBox(frame, seededRandom(4), []), /legal 5x5 focus area/);
});

test("acceptance audit passes only with six-hour runtime and confirmed red response", () => {
  const evidence = completeEvidence();
  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, true);
  assert.deepEqual(audit.failures, []);
  assert.equal(audit.metrics.focusAreasWithRedTasks, 5);
  assert.equal(audit.metrics.confirmedFleetRounds, 3);
});

test("acceptance audit rejects queued actions and missing red task evidence", () => {
  const evidence = completeEvidence();
  evidence.report.summary.wall_seconds = 21599;
  evidence.operatorEvents[0].status = "queued";
  const responseFrame = evidence.frames.find((frame) =>
    frame.intent_statuses?.some((status) => status.intent_id === "focus-1"));
  responseFrame.intent_statuses[0].assigned_task_ids = [];

  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, false);
  assert.match(audit.failures.join("\n"), /21,600|21600/);
  assert.match(audit.failures.join("\n"), /focus-1/);
  assert.match(audit.failures.join("\n"), /queued|not confirmed/i);
});

test("acceptance audit rejects interventions recorded at the wrong offsets", () => {
  const evidence = completeEvidence();
  evidence.operatorEvents[0].atMs = 3599000;
  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, false);
  assert.match(audit.failures.join("\n"), /scheduled offset/);
});

test("acceptance audit rejects an intervention started over one minute late", () => {
  const evidence = completeEvidence();
  evidence.operatorEvents[0].issuedElapsedMs += 60001;
  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, false);
  assert.match(audit.failures.join("\n"), /outside its scheduled offset/i);
});

test("audit confirmation metrics require authoritative fleet and AIS state", () => {
  const evidence = completeEvidence();
  const firstFleet = evidence.operatorEvents.find((event) => event.kind === "fleet");
  firstFleet.operations.find((operation) => operation.kind === "ais").frameId = 9999;

  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, false);
  assert.equal(audit.metrics.confirmedFleetRounds, 2);
  assert.equal(audit.metrics.confirmedAisChanges, 2);
});

test("audit rejects focus confirmations without a matching authoritative intent", () => {
  const evidence = completeEvidence();
  evidence.operatorEvents[0].confirmedFrameId = 9999;

  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, false);
  assert.equal(audit.metrics.focusAreasConfirmed, 4);
  assert.match(audit.failures.join("\n"), /authoritative intent frame/);
});

test("audit rejects a focus bbox that is not exactly five cells per side", () => {
  const evidence = completeEvidence();
  const event = evidence.operatorEvents.find((item) => item.kind === "focus");
  event.bbox = [0, 0, 4, 5];

  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, false);
  assert.match(audit.failures.join("\n"), /integer 5x5 bbox/);
});

test("audit rejects queued vessel and AIS operations", () => {
  const evidence = completeEvidence();
  const firstFleet = evidence.operatorEvents.find((event) => event.kind === "fleet");
  firstFleet.operations.find((operation) => operation.kind === "delete").status = "queued";
  firstFleet.operations.find((operation) => operation.kind === "ais").status = "queued";

  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, false);
  assert.equal(audit.metrics.confirmedFleetRounds, 2);
  assert.equal(audit.metrics.confirmedAisChanges, 2);
  assert.match(audit.failures.join("\n"), /deletion .* was not confirmed/);
  assert.match(audit.failures.join("\n"), /AIS toggle .* was not confirmed/);
});

test("audit distinguishes command-boundary frames sharing a simulation frame id", () => {
  const evidence = completeEvidence();
  const captureByFrameId = new Map();
  evidence.frames.forEach((frame, index) => {
    const captureId = `${frame.frame_id}:${index + 1}`;
    captureByFrameId.set(frame.frame_id, captureId);
    frame.capture_id = captureId;
    frame.capture_order = index + 1;
  });
  for (const event of evidence.operatorEvents) {
    if (event.kind === "focus") {
      event.selectedFrameCaptureId = captureByFrameId.get(event.selectedFrameId);
      event.confirmedFrameCaptureId = captureByFrameId.get(event.confirmedFrameId);
    } else {
      event.selectionFrameCaptureId = captureByFrameId.get(event.selectionFrameId);
      for (const operation of event.operations) {
        if (operation.selectionFrameId != null) {
          operation.selectionFrameCaptureId = captureByFrameId.get(operation.selectionFrameId);
        }
        if (operation.frameId != null) operation.frameCaptureId = captureByFrameId.get(operation.frameId);
      }
    }
  }
  for (const frame of evidence.frames) frame.frame_id = 1;

  const audit = auditAcceptance(evidence);
  assert.equal(audit.passed, true, audit.failures.join("\n"));
  assert.equal(audit.metrics.focusAreasWithRedTasks, 5);
  assert.equal(audit.metrics.confirmedFleetRounds, 3);
});

function completeEvidence() {
  const searchableCells = Array.from({ length: 30 }, (_, x) =>
    Array.from({ length: 30 }, (_, y) => [x, y])).flat();
  const frames = [];
  let frameId = 0;
  const pushFrame = (data) => {
    const frame = { frame_id: ++frameId, ...data };
    frames.push(frame);
    return frame;
  };
  const operatorEvents = [];
  const focusOffsets = [3600000, 7200000, 10800000, 14400000, 18000000];

  for (let index = 0; index < 5; index += 1) {
    const intentId = `focus-${index + 1}`;
    const bbox = [index * 5, 0, index * 5 + 5, 5];
    const selected = pushFrame({ search_domain: { searchable_cells: searchableCells } });
    const confirmed = pushFrame({ intents: [{ intent_id: intentId, bbox }], intent_statuses: [] });
    pushFrame({
      intents: [{ intent_id: intentId, bbox }],
      intent_statuses: [{ intent_id: intentId, assigned_task_ids: [`search:${intentId}:001`] }],
    });
    operatorEvents.push({
      kind: "focus", atMs: focusOffsets[index], issuedElapsedMs: focusOffsets[index],
      status: "confirmed", intentId, bbox,
      selectedFrameId: selected.frame_id, confirmedFrameId: confirmed.frame_id,
    });
  }

  for (let round = 1; round <= 3; round += 1) {
    const oldTypeI = `type-i-old-${round}`;
    const oldTypeII = `type-ii-old-${round}`;
    const newTypeI = `type-i-new-${round}`;
    const newTypeII = `type-ii-new-${round}`;
    const positionI = [round + 5, 10];
    const positionII = [round + 10, 10];
    const operations = [];
    const selected = pushFrame({ scenario_vessels: [
      { scenario_entity_id: oldTypeI, vessel_class: "type_i", position: [3, 10] },
      { scenario_entity_id: oldTypeII, vessel_class: "type_ii", position: [4, 10],
        ais_enabled: true, ais_controllable: true },
    ] });
    const deletionI = pushFrame({ scenario_vessels: [] });
    operations.push({ kind: "delete", status: "confirmed", vesselId: oldTypeI,
      vesselClass: "type_i", selectionFrameId: selected.frame_id, frameId: deletionI.frame_id });
    const deletionII = pushFrame({ scenario_vessels: [] });
    operations.push({ kind: "delete", status: "confirmed", vesselId: oldTypeII,
      vesselClass: "type_ii", selectionFrameId: selected.frame_id, frameId: deletionII.frame_id });
    const placementI = pushFrame({ scenario_vessels: [
      { scenario_entity_id: newTypeI, vessel_class: "type_i", position: positionI },
    ] });
    operations.push({ kind: "create", status: "confirmed", vesselId: newTypeI,
      replacementFor: oldTypeI, vesselClass: "type_i", position: positionI,
      frameId: placementI.frame_id });
    const placementII = pushFrame({ scenario_vessels: [
      { scenario_entity_id: newTypeI, vessel_class: "type_i", position: positionI },
      { scenario_entity_id: newTypeII, vessel_class: "type_ii", position: positionII,
        ais_enabled: true, ais_controllable: true },
    ] });
    operations.push({ kind: "create", status: "confirmed", vesselId: newTypeII,
      replacementFor: oldTypeII, vesselClass: "type_ii", position: positionII,
      frameId: placementII.frame_id });
    const aisSelected = pushFrame({ scenario_vessels: [
      { scenario_entity_id: newTypeII, vessel_class: "type_ii", position: positionII,
        ais_enabled: true, ais_controllable: true },
    ] });
    const ais = pushFrame({ scenario_vessels: [
      { scenario_entity_id: newTypeII, vessel_class: "type_ii", position: positionII, ais_enabled: false },
    ] });
    operations.push({ kind: "ais", status: "confirmed", vesselId: newTypeII,
      aisEnabled: false, selectionFrameId: aisSelected.frame_id, frameId: ais.frame_id });
    operatorEvents.push({ kind: "fleet", atMs: round * 5400000,
      issuedElapsedMs: round * 5400000, status: "confirmed", round,
      selectionFrameId: selected.frame_id, operations });
  }

  return { report: { summary: { wall_seconds: 21600 } }, operatorEvents, frames };
}
