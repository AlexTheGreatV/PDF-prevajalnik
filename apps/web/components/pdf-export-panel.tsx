"use client";

import { useState } from "react";

import { downloadPdfExport, preparePdfExport, type PdfExportMode, type PdfExportPlan } from "../lib/pdf-export-client";
import type { TranslationJob } from "../lib/translation-types";

const REASON_LABELS: Record<string, string> = {
  not_reviewed: "prevod ni potrjen",
  empty_translation: "prazen prevod",
  missing_bbox: "manjkajoče območje",
  invalid_page: "neveljavna stran",
  bbox_outside_page: "območje je zunaj strani",
  non_digital_source: "ni digitalno besedilo",
  ocr_only_source: "OCR območje",
  image_overlap: "območje se prekriva z vdelano sliko",
  complex_table: "kompleksna tabela",
  rotated_page: "rotirana stran",
  source_text_mismatch: "izvorno besedilo se ne ujema",
  overlap_conflict: "prekrivanje območij",
  missing_glyph: "manjkajoč znak v pisavi",
  overflow: "prevod se ne prilega",
};

export function PdfExportPanel({ sourceFile, job }: { sourceFile: File; job: TranslationJob }) {
  const [mode, setMode] = useState<PdfExportMode>("diagnostic");
  const [planned, setPlanned] = useState<{ job: TranslationJob; mode: PdfExportMode; plan: PdfExportPlan } | null>(null);
  const [busy, setBusy] = useState<"plan" | "download" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const plan = planned?.job === job && planned.mode === mode ? planned.plan : null;

  async function prepare() {
    setBusy("plan");
    setError(null);
    try {
      setPlanned({ job, mode, plan: await preparePdfExport(sourceFile, job, mode) });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Načrta izvoza ni bilo mogoče pripraviti.");
    } finally {
      setBusy(null);
    }
  }

  async function download() {
    setBusy("download");
    setError(null);
    try {
      const result = await downloadPdfExport(sourceFile, job, mode);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Izvoz PDF ni uspel.");
    } finally {
      setBusy(null);
    }
  }

  return <section className="pdf-export-panel">
    <div className="translation-subhead">
      <div><h3>Izvoz prevedenega PDF-ja</h3><p>Izvozijo se samo ročno potrjeni in geometrijsko preverjeni prevodi.</p></div>
      <div className="segmented" aria-label="Način izvoza PDF">
        <button className={mode === "diagnostic" ? "active" : ""} onClick={() => { setMode("diagnostic"); setError(null); }}>Diagnostika</button>
        <button className={mode === "replacement_preview" ? "active" : ""} onClick={() => { setMode("replacement_preview"); setError(null); }}>Nadomestni predogled</button>
      </div>
    </div>
    <div className="translation-actions">
      <button className="button" disabled={busy !== null || !job.document_identity} onClick={() => void prepare()}>{busy === "plan" ? "Preverjam izvoz …" : "Pripravi izvoz PDF"}</button>
      <button className="button secondary" disabled={!plan || plan.summary.eligible === 0 || busy !== null} onClick={() => void download()}>{busy === "download" ? "Pripravljam datoteko …" : "Prenesi novi PDF"}</button>
    </div>
    {!job.document_identity && <p className="translation-warning">Opravilo nima preverjene identitete PDF-ja. Znova ga povežite s trenutno analizo.</p>}
    {error && <p className="translation-error">{error}</p>}
    {plan && <>
      <div className="export-summary">
        <ExportMetric label="Skupaj" value={plan.summary.total_segments} />
        <ExportMetric label="Potrjeni" value={plan.summary.reviewed} />
        <ExportMetric label="Upravičeni" value={plan.summary.eligible} />
        <ExportMetric label="Preskočeni" value={plan.summary.skipped} />
        <ExportMetric label="Overflow" value={plan.summary.overflow} />
        <ExportMetric label="Konflikti" value={plan.summary.conflicts} />
        <ExportMetric label="Exact" value={plan.summary.exact} />
        <ExportMetric label="Razširjeni" value={plan.summary.expanded} />
        <ExportMetric label="Pomanjšani" value={plan.summary.scaled} />
        <ExportMetric label="Lokalni patchi" value={plan.summary.background_patches} />
        <ExportMetric label="Slikovni preskoki" value={plan.summary.image_overlap_skipped} />
      </div>
      <details className="export-typography">
        <summary>Tipografske podrobnosti</summary>
        {plan.replacements.map((item) => <div key={item.segment_id} className="export-typography-row">
          <strong>{item.segment_id}</strong>
          <span>{item.font_family} · {item.font_weight}{item.font_style === "italic" ? " italic" : ""}</span>
          <span>{formatSize(item.requested_font_size)} → {item.fitted_font_size === null ? "—" : formatSize(item.fitted_font_size)}</span>
          <span>{FIT_LABELS[item.fit_strategy] ?? item.fit_strategy}{item.scale !== null && item.scale < 1 ? ` · −${Math.round((1 - item.scale) * 100)} %` : ""}</span>
          {item.image_overlap_kind !== "none" && <span className="export-overlap-detail">
            {OVERLAP_LABELS[item.image_overlap_kind]} · Ocena varnosti ozadja: {Math.round(item.overlap_confidence * 100)} %
            {item.background_color && <> · <i className="color-swatch" style={{ backgroundColor: item.background_color }} /> {item.background_color}</>}
          </span>}
          {item.typography_warnings.length > 0 && <span className="translation-warning">{item.typography_warnings.map((warning) => TYPOGRAPHY_WARNING_LABELS[warning] ?? warning).join(", ")}</span>}
          {item.image_overlap_warnings.length > 0 && <span className="translation-warning">{item.image_overlap_warnings.map((warning) => OVERLAP_WARNING_LABELS[warning] ?? warning).join(", ")}</span>}
        </div>)}
      </details>
      {plan.replacements.some((item) => !item.eligible) && <details className="export-skips">
        <summary>Razlogi za preskok</summary>
        {plan.replacements.filter((item) => !item.eligible).map((item) => <p key={item.segment_id}><strong>{item.segment_id}</strong>: {REASON_LABELS[item.skip_reason ?? ""] ?? item.skip_reason ?? "ni upravičen"}</p>)}
      </details>}
    </>}
  </section>;
}

function ExportMetric({ label, value }: { label: string; value: number }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

const FIT_LABELS: Record<string, string> = {
  exact: "brez prilagoditve",
  expanded_bbox: "varen razširjen okvir",
  scaled: "pomanjšano",
  overflow: "se ne prilega",
  skipped: "preskočeno",
  missing_glyph: "manjkajoč znak",
  unsupported_style: "nepodprt slog",
};

const TYPOGRAPHY_WARNING_LABELS: Record<string, string> = {
  mixed_source_style: "mešan izvorni slog",
  source_style_fallback: "izvornega sloga ni bilo mogoče natančno določiti",
  droid_glyph_fallback: "uporabljena je nadomestna pisava Droid Sans Fallback",
  font_style_fallback: "nadomestna pisava ne ohrani reza",
};

const OVERLAP_LABELS: Record<string, string> = {
  simple_background: "varno popravljeno slikovno prekrivanje",
  complex_background: "kompleksno slikovno ozadje",
  image_only_text: "besedilo samo v sliki",
  uncertain: "negotovo slikovno območje",
};

const OVERLAP_WARNING_LABELS: Record<string, string> = {
  image_layer_contains_visible_text: "slikovna plast vsebuje vidno kopijo besedila",
  expanded_background_checked: "razširjeno območje je ponovno preverjeno",
  expanded_background_not_safe: "razširjeno območje ni varno",
  background_complexity_exceeds_limit: "ozadje presega dovoljeno kompleksnost",
  background_metrics_near_threshold: "meritve so preblizu varnostnemu pragu",
  image_only_text_requires_manual_ocr_workflow: "potreben je ročni OCR potek",
};

function formatSize(value: number): string {
  return `${value.toLocaleString("sl-SI", { maximumFractionDigits: 2 })} pt`;
}
