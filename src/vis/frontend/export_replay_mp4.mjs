import { chromium } from "playwright";
import fs from "fs";

const TARGET_FILE = "simulation_20261003_204349.jsonl";
const OUT = "/home/ubuntu/repos/Maritime-Surveillance/outputs/run4-replay-6h.mp4";

const browser = await chromium.connectOverCDP("http://localhost:29229");
const context = browser.contexts()[0];
const page = context.pages().find((p) => p.url().startsWith("http://localhost:8765"))
  ?? await context.newPage();
page.setDefaultTimeout(180000);

if (!page.url().startsWith("http://localhost:8765")) {
  await page.goto("http://localhost:8765", { waitUntil: "domcontentloaded" });
}
await page.waitForTimeout(1500);
await page.bringToFront();

const replayBtn = page.getByRole("button", { name: "回放" });
if (await replayBtn.count()) await replayBtn.first().click();
await page.waitForSelector("select.file-select", { timeout: 15000 });

const already = await page.evaluate(() => document.querySelector("select.file-select")?.value);
if (already !== TARGET_FILE) {
  const cur = await page.selectOption("select.file-select", TARGET_FILE).catch(() => []);
  console.log("selected:", JSON.stringify(cur));
} else {
  console.log("already selected:", already);
}

// wait for the initial chunk to land (connection label shows "N 帧")
await page.waitForFunction(() => {
  const el = document.querySelector(".connection-state");
  return el && /\d+ 帧/.test(el.innerText || "");
}, { timeout: 180000 });
console.log("initial frames loaded");

await page.waitForFunction(() => {
  const btn = document.querySelector("button.export-mp4-btn");
  return btn && !btn.disabled;
}, { timeout: 120000 });
console.log("export button enabled");

let done = false;
const downloadPromise = page.waitForEvent("download", { timeout: 900000 })
  .then((d) => { done = true; return d; });
await page.click("button.export-mp4-btn");
console.log("clicked export");

const t0 = Date.now();
while (!done && Date.now() - t0 < 840000) {
  await page.waitForTimeout(15000);
  const state = await page.evaluate(() => {
    const btn = document.querySelector("button.export-mp4-btn");
    return {
      label: btn?.getAttribute("aria-label") ?? null,
      title: btn?.getAttribute("title") ?? null,
      disabled: btn?.disabled ?? null,
      text: btn?.innerText?.trim() ?? null,
    };
  });
  console.log(`[${Math.round((Date.now() - t0) / 1000)}s]`, JSON.stringify(state));
  if (state.title && state.title !== "下载回放 MP4" && !state.label?.includes("Exporting")) {
    console.log("export errored:", state.title);
    process.exit(3);
  }
}
const download = await downloadPromise;
await download.saveAs(OUT);
console.log("saved:", OUT, fs.statSync(OUT).size, "bytes");
await browser.close();
process.exit(0);
