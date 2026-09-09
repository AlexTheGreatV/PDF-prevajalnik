"use client";

import { useEffect, useMemo, useState } from "react";

import {
  BrowserOcrProvider,
  checkBrowserOcrRuntime,
  explainBrowserOcrError,
  findExternalOcrRuntimeUrls,
  type BrowserOcrResult,
  type BrowserOcrSegmentationMode,
} from "../lib/browser-ocr-provider";

type SmokeState =
  | { kind: "idle"; message: string }
  | { kind: "running"; message: string }
  | {
      kind: "success";
      message: string;
      result: BrowserOcrResult;
      runtimeUrls: string[];
      observedUrls: string[];
      externalUrls: string[];
    }
  | { kind: "error"; message: string };

function createApplicationsCanvas(): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = 420;
  canvas.height = 110;
  const context = canvas.getContext("2d");
  if (!context) {
    throw new Error("Canvas ni na voljo.");
  }
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#111111";
  context.font = "700 48px Arial, sans-serif";
  context.textBaseline = "middle";
  context.fillText("APPLICATIONS", 20, 58);
  return canvas;
}

function dataUrlToImage(dataUrl: string): Promise<HTMLImageElement> {
  if (!/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(dataUrl)) return Promise.reject(new Error("Dovoljen je samo lokalni PNG data URL."));
  return new Promise((resolve, reject) => {
    const image = new window.Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("Diagnostične OCR slike ni bilo mogoče naložiti."));
    image.src = dataUrl;
  });
}

export function OcrSmoke() {
  const [state, setState] = useState<SmokeState>({
    kind: "idle",
    message: "OCR je pripravljen za lokalni preizkus.",
  });
  const [diagnosticDataUrl, setDiagnosticDataUrl] = useState("");
  const [diagnosticMode, setDiagnosticMode] = useState<BrowserOcrSegmentationMode>("single_line");
  const provider = useMemo(() => new BrowserOcrProvider(), []);

  useEffect(() => () => { void provider.terminate(); }, [provider]);

  async function runSmokeTest() {
    setState({ kind: "running", message: "Preverjam lokalne OCR datoteke ..." });
    try {
      const runtime = await checkBrowserOcrRuntime();
      const runtimeUrls = [
        "/vendor/tesseract/worker.min.js",
        "/vendor/tesseract/tesseract-core.wasm.js",
        "/vendor/tesseract/tesseract-core.wasm",
        "/vendor/tesseract/lang/eng.traineddata.gz",
      ];
      if (!runtime.ok) {
        setState({
          kind: "error",
          message: `OCR ni na voljo. Manjkajo lokalne datoteke: ${runtime.missing.join(", ")}`,
        });
        return;
      }

      setState({ kind: "running", message: "Berem majhno testno sliko ..." });
      const before = performance.getEntriesByType("resource").map((entry) => entry.name);
      const canvas = createApplicationsCanvas();
      const result = await provider.recognize({
        image: canvas,
        pixel_bbox: { x0: 0, y0: 0, x1: canvas.width, y1: canvas.height },
        pdf_bbox: null,
        reason: "smoke_test_applications",
      });
      const after = performance.getEntriesByType("resource").map((entry) => entry.name);
      const observedUrls = after.filter((entry) => !before.includes(entry));
      const externalUrls = findExternalOcrRuntimeUrls(observedUrls);
      const matched = /\bAPPLICATIONS\b/i.test(result.text);

      setState({
        kind: matched && externalUrls.length === 0 ? "success" : "error",
        message: matched
          ? "OCR je lokalno prebral testno sliko."
          : `OCR je vrnil nepričakovano besedilo: ${result.text || "(prazno)"}`,
        result,
        runtimeUrls,
        observedUrls,
        externalUrls,
      });
    } catch (error) {
      setState({ kind: "error", message: explainBrowserOcrError(error) });
    }
  }

  async function stopWorker() {
    await provider.terminate();
    setState({ kind: "idle", message: "OCR worker je zaprt." });
  }

  async function runDiagnosticDataUrl() {
    setState({ kind: "running", message: "Berem diagnostični PNG izrez ..." });
    try {
      const before = performance.getEntriesByType("resource").map((entry) => entry.name);
      const image = await dataUrlToImage(diagnosticDataUrl);
      const result = await provider.recognize({
        image,
        pixel_bbox: { x0: 0, y0: 0, x1: image.naturalWidth, y1: image.naturalHeight },
        pdf_bbox: null,
        reason: "diagnostic_data_url",
        preprocessing_method: "diagnostic-data-url",
        segmentation_mode: diagnosticMode,
      });
      const after = performance.getEntriesByType("resource").map((entry) => entry.name);
      const observedUrls = after.filter((entry) => !before.includes(entry));
      setState({
        kind: "success",
        message: "OCR je prebral diagnostični izrez.",
        result,
        runtimeUrls: [
          "/vendor/tesseract/worker.min.js",
          "/vendor/tesseract/tesseract-core.wasm.js",
          "/vendor/tesseract/tesseract-core.wasm",
          "/vendor/tesseract/lang/eng.traineddata.gz",
        ],
        observedUrls,
        externalUrls: findExternalOcrRuntimeUrls(observedUrls),
      });
    } catch (error) {
      setState({ kind: "error", message: explainBrowserOcrError(error) });
    }
  }

  return (
    <main className="app-shell">
      <section className="workspace">
        <div className="heading">
          <p>JUB/KOOP interno</p>
          <h1>OCR smoke test</h1>
        </div>
        <div className="panel">
          <p>{state.message}</p>
          <div className="actions">
            <button type="button" onClick={runSmokeTest} disabled={state.kind === "running"}>
              Zaženi OCR preizkus
            </button>
            <button type="button" onClick={stopWorker}>
              Zapri worker
            </button>
          </div>
          <div className="ocr-smoke-diagnostic">
            <label>
              Diagnostični data URL
              <textarea
                value={diagnosticDataUrl}
                onChange={(event) => setDiagnosticDataUrl(event.target.value)}
                placeholder="data:image/png;base64,..."
              />
            </label>
            <label>
              Segmentacija
              <select
                value={diagnosticMode}
                onChange={(event) => setDiagnosticMode(event.target.value as BrowserOcrSegmentationMode)}
              >
                <option value="single_line">ena vrstica</option>
                <option value="single_block">blok besedila</option>
                <option value="sparse_text">redko besedilo</option>
              </select>
            </label>
            <button type="button" onClick={runDiagnosticDataUrl} disabled={state.kind === "running" || !diagnosticDataUrl}>
              Preberi diagnostični izrez
            </button>
          </div>
          {state.kind === "success" || state.kind === "error" ? (
            <pre className="ocr-smoke-output">
              {JSON.stringify(
                {
                  state: state.kind,
                  result: "result" in state ? state.result : null,
                  runtimeUrls: "runtimeUrls" in state ? state.runtimeUrls : [],
                  observedUrls: "observedUrls" in state ? state.observedUrls : [],
                  externalUrls: "externalUrls" in state ? state.externalUrls : [],
                },
                null,
                2,
              )}
            </pre>
          ) : null}
        </div>
      </section>
    </main>
  );
}
