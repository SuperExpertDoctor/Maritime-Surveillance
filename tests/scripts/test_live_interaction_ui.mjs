import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { attachLiveFrames, createFrameTracker } from "../../scripts/live_interaction_ui.mjs";

test("live frame capture parses Playwright WebSocket payload wrappers", () => {
  const page = new EventEmitter();
  const socket = new EventEmitter();
  socket.url = () => "ws://127.0.0.1:8765/ws/live";
  const tracker = createFrameTracker();
  const frame = { frame_id: 4, episode_id: "episode-live" };
  const dispose = attachLiveFrames(page, tracker);

  page.emit("websocket", socket);
  socket.emit("framereceived", { payload: JSON.stringify(frame) });

  assert.deepEqual(tracker.latest, frame);
  dispose();
});

test("frame tracker assigns unique capture order to command-boundary snapshots", () => {
  const captured = [];
  const tracker = createFrameTracker(null, {
    onFrame: (frame, capture) => captured.push({ frame, capture }),
  });
  const before = { frame_id: 4, episode_id: "episode-live", marker: "before" };
  const after = { frame_id: 4, episode_id: "episode-live", marker: "after" };

  tracker.observe(before);
  tracker.observe(after);

  assert.equal(tracker.latest, after);
  assert.deepEqual(tracker.captureFor(before), { id: "episode-live:4:1", order: 1 });
  assert.deepEqual(tracker.captureFor(after), { id: "episode-live:4:2", order: 2 });
  assert.deepEqual(captured.map(({ capture }) => capture.order), [1, 2]);
});
