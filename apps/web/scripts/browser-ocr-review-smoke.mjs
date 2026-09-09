import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";

// Uses an existing Chromium browser and its debugging protocol; no test dependency.
const [browserPath, pdfPath, casesPath, outputPath, origin = "http://127.0.0.1:3001"] = process.argv.slice(2);
if (!outputPath) throw new Error("Usage: node browser-ocr-review-smoke.mjs browser.exe document.pdf cases.json output-directory [origin]");
const output = path.resolve(outputPath);
await mkdir(output, { recursive: true });
const profile = path.join(output, `browser-${randomUUID()}`);
const browser = spawn(browserPath, ["--headless=new", "--disable-gpu", "--no-first-run", "--disable-background-networking", "--disable-component-update", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { windowsHide: true, stdio: "ignore" });
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let socket;
const requests = [];
const failures = [];
const report = { cases: [], requests, failures };
try {
  let port;
  for (let i = 0; i < 100; i++) {
    try { port = Number((await readFile(path.join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]); break; }
    catch { await pause(100); }
  }
  if (!port) throw new Error("Browser debugging port was not created.");
  const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  socket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let id = 0;
  const pending = new Map();
  function send(method, params = {}, sessionId) {
    return new Promise((resolve, reject) => {
      const key = ++id;
      const timer = setTimeout(() => { pending.delete(key); reject(new Error(`Timeout: ${method}`)); }, 60000);
      pending.set(key, { resolve, reject, timer });
      socket.send(JSON.stringify({ id: key, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
  const allowed = (url) => /^(data:|blob:|about:)/.test(url) || ["127.0.0.1", "localhost", "[::1]"].includes(new URL(url).hostname);
  async function prepare(sessionId) {
    await send("Network.enable", {}, sessionId);
    try { await send("Fetch.enable", { patterns: [{ urlPattern: "*" }] }, sessionId); }
    catch (error) {
      if (!String(error).includes("wasn't found")) throw error;
      report.workerInterception = "Worker requests observed using Network events; Fetch interception is unavailable for worker targets.";
    }
    await send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, sessionId);
  }
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const job = pending.get(message.id);
      if (!job) return;
      clearTimeout(job.timer);
      pending.delete(message.id);
      if (message.error) job.reject(new Error(JSON.stringify(message.error)));
      else job.resolve(message.result);
      return;
    }
    if (message.method === "Target.attachedToTarget") {
      const session = message.params.sessionId;
      prepare(session).catch((error) => failures.push(String(error))).finally(() => send("Runtime.runIfWaitingForDebugger", {}, session).catch((error) => failures.push(String(error))));
    }
    if (message.method === "Fetch.requestPaused") {
      const { requestId, request } = message.params;
      const local = allowed(request.url);
      requests.push({ url: request.url, method: request.method, local, session: message.sessionId });
      send(local ? "Fetch.continueRequest" : "Fetch.failRequest", local ? { requestId } : { requestId, errorReason: "BlockedByClient" }, message.sessionId).catch((error) => failures.push(String(error)));
    }
    if (message.method === "Network.requestWillBeSent") {
      const request = message.params.request;
      requests.push({ url: request.url, method: request.method, local: allowed(request.url), session: message.sessionId, observed: true });
    }
  };
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  await prepare(sessionId);
  await send("Page.enable", {}, sessionId);
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);
  async function evaluate(expression) {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async function until(expression, timeout = 120000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      if (await evaluate(expression)) return;
      await pause(200);
    }
    throw new Error(`Timed out waiting for ${expression}`);
  }
  const click = (label) => evaluate(`(() => { const button = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === ${JSON.stringify(label)}); if (!button || button.disabled) throw new Error('Unavailable button: ' + ${JSON.stringify(label)}); button.click(); })()`);
  await send("Page.navigate", { url: origin }, sessionId);
  await until("!!document.querySelector('input[type=file]')");
  await until("document.querySelector('.ocr-panel strong')?.textContent.includes('OCR je pripravljen')");
  const { root } = await send("DOM.getDocument", {}, sessionId);
  const { nodeId } = await send("DOM.querySelector", { nodeId: root.nodeId, selector: "input[type=file]" }, sessionId);
  await send("DOM.setFileInputFiles", { nodeId, files: [path.resolve(pdfPath)] }, sessionId);
  await until("[...document.querySelectorAll('button')].some(b => b.textContent.trim() === 'Analiziraj PDF' && !b.disabled)");
  await click("Analiziraj PDF");
  await until("document.querySelectorAll('.page-detail').length > 0");
  report.initialCandidates = await evaluate("document.querySelectorAll('.ocr-candidate').length");
  await click("OCR-preveri sumljive dele");
  await until("!!document.querySelector('.ocr-panel progress')");
  await click("Prekliči OCR");
  await until("!document.querySelector('.ocr-panel progress')");
  report.cancelledItemsPending = await evaluate("[...document.querySelectorAll('.ocr-candidate')].every(card => card.classList.contains('ocr-status-pending'))");
  if (!report.cancelledItemsPending) throw new Error("Cancelled queue did not retain pending candidates.");
  await click("OCR-preveri sumljive dele");
  await until("!document.querySelector('.ocr-panel progress')");

  async function capture(name) {
    const result = await evaluate(`(() => { const card = [...document.querySelectorAll('.ocr-candidate')].at(-1); return { text: card.innerText, status: card.className, texts: [...card.querySelectorAll('.ocr-compare pre')].map(n => n.textContent), attempts: [...card.querySelectorAll('tbody tr')].map(row => [...row.cells].map(cell => cell.textContent)), crop: card.querySelector('a[download]')?.href ?? null }; })()`);
    if (result.crop) {
      await writeFile(path.join(output, `${name}.png`), Buffer.from(result.crop.split(",")[1], "base64"));
      result.crop = `${name}.png`;
    }
    report.cases.push({ name, ...result });
    console.log(JSON.stringify({ name, texts: result.texts, attempts: result.attempts }));
  }
  await capture("automatic");
  const beforeAcceptance = report.cases[0].texts;
  await click("Uporabi OCR rezultat");
  const afterAcceptance = await evaluate("[...document.querySelector('.ocr-candidate').querySelectorAll('.ocr-compare pre')].map(node => node.textContent)");
  report.manualAcceptance = { before: beforeAcceptance, after: afterAcceptance };
  if (afterAcceptance[0] !== beforeAcceptance[0] || afterAcceptance[2] !== beforeAcceptance[1]) throw new Error("Manual acceptance changed native text or selected the wrong text.");
  await click("Ohrani izvirnik");
  const cases = JSON.parse(await readFile(casesPath, "utf8"));
  await click("Izberi območje za OCR");
  for (const [index, area] of cases.entries()) {
    const rect = await evaluate(`(() => { const node = document.querySelectorAll('.manual-selection-layer')[${area.page_number - 1}]; node.scrollIntoView({block:'center'}); const r = node.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; })()`);
    const [x0, y0, x1, y1] = area.bbox;
    const start = { x: rect.x + x0 / area.page_width * rect.width, y: rect.y + y0 / area.page_height * rect.height };
    const end = { x: rect.x + x1 / area.page_width * rect.width, y: rect.y + y1 / area.page_height * rect.height };
    await send("Input.dispatchMouseEvent", { type: "mousePressed", ...start, button: "left", clickCount: 1 }, sessionId);
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", ...end, button: "left", buttons: 1 }, sessionId);
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", ...end, button: "left", clickCount: 1 }, sessionId);
    await click("Prikaži izrez pri 300 DPI");
    await until("!!document.querySelector('img[alt=\"Izbrani izrez pred OCR\"]')");
    if (index === 0) {
      await click("Ponovi izbor");
      report.reselectionClearedCrop = await evaluate("!document.querySelector('img[alt=\"Izbrani izrez pred OCR\"]')");
      await send("Input.dispatchMouseEvent", { type: "mousePressed", ...start, button: "left", clickCount: 1 }, sessionId);
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", ...end, button: "left", buttons: 1 }, sessionId);
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", ...end, button: "left", clickCount: 1 }, sessionId);
      await click("Prikaži izrez pri 300 DPI");
      await until("!!document.querySelector('img[alt=\"Izbrani izrez pred OCR\"]')");
    }
    if (area.mode) await evaluate(`(() => { const select = document.querySelector('.ocr-crop select'); select.value = ${JSON.stringify(area.mode)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    const beforeCrop = await evaluate("document.querySelector('img[alt=\"Izbrani izrez pred OCR\"]').src");
    await writeFile(path.join(output, `${area.name}-before.png`), Buffer.from(beforeCrop.split(",")[1], "base64"));
    await click("Zaženi OCR izreza");
    await until("!document.querySelector('.ocr-panel progress')");
    await capture(area.name);
  }
  await evaluate("document.querySelector('.ocr-results').scrollIntoView({block:'start'})");
  const screenshot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }, sessionId);
  await writeFile(path.join(output, "review.png"), Buffer.from(screenshot.data, "base64"));
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true }, sessionId);
  await evaluate("document.querySelector('.ocr-results').scrollIntoView({block:'start'})");
  const mobile = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }, sessionId);
  await writeFile(path.join(output, "review-mobile.png"), Buffer.from(mobile.data, "base64"));
  report.mobileOverflow = await evaluate("document.documentElement.scrollWidth > window.innerWidth");
  if (report.mobileOverflow) throw new Error("Mobile page has horizontal overflow.");
  report.externalUrls = [...new Set(requests.filter((request) => !request.local).map((request) => request.url))];
  report.runtimeUrls = [...new Set(requests.filter((request) => request.url.includes("/vendor/tesseract/")).map((request) => request.url))];
  if (report.externalUrls.length || failures.length) throw new Error("Network audit failed; see report.");
} catch (error) {
  report.error = String(error);
  process.exitCode = 1;
  console.error(error);
} finally {
  await writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2));
  socket?.close();
  browser.kill();
}
