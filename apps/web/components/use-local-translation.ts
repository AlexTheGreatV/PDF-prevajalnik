"use client";

import { useEffect, useRef, useState } from "react";
import { LocalTranslationProvider, getLocalTranslationStatus, type LocalTranslationStatus } from "../lib/local-translation-provider";
import { translateJobBatch } from "../lib/translation-job";
import type { TranslationJob } from "../lib/translation-types";

export function useLocalTranslation(
  job: TranslationJob | null,
  setJob: React.Dispatch<React.SetStateAction<TranslationJob | null>>,
) {
  const [status, setStatus] = useState<LocalTranslationStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const [message, setMessage] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getLocalTranslationStatus(controller.signal)
      .then((next) => { setStatus(next); setStatusError(null); })
      .catch((error) => {
        if (!controller.signal.aborted) setStatusError(error instanceof Error ? error.message : "Status lokalnega prevajalnika ni dosegljiv.");
      });
    return () => controller.abort();
  }, []);

  async function translate() {
    if (!job || !status?.available || running) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    const original = job;
    const selected = original.segments.filter((segment) => ["pending", "failed"].includes(segment.status) && !segment.skip_reason);
    setProgress({ completed: 0, total: selected.length });
    setRunning(true);
    setMessage("Prevajam z lokalnim modelom …");
    setJob({
      ...original,
      segments: original.segments.map((segment) => selected.some((item) => item.segment_id === segment.segment_id)
        ? { ...segment, status: "translating" }
        : segment),
    });
    try {
      const translated = await translateJobBatch(
        original,
        new LocalTranslationProvider(),
        controller.signal,
        (completed, total) => setProgress({ completed, total }),
      );
      setJob(translated);
      if (controller.signal.aborted) setMessage("Prevajanje je bilo preklicano.");
      else if (translated.segments.some((segment) => segment.status === "failed")) setMessage("Nekateri segmenti niso bili prevedeni. Poskusite jih lahko znova.");
      else setMessage("Lokalni prevod je pripravljen za ročni pregled.");
      setStatus(await getLocalTranslationStatus());
    } catch (error) {
      setJob(original);
      setMessage(error instanceof Error ? error.message : "Lokalno prevajanje ni uspelo.");
    } finally {
      controllerRef.current = null;
      setRunning(false);
    }
  }

  function cancel() {
    controllerRef.current?.abort();
  }

  return { status, statusError, running, progress, message, translate, cancel };
}
