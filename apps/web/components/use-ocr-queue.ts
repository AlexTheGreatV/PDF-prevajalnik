"use client";

import { useEffect, useRef, useState } from "react";
import { BrowserOcrProvider, explainBrowserOcrError, findExternalOcrRuntimeUrls } from "../lib/browser-ocr-provider";
import { markOcrFailed, type OcrReviewItem } from "../lib/ocr-review";

export function useOcrQueue() {
  const active = useRef<AbortController | null>(null);
  const [isRunning, setRunning] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [externalUrls, setExternalUrls] = useState<string[]>([]);

  useEffect(() => () => active.current?.abort(), []);

  function cancel() { active.current?.abort(); }

  async function run(
    items: OcrReviewItem[],
    recognize: (item: OcrReviewItem, provider: BrowserOcrProvider, signal: AbortSignal) => Promise<OcrReviewItem>,
    onItem: (item: OcrReviewItem) => void,
  ) {
    if (active.current || items.length === 0) return;
    const controller = new AbortController();
    active.current = controller;
    const provider = new BrowserOcrProvider();
    const before = new Set(performance.getEntriesByType("resource").map((entry) => entry.name));
    setRunning(true);
    setProgress({ done: 0, total: items.length });
    try {
      for (const [index, item] of items.entries()) {
        if (controller.signal.aborted) break;
        try {
          const result = await recognize(item, provider, controller.signal);
          onItem(controller.signal.aborted ? { ...item, status: "pending" } : result);
        } catch (error) {
          onItem(controller.signal.aborted ? { ...item, status: "pending" } : markOcrFailed(item, explainBrowserOcrError(error)));
        }
        setProgress({ done: index + 1, total: items.length });
      }
    } finally {
      try { await provider.terminate(); }
      finally {
        active.current = null;
        setRunning(false);
        setExternalUrls(findExternalOcrRuntimeUrls(performance.getEntriesByType("resource").map((entry) => entry.name).filter((url) => !before.has(url))));
      }
    }
  }

  return { run, cancel, isRunning, progress, externalUrls, setProgress, setExternalUrls };
}
