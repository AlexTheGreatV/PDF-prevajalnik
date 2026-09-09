"use client";

import { useMemo, useRef, useState } from "react";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfAnalysis } from "../lib/pdf-types";
import {
  createTranslationJob,
  editTranslation,
  exportTranslationJob,
  importTranslationJob,
  resetTranslation,
  retryFailedTranslation,
  reviewTranslation,
  updateJobGlossary,
  validateTranslationJobCompatibility,
} from "../lib/translation-job";
import type { GlossaryEntry, TranslationJob, TranslationSegment, TranslationStatus } from "../lib/translation-types";
import { useLocalTranslation } from "./use-local-translation";

type TranslationFilter = "all" | Exclude<TranslationStatus, "translating">;
const FILTERS: TranslationFilter[] = ["all", "pending", "translated", "reviewed", "failed", "skipped"];
const FILTER_LABELS: Record<(typeof FILTERS)[number], string> = {
  all: "Vsi", pending: "Čakajo", translated: "Prevedeni", reviewed: "Potrjeni", failed: "Napake", skipped: "Preskočeni",
};

export function TranslationWorkspace({ analysis, ocrItems }: { analysis: PdfAnalysis; ocrItems: OcrReviewItem[] }) {
  const importRef = useRef<HTMLInputElement | null>(null);
  const [sourceLanguage, setSourceLanguage] = useState("auto");
  const [targetLanguage, setTargetLanguage] = useState("sl");
  const [glossary, setGlossary] = useState<GlossaryEntry[]>([]);
  const [job, setJob] = useState<TranslationJob | null>(null);
  const [filter, setFilter] = useState<TranslationFilter>("all");
  const [message, setMessage] = useState("Pripravite segmente za lokalni prevod ali ročni pregled.");
  const local = useLocalTranslation(job, setJob);

  const visibleSegments = useMemo(
    () => job?.segments.filter((segment) => filter === "all" || segment.status === filter) ?? [],
    [job, filter],
  );
  const completed = job?.segments.filter((segment) => segment.status === "reviewed").length ?? 0;
  const actionable = job?.segments.filter((segment) => segment.status !== "skipped").length ?? 0;

  function prepare() {
    setJob(createTranslationJob(analysis, ocrItems, { source_language: sourceLanguage, target_language: targetLanguage, glossary }));
    setMessage("Segmenti so pripravljeni. Prevod lahko vnesete ročno.");
  }

  function updateSegment(segmentId: string, update: (segment: TranslationSegment) => TranslationSegment) {
    setJob((current) => current ? { ...current, segments: current.segments.map((segment) => segment.segment_id === segmentId ? update(segment) : segment) } : current);
  }

  function changeGlossary(next: GlossaryEntry[]) {
    setGlossary(next);
    setJob((current) => current ? updateJobGlossary(current, next) : current);
  }

  function addGlossaryEntry() {
    changeGlossary([...glossary, { id: `glossary-${Date.now()}`, source_term: "", target_term: "", case_sensitive: false, exact_match: true, do_not_translate: false }]);
  }

  function exportJob() {
    if (!job) return;
    const url = URL.createObjectURL(new Blob([exportTranslationJob(job)], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${analysis.filename.replace(/\.pdf$/i, "")}-translation.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function importJob(file: File | null) {
    if (!file) return;
    try {
      const imported = importTranslationJob(await file.text());
      const expected = createTranslationJob(analysis, ocrItems, { source_language: imported.source_language, target_language: imported.target_language, glossary: imported.glossary, created_at: imported.created_at });
      validateTranslationJobCompatibility(imported, expected);
      setJob(imported);
      setGlossary(imported.glossary);
      setSourceLanguage(imported.source_language);
      setTargetLanguage(imported.target_language);
      setMessage("Prevajalsko opravilo je bilo uspešno uvoženo.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Uvoz prevajalskega opravila ni uspel.");
    } finally {
      if (importRef.current) importRef.current.value = "";
    }
  }

  return (
    <section className="translation-workspace">
      <div className="translation-header">
        <div>
          <span>Mejnik 6B</span>
          <h2>Pregled prevodov</h2>
          <p>{message}</p>
        </div>
        <div className="translation-languages">
          <label>Izvorni jezik
            <select value={sourceLanguage} onChange={(event) => setSourceLanguage(event.target.value)} disabled={Boolean(job)}>
              <option value="auto">Samodejno</option><option value="en">English</option><option value="de">Deutsch</option><option value="sl">Slovenščina</option>
            </select>
          </label>
          <label>Ciljni jezik
            <select value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value)} disabled={Boolean(job)}>
              <option value="sl">Slovenščina</option><option value="en">English</option><option value="de">Deutsch</option>
            </select>
          </label>
        </div>
      </div>

      <GlossaryEditor entries={glossary} onChange={changeGlossary} onAdd={addGlossaryEntry} />

      <div className={`translation-provider-status ${local.status?.available ? "available" : "unavailable"}`}>
        <strong>{local.status?.available ? "Lokalni prevajalnik je pripravljen" : "Lokalni prevajalnik ni nameščen"}</strong>
        <span>{local.statusError ?? local.status?.message ?? (local.status ? `${local.status.model_id} · ${local.status.device}/${local.status.compute_type}` : "Preverjam stanje lokalnega prevajalnika …")}</span>
      </div>

      <div className="translation-actions">
        <button className="button" onClick={prepare}>{job ? "Znova pripravi prevod" : "Pripravi prevod"}</button>
        <button className="button secondary" disabled={!job || !local.status?.available || local.running} onClick={() => void local.translate()}>Prevedi čakajoče</button>
        <button className="button secondary" disabled={!local.running} onClick={local.cancel}>Prekliči</button>
        <button className="button secondary" disabled={!job} onClick={exportJob}>Izvozi JSON</button>
        <button className="button secondary" onClick={() => importRef.current?.click()}>Uvozi JSON</button>
        <input ref={importRef} className="visually-hidden" type="file" accept="application/json,.json" onChange={(event) => void importJob(event.target.files?.[0] ?? null)} />
      </div>

      {job && (
        <>
          {local.message && <p className="translation-provider-message">{local.message}</p>}
          {local.running && <div className="translation-progress"><span>{local.progress.completed} od {local.progress.total} segmentov</span><progress value={local.progress.completed} max={local.progress.total || 1} /></div>}
          <div className="translation-progress">
            <span>{completed} od {actionable} prevodov potrjenih</span>
            <progress value={completed} max={actionable || 1} />
          </div>
          <div className="translation-filters" aria-label="Filtri prevodov">
            {FILTERS.map((value) => <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{FILTER_LABELS[value]}</button>)}
          </div>
          <div className="translation-segments">
            {visibleSegments.map((segment) => (
              <TranslationSegmentRow key={segment.segment_id} segment={segment}
                onEdit={(text) => updateSegment(segment.segment_id, (current) => editTranslation(current, text))}
                onReview={() => updateSegment(segment.segment_id, reviewTranslation)}
                onReset={() => updateSegment(segment.segment_id, resetTranslation)}
                onRetry={() => updateSegment(segment.segment_id, retryFailedTranslation)} />
            ))}
            {visibleSegments.length === 0 && <p>V tem filtru ni segmentov.</p>}
          </div>
        </>
      )}
    </section>
  );
}

function TranslationSegmentRow({ segment, onEdit, onReview, onReset, onRetry }: {
  segment: TranslationSegment; onEdit: (text: string) => void; onReview: () => void; onReset: () => void; onRetry: () => void;
}) {
  return (
    <article className={`translation-segment translation-${segment.status}`}>
      <header>
        <strong>Stran {segment.page_number} · {segment.block_type}</strong>
        <span>{segment.status} · vir: {segment.source_text_origin === "accepted_ocr" ? "potrjeni OCR" : "native"}</span>
      </header>
      <div className="translation-columns">
        <div><h3>Izvorno</h3><pre>{segment.source_text || "Ni izvornega besedila."}</pre></div>
        {segment.skip_reason
          ? <div><h3>Prevod</h3><p className="translation-skip">Preskočeno: {segment.skip_reason}</p></div>
          : <label><span>Prevod</span><textarea value={segment.edited_translation} onChange={(event) => onEdit(event.target.value)} placeholder="Vnesite slovenski prevod" /></label>}
      </div>
      {segment.glossary_matches.length > 0 && <div className="glossary-matches">Slovar: {segment.glossary_matches.map((match) => `${match.matched_text} → ${match.do_not_translate ? "ne prevajaj" : match.target_term}`).join(", ")}</div>}
      {segment.error && <p className="translation-error">{segment.error}</p>}
      {!segment.skip_reason && <div className="translation-row-actions">
        <button className="button secondary" disabled={!segment.edited_translation.trim()} onClick={onReview}>Potrdi prevod</button>
        <button className="button secondary" onClick={onReset}>Ponastavi</button>
        {segment.status === "failed" && <button className="button secondary" onClick={onRetry}>Poskusi znova</button>}
      </div>}
    </article>
  );
}

function GlossaryEditor({ entries, onChange, onAdd }: { entries: GlossaryEntry[]; onChange: (entries: GlossaryEntry[]) => void; onAdd: () => void }) {
  function update(id: string, value: Partial<GlossaryEntry>) { onChange(entries.map((entry) => entry.id === id ? { ...entry, ...value } : entry)); }
  return <div className="glossary-editor">
    <div className="translation-subhead"><div><h3>Slovar izrazov</h3><p>Slovar je shranjen samo v izvoženem opravilu.</p></div><button className="button secondary" onClick={onAdd}>Dodaj izraz</button></div>
    {entries.map((entry) => <div className="glossary-row" key={entry.id}>
      <input aria-label="Izvorni izraz" placeholder="Izvorni izraz" value={entry.source_term} onChange={(event) => update(entry.id, { source_term: event.target.value })} />
      <input aria-label="Ciljni izraz" placeholder="Ciljni izraz" value={entry.target_term} disabled={entry.do_not_translate} onChange={(event) => update(entry.id, { target_term: event.target.value })} />
      <label><input type="checkbox" checked={entry.case_sensitive} onChange={(event) => update(entry.id, { case_sensitive: event.target.checked })} /> Velike/male črke</label>
      <label><input type="checkbox" checked={entry.exact_match} onChange={(event) => update(entry.id, { exact_match: event.target.checked })} /> Točen izraz</label>
      <label><input type="checkbox" checked={entry.do_not_translate} onChange={(event) => update(entry.id, { do_not_translate: event.target.checked })} /> Ne prevajaj</label>
      <button className="button secondary" onClick={() => onChange(entries.filter((item) => item.id !== entry.id))}>Odstrani</button>
    </div>)}
  </div>;
}
