import assert from "node:assert/strict";
import { test } from "node:test";
import { RoutineCooldown, withRunHeartbeat } from "./scheduling.ts";

test("successful job starts a fifteen-second routine-only cooldown", () => {
  const schedule = new RoutineCooldown();
  assert.deepEqual(schedule.nextRequest(100), { defer_routine: false });
  schedule.completed(100);
  assert.deepEqual(schedule.nextRequest(101), { defer_routine: true });
  assert.deepEqual(schedule.nextRequest(15099), { defer_routine: true });
  assert.deepEqual(schedule.nextRequest(15100), { defer_routine: false });
});

test("polling during cooldown never extends it or blocks an urgent-only poll", () => {
  const schedule = new RoutineCooldown();
  schedule.completed(0);
  for (let now = 1000; now < 15000; now += 1000) assert.deepEqual(schedule.nextRequest(now), { defer_routine: true });
  assert.deepEqual(schedule.nextRequest(15000), { defer_routine: false });
  schedule.completed(20000);
  assert.deepEqual(schedule.nextRequest(34999), { defer_routine: true });
  assert.deepEqual(schedule.nextRequest(35000), { defer_routine: false });
});

test("heartbeat renews a fifteen-second lease throughout slow event drain and completion receipt", async (context) => {
  context.mock.timers.enable({ apis: ["setInterval", "Date"], now: 0 });
  const events = Promise.withResolvers<void>();
  const receipt = Promise.withResolvers<void>();
  let deadline = 15000;
  let renewals = 0;
  let completed = false;
  const running = withRunHeartbeat(async () => {
    await events.promise;
    assert.ok(Date.now() < deadline);
    await receipt.promise;
    assert.ok(Date.now() < deadline);
    completed = true;
  }, async () => {
    assert.ok(Date.now() < deadline);
    deadline = Date.now() + 15000;
    renewals++;
  }, (error) => { throw error; });
  for (let step = 0; step < 8; step++) {
    context.mock.timers.tick(3000);
    await Promise.resolve();
  }
  assert.equal(renewals, 8);
  assert.equal(completed, false);
  events.resolve();
  await Promise.resolve();
  for (let step = 0; step < 6; step++) {
    context.mock.timers.tick(3000);
    await Promise.resolve();
  }
  assert.equal(renewals, 14);
  receipt.resolve();
  await running;
  assert.equal(completed, true);
  context.mock.timers.tick(30000);
  assert.equal(renewals, 14);
});

test("heartbeat errors preserve abort handling and clear the timer on rejected settlement", async (context) => {
  context.mock.timers.enable({ apis: ["setInterval"] });
  const operation = Promise.withResolvers<void>();
  const failure = new Error("run_cancelled");
  let attempts = 0;
  let aborted = false;
  const running = withRunHeartbeat(() => operation.promise, async () => {
    attempts++;
    throw failure;
  }, (error) => {
    assert.equal(error, failure);
    aborted = true;
    operation.reject(error);
  });
  const rejected = assert.rejects(running, (error) => error === failure);
  context.mock.timers.tick(3000);
  await rejected;
  assert.equal(aborted, true);
  context.mock.timers.tick(30000);
  assert.equal(attempts, 1);
});
