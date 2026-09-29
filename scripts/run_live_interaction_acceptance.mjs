import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";
import net from "node:net";
import { appendFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { createWriteStream } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import {
  auditAcceptance,
  chooseAisTargets,
  chooseFleetTargets,
  chooseFocusBBox,
  operatorSchedule,
  seededRandom,
} from "./live_interaction_scenario.mjs";
import {
  attachLiveFrames,
  createFrameTracker,
  deleteVessel,
  drawFocusArea,
  retryPausedRuntime,
  selectVesselPlacement,
  setVesselAis,
} from "./live_interaction_ui.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PYTHON = process.env.PYTHON || "python";
const ACCEPTANCE_SECONDS = 21_720;
const SMOKE_SECONDS = 600;
const ACCEPTANCE_STEP_DELAY = 60;
const SMOKE_STEP_DELAY = 1;

export function parseArgs(args = process.argv.slice(2)) {
  let smoke = false;
  let seed = null;
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === "--help" || argument === "-h") return { help: true };
    if (argument === "--smoke") {
      if (smoke) throw new Error("--smoke may only be specified once");
      smoke = true;
      continue;
    }
    if (argument === "--seed") {
      if (seed !== null) throw new Error("--seed may only be specified once");
      const value = args[index + 1];
      if (!value || value.startsWith("--") || !/^\d+$/.test(value)) {
        throw new Error("--seed requires an unsigned 32-bit integer");
      }
      seed = Number(value);
      if (!Number.isInteger(seed) || seed > 0xffffffff) {
        throw new Error("--seed requires an unsigned 32-bit integer");
      }
      index += 1;
      continue;
    }
    throw new Error(`unknown option: ${argument}`);
  }
  if (seed === null) throw new Error("--seed is required so the run can be reproduced");
  return { smoke, seed };
}

export function buildMainArgs({ smoke, port, memoryRoot, reportDir, mainScript = join(ROOT, "main.py") }) {
  return [
    mainScript,
    "--steps", "1000",
    "--step-delay", smoke ? String(SMOKE_STEP_DELAY) : String(ACCEPTANCE_STEP_DELAY),
    "--wall-seconds", String(smoke ? SMOKE_SECONDS : ACCEPTANCE_SECONDS),
    "--llm-probe-timeout", "120",
    "--port", String(port),
    "--memory-root", memoryRoot,
    "--run-report-dir", reportDir,
  ];
}

async function runScenario({ smoke, seed }) {
  const preflight = runPreflight();
  if (preflight.clearOutputsBeforeRun) {
    throw new Error("configs/common.yaml enables output deletion; the live run was not started");
  }
  if (!preflight.longcatKeyConfigured) {
    throw new Error("LONGCAT_API_KEY is not configured; the live run was not started");
  }

  const { chromium } = createRequire(join(ROOT, "src/vis/frontend/package.json"))("@playwright/test");
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {}),
  });
  const outputsDir = join(ROOT, "outputs");
  await mkdir(outputsDir, { recursive: true });
  const runDir = await createRunDirectory(outputsDir, smoke, seed);
  const screenshotsDir = join(runDir, "screenshots");
  await mkdir(screenshotsDir, { recursive: false });
  const paths = {
    runDir,
    screenshotsDir,
    operator: join(runDir, "operator.jsonl"),
    frames: join(runDir, "frames.jsonl"),
    console: join(runDir, "runtime-console.log"),
    run: join(runDir, "run.json"),
    reportDir: join(runDir, "main-report"),
    memoryRoot: join(runDir, "strategy_memory"),
  };
  const writeOperatorLine = createJsonlWriter(paths.operator);
  const writeFrameLine = createJsonlWriter(paths.frames);
  const evidenceErrors = [];
  const recordOperator = async (event) => {
    await writeOperatorLine({ writtenAt: new Date().toISOString(), ...event });
  };
  const recordFrame = (frame, capture) => {
    const pending = writeFrameLine({
      type: "frame",
      receivedAt: new Date().toISOString(),
      captureId: capture.id,
      captureOrder: capture.order,
      frame,
    });
    pending.catch((error) => evidenceErrors.push(String(error)));
    return pending;
  };
  const tracker = createFrameTracker(null, { onFrame: recordFrame });
  const auditEvents = [];
  const random = seededRandom(seed);
  const previousFocusBoxes = [];
  const runStartedAt = new Date().toISOString();
  const runMetadata = {
    acceptance: !smoke,
    mode: smoke ? "smoke" : "acceptance",
    seed,
    startedAt: runStartedAt,
    targetWallSeconds: smoke ? SMOKE_SECONDS : ACCEPTANCE_SECONDS,
    stepDelaySeconds: smoke ? SMOKE_STEP_DELAY : ACCEPTANCE_STEP_DELAY,
    mainArgs: null,
    port: null,
    mainFrameLog: null,
    preflight: {
      clearOutputsBeforeRun: preflight.clearOutputsBeforeRun,
      longcatKeyConfigured: preflight.longcatKeyConfigured,
    },
  };
  await writeFile(paths.run, JSON.stringify(runMetadata, null, 2) + "\n", { flag: "wx" });
  await recordOperator({ type: "run_started", ...runMetadata, outputDir: runDir });

  let child = null;
  let childStatus = null;
  let childDone = Promise.resolve({ code: null, signal: null, error: "main process not started" });
  let consoleStream = null;
  let context = null;
  let failure = null;
  let audit = null;
  let smokeSummary = null;
  let runtimeAnchor = null;
  let stopController = null;
  let pollTask = null;
  let pauseTask = null;
  const onInterrupt = () => stopController?.abort(new Error("operator interrupted the run"));

  try {
    const port = await findAvailablePort();
    const mainFrameLog = await reserveMainFrameLog(outputsDir);
    const reportDir = paths.reportDir;
    const args = buildMainArgs({
      smoke,
      port,
      memoryRoot: paths.memoryRoot,
      reportDir,
    });
    runMetadata.mainArgs = [PYTHON, ...args.map((argument) => argument.startsWith(ROOT)
      ? argument.slice(ROOT.length + 1) : argument)];
    runMetadata.port = port;
    runMetadata.mainFrameLog = mainFrameLog;
    await writeFile(paths.run, JSON.stringify(runMetadata, null, 2) + "\n", { flag: "w" });
    await recordOperator({ type: "main_starting", args: runMetadata.mainArgs, port });

    consoleStream = createWriteStream(paths.console, { flags: "wx" });
    child = spawn(PYTHON, args, { cwd: ROOT, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    childDone = observeChild(child);
    childDone.then((status) => { childStatus = status; });
    pipeChildOutput(child.stdout, "stdout", consoleStream);
    pipeChildOutput(child.stderr, "stderr", consoleStream);
    process.on("SIGINT", onInterrupt);
    process.on("SIGTERM", onInterrupt);

    const baseUrl = `http://127.0.0.1:${port}`;
    await waitForServer(baseUrl, child, childDone);
    const viewport = { width: 1500, height: 900 };
    context = await browser.newContext({ viewport });
    const focusPage = await context.newPage();
    const disposeFrames = attachLiveFrames(focusPage, tracker);
    await focusPage.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
    await focusPage.locator(".connection-state.connected").waitFor({ state: "visible", timeout: 60_000 });
    const initialFrame = await tracker.waitFor((frame) =>
      frame.episode_id && Array.isArray(frame.search_domain?.searchable_cells)
      && Array.isArray(frame.scenario_vessels), 60_000);
    const fleetPage = await context.newPage();
    await fleetPage.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
    await fleetPage.locator(".connection-state.connected").waitFor({ state: "visible", timeout: 60_000 });
    const retryPage = await context.newPage();
    await retryPage.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
    await retryPage.locator(".connection-state.connected").waitFor({ state: "visible", timeout: 60_000 });
    runtimeAnchor = process.hrtime.bigint();
    await recordOperator({
      type: "runtime_anchor",
      wallTime: new Date().toISOString(),
      frameId: initialFrame.frame_id,
      frameCaptureId: tracker.captureFor(initialFrame)?.id,
      frameCaptureOrder: tracker.captureFor(initialFrame)?.order,
      episodeId: initialFrame.episode_id,
      monotonicNs: runtimeAnchor.toString(),
    });
    await writeFrameLine({
      type: "runtime_anchor",
      wallTime: new Date().toISOString(),
      frameId: initialFrame.frame_id,
      frameCaptureId: tracker.captureFor(initialFrame)?.id,
      frameCaptureOrder: tracker.captureFor(initialFrame)?.order,
      episodeId: initialFrame.episode_id,
    });

    stopController = new AbortController();
    pollTask = pollRuntimeEvidence(baseUrl, tracker, writeFrameLine, stopController.signal);
    pauseTask = watchModelPauses({
      page: retryPage,
      tracker,
      record: recordOperator,
      anchor: runtimeAnchor,
      targetWallSeconds: runMetadata.targetWallSeconds,
      signal: stopController.signal,
    });
    await recordOperator({ type: "server_ready", frameId: initialFrame.frame_id, baseUrl });

    if (smoke) {
      const smokeEvents = [
        { kind: "focus", atMs: 0, order: 0 },
        { kind: "fleet", atMs: 0, order: 1 },
      ];
      await Promise.all(smokeEvents.map((event) => runIntervention({
        event,
        page: event.kind === "focus" ? focusPage : fleetPage,
        tracker,
        random,
        previousFocusBoxes,
        anchor: runtimeAnchor,
        smoke: true,
        screenshotsDir,
        recordOperator,
        auditEvents,
      })));
      await focusPage.screenshot({ path: join(screenshotsDir, "final.png"), fullPage: true });
      await recordOperator({ type: "smoke_operations_finished", wallTime: new Date().toISOString() });
      const status = await childDone;
      childStatus = status;
      smokeSummary = {
        acceptance: false,
        processExitCode: status.code,
        processSignal: status.signal,
        focusConfirmed: auditEvents.some((event) => event.kind === "focus" && event.status === "confirmed"),
        fleetConfirmed: auditEvents.some((event) => event.kind === "fleet" && event.status === "confirmed"),
        auditEvents,
      };
    } else {
      for (const group of groupByOffset(operatorSchedule())) {
        await waitUntil(runtimeAnchor + BigInt(group.atMs) * 1_000_000n, child, childDone, stopController.signal);
        await Promise.all(group.events.map((event) => runIntervention({
          event,
          page: event.kind === "focus" ? focusPage : fleetPage,
          tracker,
          random,
          previousFocusBoxes,
          anchor: runtimeAnchor,
          smoke: false,
          screenshotsDir,
          recordOperator,
          auditEvents,
        })));
      }
      await recordOperator({ type: "scheduled_interventions_finished", elapsedMs: elapsedMs(runtimeAnchor) });
      const status = await childDone;
      childStatus = status;
      try {
        await focusPage.screenshot({ path: join(screenshotsDir, "final.png"), fullPage: true });
      } catch (error) {
        await recordOperator({ type: "final_screenshot_failed", message: errorMessage(error) });
      }
    }

    disposeFrames();
  } catch (error) {
    failure = error;
    await recordOperator({
      type: "runner_failure",
      message: errorMessage(error),
      wallTime: new Date().toISOString(),
      elapsedMs: runtimeAnchor ? elapsedMs(runtimeAnchor) : null,
    }).catch(() => {});
    stopController?.abort(error);
  } finally {
    process.off("SIGINT", onInterrupt);
    process.off("SIGTERM", onInterrupt);
    stopController?.abort();
    if (context) await context.close().catch(() => {});
    await browser.close().catch(() => {});
    if (child && childStatus === null && child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      const stopped = await Promise.race([
        childDone.then(() => true),
        delay(10_000).then(() => false),
      ]);
      if (!stopped) child.kill();
      childStatus = await childDone;
    }
    if (pollTask) await pollTask.catch(() => {});
    if (pauseTask) await pauseTask.catch(() => {});
    await Promise.all([writeOperatorLine.flush(), writeFrameLine.flush()]).catch((error) => {
      evidenceErrors.push(String(error));
    });
    if (consoleStream) {
      await new Promise((resolveClose) => consoleStream.end(resolveClose));
    }
  }

  if (evidenceErrors.length) {
    failure ||= new Error(`evidence write failed: ${evidenceErrors[0]}`);
  }
  if (!smoke) {
    let report = null;
    try {
      report = JSON.parse(await readFile(join(paths.reportDir, "report.json"), "utf8"));
    } catch (error) {
      failure ||= new Error(`main.py did not produce report.json: ${errorMessage(error)}`);
    }
    const ledger = await readJsonLines(paths.operator).catch(() => []);
    const frameRecords = await readJsonLines(paths.frames).catch(() => []);
    audit = auditAcceptance({
      report,
      operatorEvents: ledger.filter((item) => item.type === "scenario_event"),
        frames: frameRecords.filter((item) => item.type === "frame").map((item) => ({
          ...item.frame,
          capture_id: item.captureId,
          capture_order: item.captureOrder,
        })),
    });
    audit.processExitCode = childStatus?.code ?? null;
    audit.processSignal = childStatus?.signal ?? null;
    audit.outputDir = runDir;
    if (childStatus?.code !== 0 || childStatus?.signal) {
      audit.failures.push(`main.py exited abnormally (code=${childStatus?.code}, signal=${childStatus?.signal})`);
      audit.passed = false;
    }
    await writeFile(join(runDir, "audit.json"), JSON.stringify(audit, null, 2) + "\n", { flag: "wx" });
  } else {
    smokeSummary ||= {
      acceptance: false,
      processExitCode: childStatus?.code ?? null,
      processSignal: childStatus?.signal ?? null,
      focusConfirmed: auditEvents.some((event) => event.kind === "focus" && event.status === "confirmed"),
      fleetConfirmed: auditEvents.some((event) => event.kind === "fleet" && event.status === "confirmed"),
      auditEvents,
    };
    await writeFile(join(runDir, "smoke.json"), JSON.stringify(smokeSummary, null, 2) + "\n", { flag: "wx" });
    if (smokeSummary.processExitCode !== 0) failure ||= new Error("smoke main.py process did not exit cleanly");
    if (!smokeSummary.focusConfirmed || !smokeSummary.fleetConfirmed) {
      failure ||= new Error("smoke run did not confirm both focus and fleet/AIS interactions");
    }
  }

  await writeOperatorLine({
    type: "run_finished",
    acceptance: !smoke,
    endedAt: new Date().toISOString(),
    processExitCode: childStatus?.code ?? null,
    processSignal: childStatus?.signal ?? null,
    auditPassed: audit?.passed ?? null,
    failure: failure ? errorMessage(failure) : null,
  }).catch(() => {});
  await writeOperatorLine.flush().catch(() => {});

  process.stdout.write(`Run evidence: ${runDir}\n`);
  if (audit) process.stdout.write(`Six-hour audit: ${audit.passed ? "PASS" : "FAIL"}\n`);
  if (failure) throw Object.assign(failure, { runDir, audit });
  if (audit && !audit.passed) throw Object.assign(new Error(audit.failures.join("; ")), { runDir, audit });
  return { runDir, audit, smokeSummary };
}

function runPreflight() {
  const code = [
    "import json, os",
    "from src.schedule.config_loader import ConfigLoader",
    "from src.schedule.env_loader import EnvLoader",
    "EnvLoader.load_dotenv()",
    "config = ConfigLoader.load('configs')",
    "print(json.dumps({'clearOutputsBeforeRun': config.common.clear_outputs_before_run, 'longcatKeyConfigured': bool(os.environ.get('LONGCAT_API_KEY'))}))",
  ].join("; ");
  const result = spawnSync(PYTHON, ["-c", code], {
    cwd: ROOT,
    encoding: "utf8",
    windowsHide: true,
    timeout: 60_000,
  });
  if (result.error) throw new Error(`preflight could not run Python: ${result.error.message}`);
  if (result.status !== 0) {
    throw new Error(`preflight could not load runtime config: ${(result.stderr || result.stdout).trim()}`);
  }
  try {
    return JSON.parse(result.stdout.trim());
  } catch {
    throw new Error("preflight returned invalid runtime configuration status");
  }
}

async function createRunDirectory(outputsDir, smoke, seed) {
  const timestamp = new Date().toISOString().replace(/[^0-9TZ]/g, "");
  const name = `live-interaction-${smoke ? "smoke-" : ""}${timestamp}-${seed}-${randomUUID().slice(0, 8)}`;
  const runDir = join(outputsDir, name);
  await mkdir(runDir, { recursive: false });
  return runDir;
}

function createJsonlWriter(filePath) {
  let chain = Promise.resolve();
  const write = (record) => {
    chain = chain.then(() => appendFile(filePath, JSON.stringify(record) + "\n", "utf8"));
    return chain;
  };
  write.flush = () => chain;
  return write;
}

async function findAvailablePort() {
  const listener = net.createServer();
  await new Promise((resolveListen, reject) => {
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", resolveListen);
  });
  const port = listener.address().port;
  await new Promise((resolveClose, reject) => listener.close((error) => error ? reject(error) : resolveClose()));
  return port;
}

function expectedMainFrameLog(outputsDir, date) {
  const pad = (value) => String(value).padStart(2, "0");
  const timestamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}_`
    + `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
  return join(outputsDir, `simulation_${timestamp}.jsonl`);
}

async function reserveMainFrameLog(outputsDir) {
  while (true) {
    const target = expectedMainFrameLog(outputsDir, new Date());
    try {
      await writeFile(target, "", { flag: "wx" });
      return target;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      await delay(1000 - new Date().getMilliseconds() + 25);
    }
  }
}

function observeChild(child) {
  return new Promise((resolveChild) => {
    let settled = false;
    const settle = (status) => {
      if (settled) return;
      settled = true;
      resolveChild(status);
    };
    child.once("error", (error) => settle({ code: null, signal: null, error: error.message }));
    child.once("close", (code, signal) => settle({ code, signal, error: null }));
  });
}

function pipeChildOutput(stream, name, destination) {
  stream.on("data", (chunk) => {
    destination.write(chunk);
    process.stdout.write(name === "stderr" ? `[main stderr] ${chunk}` : chunk);
  });
}

async function waitForServer(baseUrl, child, childDone) {
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      const status = await childDone;
      throw new Error(`main.py exited before the live server was ready (code=${status.code}, signal=${status.signal})`);
    }
    try {
      const response = await fetch(`${baseUrl}/api/runtime/logs?after=0`, {
        signal: AbortSignal.timeout(3000),
      });
      if (response.ok) return;
    } catch {
      // The connectivity probe and server startup happen before this endpoint is available.
    }
    await delay(250);
  }
  throw new Error("timed out waiting for the live server after LongCat connectivity probe");
}

async function pollRuntimeEvidence(baseUrl, tracker, writeFrameLine, signal) {
  let cursor = 0;
  const seenDecisions = new Set();
  let lastPollError = "";
  while (!signal.aborted) {
    try {
      const episodeId = tracker.latest?.episode_id;
      const logResponse = await fetch(`${baseUrl}/api/runtime/logs?after=${cursor}`, {
        signal: AbortSignal.timeout(5000),
      });
      if (logResponse.ok) {
        const payload = await logResponse.json();
        for (const entry of payload.entries || []) {
          cursor = Math.max(cursor, Number(entry.id) || 0);
          await writeFrameLine({ type: "runtime_log", receivedAt: new Date().toISOString(), entry });
        }
      }
      if (episodeId) {
        const decisionResponse = await fetch(
          `${baseUrl}/api/runtime/decisions?episode_id=${encodeURIComponent(episodeId)}`,
          { signal: AbortSignal.timeout(5000) },
        );
        if (decisionResponse.ok) {
          const payload = await decisionResponse.json();
          for (const decision of payload.decisions || []) {
            const key = JSON.stringify(decision);
            if (seenDecisions.has(key)) continue;
            seenDecisions.add(key);
            await writeFrameLine({
              type: "decision",
              receivedAt: new Date().toISOString(),
              episodeId,
              decision,
            });
          }
        }
      }
      lastPollError = "";
    } catch (error) {
      if (signal.aborted) break;
      const message = errorMessage(error);
      if (message !== lastPollError) {
        lastPollError = message;
        await writeFrameLine({ type: "poll_error", receivedAt: new Date().toISOString(), message }).catch(() => {});
      }
    }
    await sleep(2000, signal);
  }
}

async function watchModelPauses({ page, tracker, record, anchor, targetWallSeconds, signal }) {
  const handled = new Set();
  while (!signal.aborted) {
    const frame = tracker.latest;
    if (frame?.runtime_status === "paused_model") {
      const blockedCall = [...(frame.model_calls || [])].reverse()
        .find((call) => call.role === frame.blocked_role);
      const pauseKey = [frame.episode_id, frame.sim_time_min, frame.blocked_role, blockedCall?.call_id]
        .map((value) => value ?? "").join(":");
      if (!handled.has(pauseKey)) {
        handled.add(pauseKey);
        await record({
          type: "model_pause",
          pauseKey,
          status: "observed",
          blockedRole: frame.blocked_role,
          simTimeMin: frame.sim_time_min,
          frameId: frame.frame_id,
          elapsedMs: elapsedMs(anchor),
        });
        if (elapsedMs(anchor) >= targetWallSeconds * 1000 - 60_000) {
          await record({
            type: "model_pause",
            pauseKey,
            status: "observed_no_retry_near_wall_end",
            frameId: frame.frame_id,
            elapsedMs: elapsedMs(anchor),
          });
          await sleep(500, signal);
          continue;
        }
        try {
          const result = await retryPausedRuntime(page, tracker, {
            timeoutMs: 300_000,
            record: (event) => record({ type: "ui_transition", ...event }),
          });
          await record({
            type: "model_pause",
            pauseKey,
            status: "retry_confirmed",
            commandId: result.commandId,
            frameId: result.frameId,
            frameCaptureId: result.confirmedCaptureId,
            elapsedMs: elapsedMs(anchor),
          });
        } catch (error) {
          await record({
            type: "model_pause",
            pauseKey,
            status: "retry_failed",
            message: errorMessage(error),
            elapsedMs: elapsedMs(anchor),
          });
        }
      }
    } else if (frame?.runtime_status === "paused_safety") {
      const pauseKey = `${frame.episode_id}:${frame.sim_time_min}:paused_safety`;
      if (!handled.has(pauseKey)) {
        handled.add(pauseKey);
        await record({
          type: "safety_pause",
          status: "observed_no_automatic_retry",
          simTimeMin: frame.sim_time_min,
          frameId: frame.frame_id,
          elapsedMs: elapsedMs(anchor),
        });
      }
    }
    await sleep(500, signal);
  }
}

async function runIntervention({
  event,
  page,
  tracker,
  random,
  previousFocusBoxes,
  anchor,
  smoke,
  screenshotsDir,
  recordOperator,
  auditEvents,
}) {
  const issuedElapsedMs = anchor ? elapsedMs(anchor) : 0;
  await recordOperator({
    type: "scheduled",
    kind: event.kind,
    atMs: event.atMs,
    issuedElapsedMs,
    order: event.order,
    smoke,
  });
  if (event.kind === "focus") {
    const sourceFrame = tracker.latest;
    const sourceCapture = tracker.captureFor(sourceFrame);
    let bbox = null;
    const auditEvent = {
      kind: "focus",
      atMs: event.atMs,
      issuedElapsedMs,
      status: "failed",
      selectedFrameId: sourceFrame?.frame_id,
      selectedFrameCaptureId: sourceCapture?.id,
      selectedFrameCaptureOrder: sourceCapture?.order,
    };
    try {
      bbox = chooseFocusBBox(sourceFrame, random, previousFocusBoxes);
      previousFocusBoxes.push(bbox);
      const label = `Operator focus ${previousFocusBoxes.length}`;
      await recordOperator({
        type: "focus_selected", bbox, label, frameId: sourceFrame.frame_id,
        frameCaptureId: sourceCapture?.id, frameCaptureOrder: sourceCapture?.order, atMs: event.atMs,
      });
      const result = await drawFocusArea(page, tracker, {
        bbox,
        label,
        durationMin: 360,
        record: (transition) => recordOperator({ type: "ui_transition", ...transition }),
      });
      auditEvent.status = "confirmed";
      auditEvent.intentId = result.state.intent.intent_id;
      auditEvent.bbox = bbox;
      auditEvent.selectedFrameId = result.sourceFrameId;
      auditEvent.selectedFrameCaptureId = result.sourceCaptureId;
      auditEvent.selectedFrameCaptureOrder = result.sourceCaptureOrder;
      auditEvent.confirmedFrameId = result.frameId;
      auditEvent.confirmedFrameCaptureId = result.confirmedCaptureId;
      auditEvent.confirmedFrameCaptureOrder = result.confirmedCaptureOrder;
      auditEvent.commandId = result.commandId;
      auditEvent.receipt = result.receipt;
      const screenshot = `focus-${String(event.atMs).padStart(8, "0")}.png`;
      await page.screenshot({ path: join(screenshotsDir, screenshot), fullPage: true })
        .catch((error) => recordOperator({ type: "screenshot_failed", screenshot, message: errorMessage(error) }));
      auditEvent.screenshot = screenshot;
    } catch (error) {
      auditEvent.bbox = bbox;
      auditEvent.error = errorMessage(error);
      await recordOperator({ type: "intervention_failed", kind: "focus", atMs: event.atMs, message: auditEvent.error });
    }
    auditEvents.push(auditEvent);
    await recordOperator({ type: "scenario_event", ...auditEvent });
    return;
  }

  const startingFrame = tracker.latest;
  const startingCapture = tracker.captureFor(startingFrame);
  const auditEvent = {
    kind: "fleet",
    atMs: event.atMs,
    issuedElapsedMs,
    status: "failed",
    round: event.round || Math.round(event.atMs / 5_400_000),
    selectionFrameId: startingFrame?.frame_id,
    selectionFrameCaptureId: startingCapture?.id,
    selectionFrameCaptureOrder: startingCapture?.order,
    operations: [],
  };
  try {
    const targets = chooseFleetTargets(startingFrame, random);
    await recordOperator({
      type: "fleet_selection", round: auditEvent.round, ...targets,
      frameId: startingFrame.frame_id,
      frameCaptureId: startingCapture?.id,
      frameCaptureOrder: startingCapture?.order,
    });
    for (const [vesselClass, vesselId] of [
      ["type_i", targets.replaceTypeIId],
      ["type_ii", targets.replaceTypeIIId],
    ]) {
      const operation = {
        kind: "delete",
        status: "failed",
        vesselId,
        vesselClass,
        selectionFrameId: startingFrame.frame_id,
        selectionFrameCaptureId: startingCapture?.id,
        selectionFrameCaptureOrder: startingCapture?.order,
      };
      try {
        const result = await deleteVessel(page, tracker, vesselId, {
          record: (transition) => recordOperator({ type: "ui_transition", round: auditEvent.round, ...transition }),
        });
        operation.status = "confirmed";
        operation.frameId = result.frameId;
        operation.frameCaptureId = result.confirmedCaptureId;
        operation.frameCaptureOrder = result.confirmedCaptureOrder;
        auditEvent.operations.push(operation);
        await recordOperator({ type: "fleet_operation", round: auditEvent.round, ...operation });
      } catch (error) {
        operation.error = errorMessage(error);
        auditEvent.operations.push(operation);
        await recordOperator({ type: "fleet_operation", round: auditEvent.round, ...operation });
        continue;
      }

      const createOperation = {
        kind: "create",
        status: "failed",
        replacementFor: vesselId,
        vesselClass,
      };
      try {
        const placementFrame = tracker.latest;
        const oldVessel = startingFrame.scenario_vessels.find((vessel) =>
          vessel.scenario_entity_id === vesselId);
        const cell = choosePlacementCell(placementFrame, random, [oldVessel?.position]);
        const placement = await selectVesselPlacement(page, tracker, vesselClass, cell, {
          record: (transition) => recordOperator({ type: "ui_transition", round: auditEvent.round, ...transition }),
        });
        Object.assign(createOperation, {
          status: "confirmed",
          vesselId: placement.vessel.scenario_entity_id,
          position: placement.vessel.position,
          frameId: placement.frameId,
          frameCaptureId: placement.confirmedCaptureId,
          frameCaptureOrder: placement.confirmedCaptureOrder,
        });
        await recordOperator({ type: "fleet_operation", round: auditEvent.round, ...createOperation, cell });
      } catch (error) {
        createOperation.error = errorMessage(error);
      }
      auditEvent.operations.push(createOperation);
      if (createOperation.status !== "confirmed") {
        await recordOperator({ type: "fleet_operation", round: auditEvent.round, ...createOperation });
      }
    }

    const aisFrame = tracker.latest;
    const aisSelectionCapture = tracker.captureFor(aisFrame);
    auditEvent.aisSelectionFrameId = aisFrame?.frame_id;
    auditEvent.aisSelectionFrameCaptureId = aisSelectionCapture?.id;
    auditEvent.aisSelectionFrameCaptureOrder = aisSelectionCapture?.order;
    const aisTargets = chooseAisTargets(aisFrame, random);
    const selectedAis = smoke
      ? [aisTargets[Math.floor(random() * aisTargets.length)]]
      : aisTargets;
    for (const vesselId of selectedAis) {
      const priorFrame = tracker.latest;
      const prior = priorFrame.scenario_vessels.find((vessel) => vessel.scenario_entity_id === vesselId);
      const operation = {
        kind: "ais",
        status: "failed",
        vesselId,
        vesselClass: "type_ii",
        aisEnabled: !prior.ais_enabled,
        selectionFrameId: priorFrame.frame_id,
        selectionFrameCaptureId: tracker.captureFor(priorFrame)?.id,
        selectionFrameCaptureOrder: tracker.captureFor(priorFrame)?.order,
      };
      try {
        const result = await setVesselAis(page, tracker, vesselId, operation.aisEnabled, {
          record: (transition) => recordOperator({ type: "ui_transition", round: auditEvent.round, ...transition }),
        });
        operation.status = "confirmed";
        operation.frameId = result.frameId;
        operation.frameCaptureId = result.confirmedCaptureId;
        operation.frameCaptureOrder = result.confirmedCaptureOrder;
        auditEvent.operations.push(operation);
        await recordOperator({ type: "fleet_operation", round: auditEvent.round, ...operation });
      } catch (error) {
        operation.error = errorMessage(error);
        auditEvent.operations.push(operation);
        await recordOperator({ type: "fleet_operation", round: auditEvent.round, ...operation });
      }
    }
    auditEvent.status = auditEvent.operations.length === 2 + 2 + selectedAis.length
      && auditEvent.operations.every((operation) => operation.status === "confirmed")
      ? "confirmed" : "partial";
    const screenshot = `fleet-${String(event.atMs).padStart(8, "0")}.png`;
    await page.screenshot({ path: join(screenshotsDir, screenshot), fullPage: true })
      .catch((error) => recordOperator({ type: "screenshot_failed", screenshot, message: errorMessage(error) }));
    auditEvent.screenshot = screenshot;
  } catch (error) {
    auditEvent.error = errorMessage(error);
    await recordOperator({ type: "intervention_failed", kind: "fleet", atMs: event.atMs, message: auditEvent.error });
  }
  auditEvents.push(auditEvent);
  await recordOperator({ type: "scenario_event", ...auditEvent });
}

function choosePlacementCell(frame, random, excludedPositions = []) {
  const cells = frame?.search_domain?.searchable_cells;
  if (!Array.isArray(cells) || !cells.length) throw new Error("no searchable cells for vessel placement");
  const cols = Number(frame.search_domain.cols) || 30;
  const rows = Number(frame.search_domain.rows) || 30;
  const ships = frame.scenario_vessels || [];
  const candidates = cells.filter((cell) => Array.isArray(cell) && cell.length === 2
    && Number.isInteger(cell[0]) && Number.isInteger(cell[1])
    && cell[0] >= 1 && cell[0] < cols - 1 && cell[1] >= 1 && cell[1] < rows - 1
    && [...ships.map((vessel) => vessel.position), ...excludedPositions].every((position) =>
      !Array.isArray(position)
      || Math.hypot(position[0] - (cell[0] + 0.5), position[1] - (cell[1] + 0.5)) >= 1));
  if (!candidates.length) throw new Error("no legal unoccupied cell for vessel placement");
  return candidates[Math.floor(random() * candidates.length)];
}

function groupByOffset(events) {
  const groups = [];
  for (const event of events) {
    let group = groups.at(-1);
    if (!group || group.atMs !== event.atMs) {
      group = { atMs: event.atMs, events: [] };
      groups.push(group);
    }
    group.events.push(event);
  }
  return groups;
}

async function waitUntil(targetNs, child, childDone, signal) {
  while (process.hrtime.bigint() < targetNs) {
    if (signal.aborted) throw signal.reason || new Error("run interrupted");
    if (child.exitCode !== null || child.signalCode !== null) {
      const status = await childDone;
      throw new Error(`main.py exited before scheduled intervention (code=${status.code}, signal=${status.signal})`);
    }
    const remainingMs = Number((targetNs - process.hrtime.bigint()) / 1_000_000n);
    await sleep(Math.max(1, Math.min(1000, remainingMs)), signal);
  }
}

function elapsedMs(anchor) {
  return Number((process.hrtime.bigint() - anchor) / 1_000_000n);
}

async function sleep(milliseconds, signal) {
  if (signal?.aborted) return;
  try {
    await delay(milliseconds, undefined, signal ? { signal } : undefined);
  } catch (error) {
    if (error.name !== "AbortError") throw error;
  }
}

async function readJsonLines(filePath) {
  const text = await readFile(filePath, "utf8");
  return text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = parseArgs();
    if (options.help) {
      process.stdout.write("Usage: node scripts/run_live_interaction_acceptance.mjs [--smoke] --seed <uint32>\n");
    } else {
      await runScenario(options);
    }
  } catch (error) {
    process.stderr.write(`Live interaction run failed: ${errorMessage(error)}\n`);
    if (error.runDir) process.stderr.write(`Partial evidence: ${error.runDir}\n`);
    process.exitCode = 1;
  }
}
