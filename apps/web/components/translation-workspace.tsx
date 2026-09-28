"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfAnalysis } from "../lib/pdf-types";
import { createDefaultGlossary } from "../lib/default-glossary";
import { PdfExportPanel, type PdfExportUiState } from "./pdf-export-panel";
import {
  bindTranslationJobToAnalysis,
  createTranslationJob,
  editTranslation,
  exportTranslationJob,
  importTranslationJob,
  invalidateStaleOcrApprovals,
  resetTranslation,
  retryFailedTranslation,
  reviewTranslation,
  updateTranslationJobSegment,
  validateTranslationJobCompatibility,
} from "../lib/translation-job";
import type { GlossaryEntry, TranslationJob, TranslationSegment, TranslationStatus } from "../lib/translation-types";
import { useLocalTranslation } from "./use-local-translation";
import { TranslationProfileSummary } from "./translation-profile-summary";
import { updateTerminologySources } from "../lib/translation-profile-evaluation";
import { GlossaryManager, type GlossaryProposal } from "./glossary-manager";
import {
  createReviewedCorrectionDraft,
  isGlossarySnapshotStale,
  recordGlossaryUsage,
  resolveGlossarySnapshot,
  type PersistentGlossaryLoadState,
} from "../lib/persistent-glossary";
import {
  canInitializeTranslationJob,
  createTranslationMemoryDraft,
  isTranslationMemorySnapshotStale,
  recordTranslationMemoryUsage,
  type TranslationMemoryLoadState,
} from "../lib/translation-memory";
import { TranslationMemoryManager, type TranslationMemoryProposal } from "./translation-memory-manager";
import { isTranslationUnitSegment } from "../lib/translation-units";
import { determineGuidedStep, type WorkspaceMode } from "../lib/guided-workflow";
import { GuidedTranslationReview } from "./guided-translation-review";

type TranslationFilter = "all" | Exclude<TranslationStatus, "translating">;
const FILTERS: TranslationFilter[] = ["all", "pending", "translated", "reviewed", "failed", "skipped"];
const FILTER_LABELS: Record<(typeof FILTERS)[number], string> = {
  all: "Vsi", pending: "Čakajo", translated: "Prevedeni", reviewed: "Potrjeni", failed: "Napake", skipped: "Preskočeni",
};

export function TranslationWorkspace({ analysis, ocrItems, sourceFile, mode, missingTextPages }: {
  analysis: PdfAnalysis;
  ocrItems: OcrReviewItem[];
  sourceFile: File;
  mode: WorkspaceMode;
  missingTextPages: number;
}) {
  const importRef = useRef<HTMLInputElement | null>(null);
  const recordedGlossaryUsage = useRef(new Map<string, Set<string>>());
  const recordedTranslationMemoryUsage = useRef(new Map<string, Set<string>>());
  const [sourceLanguage, setSourceLanguage] = useState("auto");
  const [targetLanguage, setTargetLanguage] = useState("sl");
  const [translationDomain, setTranslationDomain] = useState("");
  const [documentGlossary, setDocumentGlossary] = useState<GlossaryEntry[]>([]);
  const [persistentGlossary, setPersistentGlossary] = useState<PersistentGlossaryLoadState>({ status: "loading" });
  const [glossaryReloadRequest, setGlossaryReloadRequest] = useState(0);
  const [glossaryProposal, setGlossaryProposal] = useState<GlossaryProposal | null>(null);
  const [translationMemory, setTranslationMemory] = useState<TranslationMemoryLoadState>({ status: "loading" });
  const [translationMemoryReloadRequest, setTranslationMemoryReloadRequest] = useState(0);
  const [translationMemoryProposal, setTranslationMemoryProposal] = useState<TranslationMemoryProposal | null>(null);
  const [job, setJob] = useState<TranslationJob | null>(null);
  const [filter, setFilter] = useState<TranslationFilter>("all");
  const [guidedReviewIndex, setGuidedReviewIndex] = useState(0);
  const [exportState, setExportState] = useState<PdfExportUiState>({ plan: null, downloadCompleted: false });
  const [message, setMessage] = useState("Pripravite segmente za lokalni prevod ali ročni pregled.");
  const local = useLocalTranslation(job, setJob);
  const glossary = useMemo(
    () => resolveGlossarySnapshot(documentGlossary, persistentGlossary, createDefaultGlossary()),
    [documentGlossary, persistentGlossary],
  );
  const persistentGlossaryHash = persistentGlossary.status === "ready" ? persistentGlossary.active_set_hash : "";
  const glossaryReady = glossary !== null;
  const translationMemoryReady = canInitializeTranslationJob(true, persistentGlossary.status, translationMemory.status);
  const translationMemoryHash = translationMemory.status === "ready" ? translationMemory.active_set_hash : "";

  useEffect(() => {
    // OCR acceptance is external review state; synchronize it before any export can reuse an old approval.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setJob((current) => current ? invalidateStaleOcrApprovals(current, ocrItems) : current);
  }, [ocrItems]);

  useEffect(() => {
    if (!job) return;
    const persistentEntryIds = new Set(job.glossary.filter((entry) => entry.scope === "persistent_user").map((entry) => entry.id));
    const used = job.glossary_usage
      .filter((item) => persistentEntryIds.has(item.glossary_id) && item.outcome === "applied")
      .map((item) => item.glossary_id);
    const jobKey = `${job.created_at}:${job.document_identity?.sha256 ?? job.source_filename}`;
    const recorded = recordedGlossaryUsage.current.get(jobKey) ?? new Set<string>();
    const pending = used.filter((entryId) => !recorded.has(entryId));
    if (pending.length === 0) return;
    pending.forEach((entryId) => recorded.add(entryId));
    recordedGlossaryUsage.current.set(jobKey, recorded);
    void recordGlossaryUsage(pending).catch(() => {
      pending.forEach((entryId) => recorded.delete(entryId));
    });
  }, [job]);

  useEffect(() => {
    if (!job) return;
    const jobKey = `${job.created_at}:${job.document_identity?.sha256 ?? job.source_filename}`;
    const recorded = recordedTranslationMemoryUsage.current.get(jobKey) ?? new Set<string>();
    const pending = job.translation_memory_usage.map((usage) => usage.entry_id).filter((entryId) => !recorded.has(entryId));
    if (!pending.length) return;
    pending.forEach((entryId) => recorded.add(entryId));
    recordedTranslationMemoryUsage.current.set(jobKey, recorded);
    void recordTranslationMemoryUsage(pending).catch(() => pending.forEach((entryId) => recorded.delete(entryId)));
  }, [job]);

  const visibleSegments = useMemo(
    () => job?.segments.filter((segment) =>
      isTranslationUnitSegment(segment) && (filter === "all" || segment.status === filter),
    ) ?? [],
    [job, filter],
  );
  const completed = job?.segments.filter((segment) => isTranslationUnitSegment(segment) && segment.status === "reviewed").length ?? 0;
  const actionable = job?.segments.filter((segment) => isTranslationUnitSegment(segment) && segment.status !== "skipped").length ?? 0;
  const reviewableSegments = job?.segments.filter((segment) =>
    isTranslationUnitSegment(segment) && segment.status === "translated" && Boolean(segment.edited_translation.trim()),
  ) ?? [];
  const guidedSegment = reviewableSegments.length ? reviewableSegments[guidedReviewIndex % reviewableSegments.length] : null;
  const guidedState = determineGuidedStep({
    hasFile: true,
    analysisReady: true,
    missingTextPages,
    ocrItems,
    glossaryStatus: persistentGlossary.status,
    translationMemoryStatus: translationMemory.status,
    translatorAvailable: Boolean(local.status?.available),
    translationRunning: local.running,
    job,
    exportPlan: exportState.plan,
    exportDownloaded: exportState.downloadCompleted,
  });

  function createPreparedJob(): TranslationJob | null {
    if (!glossaryReady || !translationMemoryReady) return null;
    return createTranslationJob(analysis, ocrItems, {
      source_language: sourceLanguage,
      target_language: targetLanguage,
      glossary: glossary!,
      glossary_set_hash: persistentGlossaryHash || null,
      translation_memory: translationMemory.status === "ready" ? translationMemory.entries : [],
      translation_memory_set_hash: translationMemory.status === "ready" ? translationMemory.active_set_hash : null,
      domain: translationDomain,
    });
  }

  function prepare() {
    const prepared = createPreparedJob();
    if (!prepared) return;
    local.cancel();
    setExportState({ plan: null, downloadCompleted: false });
    setJob(prepared);
    setMessage(persistentGlossary.status === "explicitly_skipped"
      ? "Segmenti so pripravljeni brez trajnega glosarja. Prevod zahteva posebej pozoren pregled."
      : "Segmenti so pripravljeni. Prevod lahko vnesete ročno.");
  }

  async function prepareAndTranslate() {
    const prepared = createPreparedJob();
    if (!prepared) return;
    local.cancel();
    setExportState({ plan: null, downloadCompleted: false });
    setJob(prepared);
    setMessage("Segmenti so pripravljeni iz preverjenih lokalnih virov.");
    if (local.status?.available) await local.translate(prepared);
  }

  function updateSegment(segmentId: string, update: (segment: TranslationSegment) => TranslationSegment) {
    setJob((current) => current ? updateTranslationJobSegment(current, segmentId, update) : current);
  }

  function changeDocumentGlossary(next: GlossaryEntry[]) {
    setDocumentGlossary(next.map((entry) => ({ ...entry, scope: "document" as const })));
    if (job) setMessage("Navodila dokumenta so spremenjena. Obstoječi prevodi ostajajo nespremenjeni; za nov posnetek izrecno znova pripravite prevod.");
  }

  function addGlossaryEntry() {
    changeDocumentGlossary([...documentGlossary, {
      id: `glossary-${Date.now()}`,
      source_term: "",
      target_term: "",
      case_sensitive: true,
      mode: "exact_segment",
      enabled: true,
      origin: "user",
      scope: "document",
      status: "approved",
      source: "manual",
    }]);
  }

  function updatePersistentGlossary(state: PersistentGlossaryLoadState) {
    setPersistentGlossary(state);
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
      const expected = createTranslationJob(analysis, ocrItems, {
        source_language: imported.source_language,
        target_language: imported.target_language,
        glossary: imported.glossary,
        translation_memory: imported.translation_memory_snapshot,
        translation_memory_set_hash: imported.translation_memory_set_hash,
        domain: imported.domain,
        created_at: imported.created_at,
      });
      validateTranslationJobCompatibility(imported, expected);
      const compatible = bindTranslationJobToAnalysis(imported, expected);
      setJob(compatible);
      setDocumentGlossary(compatible.glossary.filter((entry) => entry.scope === "document"));
      setSourceLanguage(imported.source_language);
      setTargetLanguage(imported.target_language);
      setTranslationDomain(imported.domain);
      setMessage("Prevajalsko opravilo je bilo uspešno uvoženo.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Uvoz prevajalskega opravila ni uspel.");
    } finally {
      if (importRef.current) importRef.current.value = "";
    }
  }

  const updateTranslationMemory = useCallback((state: TranslationMemoryLoadState) => setTranslationMemory(state), []);

  return (
    <section className={`translation-workspace ${mode === "simple" ? "simple-translation-workspace" : "advanced-translation-workspace"}`}>
      <div className={`translation-header${mode === "simple" ? " advanced-only" : ""}`}>
        <div>
          <span>Prevajalski pregled</span>
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
          <label>Področje (neobvezno)
            <input value={translationDomain} onChange={(event) => setTranslationDomain(event.target.value)} disabled={Boolean(job)} placeholder="npr. construction_adhesives" />
          </label>
        </div>
      </div>

      <div className="advanced-only">
        <GlossaryManager proposal={glossaryProposal} reloadRequest={glossaryReloadRequest} onProposalConsumed={() => setGlossaryProposal(null)} onApprovedEntriesChange={updatePersistentGlossary} />
      </div>

      <div className="advanced-only">
        <TranslationMemoryManager proposal={translationMemoryProposal} reloadRequest={translationMemoryReloadRequest} onProposalConsumed={() => setTranslationMemoryProposal(null)} onApprovedEntriesChange={updateTranslationMemory} />
      </div>

      <div className="advanced-only"><GlossaryEditor entries={documentGlossary} onChange={changeDocumentGlossary} onAdd={addGlossaryEntry} /></div>

      <div className={`translation-provider-status ${local.status?.available ? "available" : "unavailable"}`}>
        <strong>{local.status?.available ? "Lokalni prevajalnik je pripravljen" : "Lokalni prevajalnik ni nameščen"}</strong>
        {local.status && <span>Aktivni profil: {local.status.profile_id} · {local.status.model_id} · {local.status.device}/{local.status.compute_type}</span>}
        <span>{local.statusError ?? local.status?.message ?? (!local.status ? "Preverjam stanje lokalnega prevajalnika …" : "Model je naložen samo lokalno.")}</span>
      </div>

      <div className="translation-actions advanced-only">
        <button className="button" disabled={!glossaryReady || !translationMemoryReady} onClick={prepare}>{job ? "Znova pripravi prevod" : "Pripravi prevod"}</button>
        <button className="button secondary" disabled={!job || !local.status?.available || local.running} onClick={() => void local.translate()}>Prevedi čakajoče</button>
        <button className="button secondary" disabled={!local.running} onClick={local.cancel}>Prekliči</button>
        <button className="button secondary" disabled={!job} onClick={exportJob}>Izvozi JSON</button>
        <button className="button secondary" onClick={() => importRef.current?.click()}>Uvozi JSON</button>
        <input ref={importRef} className="visually-hidden" type="file" accept="application/json,.json" onChange={(event) => void importJob(event.target.files?.[0] ?? null)} />
      </div>
      {persistentGlossary.status === "loading" && <p className="translation-provider-message">Nalaganje trajnega glosarja …</p>}
      {persistentGlossary.status === "error" && <div className="translation-error">
        <p>{persistentGlossary.message} Prevajalsko opravilo ni bilo pripravljeno brez trajnega glosarja.</p>
        <button className="button secondary" onClick={() => setGlossaryReloadRequest((value) => value + 1)}>Poskusi znova</button>
        <button className="button secondary" onClick={() => setPersistentGlossary({ status: "explicitly_skipped" })}>Nadaljuj brez trajnega glosarja</button>
      </div>}
      {persistentGlossary.status === "explicitly_skipped" && <p className="translation-warning">Nadaljujete brez trajnega glosarja. Uporabljena bodo samo dokumentna navodila in vgrajeni izrazi.</p>}
      {translationMemory.status === "loading" && <p className="translation-provider-message">Nalaganje prevajalskega spomina …</p>}
      {translationMemory.status === "error" && <div className="translation-error">
        <p>{translationMemory.message} Prevajalsko opravilo še ni pripravljeno s preverjenim posnetkom TM.</p>
        <button className="button secondary" onClick={() => setTranslationMemoryReloadRequest((value) => value + 1)}>Poskusi znova</button>
        <button className="button secondary" onClick={() => setTranslationMemory({ status: "explicitly_skipped" })}>Nadaljuj brez prevajalskega spomina</button>
      </div>}
      {translationMemory.status === "explicitly_skipped" && <p className="translation-warning">Nadaljujete brez lokalnega prevajalskega spomina.</p>}

      {mode === "simple" && !["review_translations", "prepare_pdf", "download_pdf", "complete"].includes(guidedState.step) && (
        <section className="guided-next-step" aria-live="polite">
          <span>Naslednji korak</span>
          <h2>{guidedState.label}</h2>
          <p>{guidedState.explanation}</p>
          {guidedState.step === "prepare_translation" && <button className="button" disabled={guidedState.blocked || !local.status?.available} onClick={() => void prepareAndTranslate()}>Pripravi in prevedi</button>}
          {guidedState.step === "translate" && <button className="button" disabled={guidedState.blocked} onClick={() => void local.translate()}>{guidedState.label}</button>}
          {guidedState.step === "review_text_issues" && <a className="button" href="#ocr-review">Preglej OCR območja</a>}
        </section>
      )}

      {job && (
        <>
          {local.message && <p className="translation-provider-message">{local.message}</p>}
          {local.running && <div className="translation-progress"><span>{local.progress.completed} od {local.progress.total} segmentov</span><progress value={local.progress.completed} max={local.progress.total || 1} /></div>}
          <div className="translation-progress">
            <span>{completed} od {actionable} prevodov potrjenih</span>
            <progress value={completed} max={actionable || 1} />
          </div>
          <div className="advanced-only"><TranslationProfileSummary
              job={job}
              onTerminologySourcesChange={(sources) => setJob((current) => current ? updateTerminologySources(current, sources) : current)}
            /></div>
          {isGlossarySnapshotStale(job, persistentGlossaryHash) && <p className="translation-warning">Glosar je bil po prevodu spremenjen. Obstoječi prevodi ostajajo nespremenjeni; za uporabo novega nabora izrecno znova pripravite prevod.</p>}
          {isTranslationMemorySnapshotStale(job, translationMemoryHash) && <p className="translation-warning">Prevajalski spomin je bil po pripravi opravila spremenjen. Odprti prevodi ostajajo nespremenjeni; nov posnetek se uporabi šele ob izrecni ponovni pripravi.</p>}
          {mode === "simple" && guidedSegment && <GuidedTranslationReview
            segment={guidedSegment}
            position={guidedReviewIndex % reviewableSegments.length + 1}
            total={reviewableSegments.length}
            onEdit={(text) => updateSegment(guidedSegment.segment_id, (current) => editTranslation(current, text))}
            onReview={() => updateSegment(guidedSegment.segment_id, reviewTranslation)}
            onNext={() => setGuidedReviewIndex((value) => value + 1)}
          />}
          {(mode === "advanced" || (["prepare_pdf", "download_pdf", "complete"].includes(guidedState.step) && !guidedSegment)) && <PdfExportPanel
            sourceFile={sourceFile}
            job={job}
            analysis={analysis}
            ocrItems={ocrItems}
            simpleMode={mode === "simple"}
            onStateChange={setExportState}
          />}
          <div className="translation-filters advanced-only" aria-label="Filtri prevodov">
            {FILTERS.map((value) => <button key={value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{FILTER_LABELS[value]}</button>)}
          </div>
          <div className="translation-segments advanced-only">
            {visibleSegments.map((segment) => (
              <TranslationSegmentRow key={segment.segment_id} segment={segment}
                onEdit={(text) => updateSegment(segment.segment_id, (current) => editTranslation(current, text))}
                onReview={() => updateSegment(segment.segment_id, reviewTranslation)}
                onReset={() => updateSegment(segment.segment_id, resetTranslation)}
                onRetry={() => updateSegment(segment.segment_id, retryFailedTranslation)}
                onPropose={() => setGlossaryProposal({ key: `${segment.segment_id}-${Date.now()}`, ...createReviewedCorrectionDraft(segment) })}
                onProposeMemory={() => job && setTranslationMemoryProposal({ key: `${segment.segment_id}-${Date.now()}`, ...createTranslationMemoryDraft(segment, job) })} />
            ))}
            {visibleSegments.length === 0 && <p>V tem filtru ni segmentov.</p>}
          </div>
        </>
      )}
    </section>
  );
}

function TranslationSegmentRow({ segment, onEdit, onReview, onReset, onRetry, onPropose, onProposeMemory }: {
  segment: TranslationSegment; onEdit: (text: string) => void; onReview: () => void; onReset: () => void; onRetry: () => void; onPropose: () => void; onProposeMemory: () => void;
}) {
  return (
    <article className={`translation-segment translation-${segment.status}`}>
      <header>
        <strong>Stran {segment.page_number} · {segment.block_type}</strong>
        <span>{segment.status} · besedilo: {segment.source_text_origin === "accepted_ocr" ? "potrjeni OCR" : "native"} · prevod: {segment.translation_origin === "mixed" ? "mešano (TM + lokalni model)" : segment.translation_origin ?? "še ni"}</span>
      </header>
      <div className="translation-columns">
        <div><h3>Izvorno</h3><pre>{segment.source_text || "Ni izvornega besedila."}</pre></div>
        {segment.skip_reason
          ? <div><h3>Prevod</h3><p className="translation-skip">Preskočeno: {segment.skip_reason}</p></div>
          : <label><span>Prevod</span><textarea value={segment.edited_translation} onChange={(event) => onEdit(event.target.value)} placeholder="Vnesite slovenski prevod" /></label>}
      </div>
      {segment.glossary_matches.length > 0 && <div className="glossary-matches">Slovar: {segment.glossary_matches.map((match) => `${match.matched_text} → ${match.mode === "do_not_translate" ? "ne prevajaj" : match.target_term} (${match.mode})`).join(", ")}</div>}
      {segment.glossary_entry_id && <div className="translation-origin">Uporabljen slovarski vnos: {segment.glossary_entry_id}</div>}
      {segment.translation_memory_entry_id && <div className="translation-origin">Prevajalski spomin: {segment.translation_memory_entry_id} · rev. {segment.translation_memory_revision} · exact</div>}
      {segment.model_id && <div className="translation-origin">Model: {segment.model_id}</div>}
      {segment.needs_review && <p className="translation-warning">Potreben je ročni pregled.</p>}
      {segment.warnings.length > 0 && <div className="translation-origin">Opozorila: {segment.warnings.join(", ")}</div>}
      {segment.chunks.length > 1 && <details className="translation-chunks">
        <summary>Prevajalske enote: {segment.chunks.filter((chunk) => chunk.status === "translated").length}/{segment.chunks.length}</summary>
        {segment.chunks.map((chunk) => <div key={chunk.chunk_id} className={`translation-chunk ${chunk.status}`}>
          <strong>{chunk.chunk_id} · {chunk.status} · {chunk.translation_origin === "translation_memory" ? "natančen zadetek TM" : chunk.translation_origin === "provider" ? "lokalni model" : "brez izvora"}</strong>
          <p>{chunk.source_text}</p>
          {chunk.translated_text && <p>{chunk.translated_text}</p>}
          {chunk.translation_memory_entry_id && <small>TM exact za celotno prevajalsko enoto: {chunk.translation_memory_entry_id} · rev. {chunk.translation_memory_revision} · {chunk.translation_memory_domain || "splošno"}</small>}
          {chunk.decoding && <small>{chunk.source_token_count} vhodnih tokenov · {chunk.decoding.output_token_count}/{chunk.decoding.max_decoding_length} izhodnih</small>}
          {chunk.warnings.length > 0 && <small>Opozorila: {chunk.warnings.join(", ")}</small>}
        </div>)}
      </details>}
      {segment.error && <p className="translation-error">{segment.error}</p>}
      {!segment.skip_reason && <div className="translation-row-actions">
        <button className="button secondary" disabled={!segment.edited_translation.trim()} onClick={onReview}>Potrdi prevod</button>
        <button className="button secondary" onClick={onReset}>Ponastavi</button>
        {segment.status === "reviewed" && segment.translation_origin === "manual" && <button className="button secondary" onClick={onPropose}>Predlagaj za glosar</button>}
        {segment.status === "reviewed" && segment.translation_origin === "manual" && <button className="button secondary" onClick={onProposeMemory}>Predlagaj za prevajalski spomin</button>}
        {segment.status === "failed" && <button className="button secondary" onClick={onRetry}>Poskusi znova</button>}
      </div>}
    </article>
  );
}

function GlossaryEditor({ entries, onChange, onAdd }: { entries: GlossaryEntry[]; onChange: (entries: GlossaryEntry[]) => void; onAdd: () => void }) {
  function update(id: string, value: Partial<GlossaryEntry>) { onChange(entries.map((entry) => entry.id === id ? { ...entry, ...value } : entry)); }
  return <div className="glossary-editor">
    <div className="translation-subhead"><div><h3>Navodila trenutnega dokumenta</h3><p>Ti vnosi imajo najvišjo prednost in se shranijo samo v izvoženo opravilo.</p></div><button className="button secondary" onClick={onAdd}>Dodaj navodilo</button></div>
    {entries.length === 0 && <p>Dokument nima posebnih terminoloških navodil.</p>}
    {entries.map((entry) => <div className="glossary-row" key={entry.id}>
      <span className="glossary-origin user">Dokument</span>
      <label><input type="checkbox" checked={entry.enabled} onChange={(event) => update(entry.id, { enabled: event.target.checked })} /> Vključeno</label>
      <input aria-label="Izvorni izraz" placeholder="Izvorni izraz" value={entry.source_term} onChange={(event) => update(entry.id, { source_term: event.target.value })} />
      <input aria-label="Ciljni izraz" placeholder="Ciljni izraz" value={entry.target_term} disabled={entry.mode === "do_not_translate"} onChange={(event) => update(entry.id, { target_term: event.target.value })} />
      <select aria-label="Način slovarja" value={entry.mode} onChange={(event) => update(entry.id, { mode: event.target.value as GlossaryEntry["mode"] })}>
        <option value="exact_segment">Celoten segment</option>
        <option value="do_not_translate">Ne prevajaj</option>
        <option value="preferred">Priporočilo</option>
        <option value="locked_inline">Zaklenjeno v povedi</option>
      </select>
      <label><input type="checkbox" checked={entry.case_sensitive} onChange={(event) => update(entry.id, { case_sensitive: event.target.checked })} /> Razlikuj črke</label>
      <button className="button secondary" onClick={() => onChange(entries.filter((item) => item.id !== entry.id))}>Odstrani</button>
    </div>)}
  </div>;
}
