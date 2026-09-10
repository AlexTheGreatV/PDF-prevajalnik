"use client";

import { useMemo, useRef, useState } from "react";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfAnalysis } from "../lib/pdf-types";
import { createDefaultGlossary } from "../lib/default-glossary";
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
  const [glossary, setGlossary] = useState<GlossaryEntry[]>(createDefaultGlossary);
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
    changeGlossary([...glossary, {
      id: `glossary-${Date.now()}`,
      source_term: "",
      target_term: "",
      case_sensitive: true,
      mode: "exact_segment",
      enabled: true,
      origin: "user",
    }]);
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
          <span>Mejnik 6D</span>
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
        {local.status && <span>Aktivni profil: {local.status.profile_id} · {local.status.model_id} · {local.status.device}/{local.status.compute_type}</span>}
        <span>{local.statusError ?? local.status?.message ?? (!local.status ? "Preverjam stanje lokalnega prevajalnika …" : "Model je naložen samo lokalno.")}</span>
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
        <span>{segment.status} · besedilo: {segment.source_text_origin === "accepted_ocr" ? "potrjeni OCR" : "native"} · prevod: {segment.translation_origin ?? "še ni"}</span>
      </header>
      <div className="translation-columns">
        <div><h3>Izvorno</h3><pre>{segment.source_text || "Ni izvornega besedila."}</pre></div>
        {segment.skip_reason
          ? <div><h3>Prevod</h3><p className="translation-skip">Preskočeno: {segment.skip_reason}</p></div>
          : <label><span>Prevod</span><textarea value={segment.edited_translation} onChange={(event) => onEdit(event.target.value)} placeholder="Vnesite slovenski prevod" /></label>}
      </div>
      {segment.glossary_matches.length > 0 && <div className="glossary-matches">Slovar: {segment.glossary_matches.map((match) => `${match.matched_text} → ${match.mode === "do_not_translate" ? "ne prevajaj" : match.target_term} (${match.mode})`).join(", ")}</div>}
      {segment.glossary_entry_id && <div className="translation-origin">Uporabljen slovarski vnos: {segment.glossary_entry_id}</div>}
      {segment.model_id && <div className="translation-origin">Model: {segment.model_id}</div>}
      {segment.needs_review && <p className="translation-warning">Potreben je ročni pregled.</p>}
      {segment.warnings.length > 0 && <div className="translation-origin">Opozorila: {segment.warnings.join(", ")}</div>}
      {segment.chunks.length > 1 && <details className="translation-chunks">
        <summary>Modelni deli: {segment.chunks.filter((chunk) => chunk.status === "translated").length}/{segment.chunks.length}</summary>
        {segment.chunks.map((chunk) => <div key={chunk.chunk_id} className={`translation-chunk ${chunk.status}`}>
          <strong>{chunk.chunk_id} · {chunk.status}</strong>
          <p>{chunk.source_text}</p>
          {chunk.translated_text && <p>{chunk.translated_text}</p>}
          {chunk.decoding && <small>{chunk.source_token_count} vhodnih tokenov · {chunk.decoding.output_token_count}/{chunk.decoding.max_decoding_length} izhodnih</small>}
          {chunk.warnings.length > 0 && <small>Opozorila: {chunk.warnings.join(", ")}</small>}
        </div>)}
      </details>}
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
  function restore(entry: GlossaryEntry) {
    if (entry.origin === "built_in" && entry.default_target_term) update(entry.id, { target_term: entry.default_target_term, enabled: true });
  }
  return <div className="glossary-editor">
    <div className="translation-subhead"><div><h3>Slovar izrazov</h3><p>Privzeti in uporabniški vnosi se shranijo v izvoženo opravilo.</p></div><button className="button secondary" onClick={onAdd}>Dodaj izraz</button></div>
    {entries.map((entry) => <div className="glossary-row" key={entry.id}>
      <span className={`glossary-origin ${entry.origin}`}>{entry.origin === "built_in" ? "Privzeto" : "Uporabniško"}</span>
      <label><input type="checkbox" checked={entry.enabled} onChange={(event) => update(entry.id, { enabled: event.target.checked })} /> Vključeno</label>
      <input aria-label="Izvorni izraz" placeholder="Izvorni izraz" value={entry.source_term} disabled={entry.origin === "built_in"} onChange={(event) => update(entry.id, { source_term: event.target.value })} />
      <input aria-label="Ciljni izraz" placeholder="Ciljni izraz" value={entry.target_term} disabled={entry.mode === "do_not_translate"} onChange={(event) => update(entry.id, { target_term: event.target.value })} />
      <select aria-label="Način slovarja" value={entry.mode} onChange={(event) => update(entry.id, { mode: event.target.value as GlossaryEntry["mode"] })}>
        <option value="exact_segment">Celoten segment</option>
        <option value="do_not_translate">Ne prevajaj</option>
        <option value="preferred">Priporočilo</option>
        <option value="locked_inline">Zaklenjeno v povedi</option>
      </select>
      <label><input type="checkbox" checked={entry.case_sensitive} onChange={(event) => update(entry.id, { case_sensitive: event.target.checked })} /> Razlikuj črke</label>
      {entry.origin === "built_in"
        ? <button className="button secondary" onClick={() => restore(entry)}>Obnovi</button>
        : <button className="button secondary" onClick={() => onChange(entries.filter((item) => item.id !== entry.id))}>Odstrani</button>}
    </div>)}
  </div>;
}
