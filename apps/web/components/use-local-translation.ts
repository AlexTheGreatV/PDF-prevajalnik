"use client";

import { useEffect, useRef, useState } from "react";
import { LocalTranslationProvider, getLocalTranslationStatus, type LocalTranslationStatus } from "../lib/local-translation-provider";
import { mergeCurrentTranslationResult, translateJobBatch } from "../lib/translation-job";
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
  const generationRef = useRef(0);
  const runningRef = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    getLocalTranslationStatus(controller.signal)
      .then((next) => { setStatus(next); setStatusError(null); })
      .catch((error) => {
        if (!controller.signal.aborted) setStatusError(error instanceof Error ? error.message : "Status lokalnega prevajalnika ni dosegljiv.");
      });
    return () => controller.abort();
  }, []);

  useEffect(() => () => {
    generationRef.current += 1;
    runningRef.current = false;
    controllerRef.current?.abort();
    controllerRef.current = null;
  }, []);

  async function translate(jobOverride?: TranslationJob) {
    const jobToTranslate = jobOverride ?? job;
    if (!jobToTranslate || !status?.available || runningRef.current) return;
    runningRef.current = true;
    const generation = ++generationRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    const original = jobToTranslate;
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
      if (generation !== generationRef.current) return;
      setJob((current) => current ? mergeCurrentTranslationResult(current, original, translated) : current);
      if (controller.signal.aborted) setMessage("Prevajanje je bilo preklicano.");
      else if (translated.segments.some((segment) => segment.status === "failed")) setMessage("Nekateri segmenti niso bili prevedeni. Poskusite jih lahko znova.");
      else setMessage("Lokalni prevod je pripravljen za ročni pregled.");
      const nextStatus = await getLocalTranslationStatus();
      if (generation === generationRef.current) setStatus(nextStatus);
    } catch (error) {
      if (generation !== generationRef.current) return;
      setJob((current) => current ? mergeCurrentTranslationResult(current, original, original) : current);
      setMessage(error instanceof Error ? error.message : "Lokalno prevajanje ni uspelo.");
    } finally {
      if (generation === generationRef.current) {
        controllerRef.current = null;
        runningRef.current = false;
        setRunning(false);
      }
    }
  }

  function cancel() {
    const wasRunning = runningRef.current;
    generationRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    runningRef.current = false;
    setRunning(false);
    if (wasRunning) {
      setJob((current) => current ? {
        ...current,
        segments: current.segments.map((segment) => segment.status === "translating"
          ? { ...segment, status: "pending" }
          : segment),
      } : current);
      setMessage("Prevajanje je bilo preklicano.");
    }
  }

  return { status, statusError, running, progress, message, translate, cancel };
}
