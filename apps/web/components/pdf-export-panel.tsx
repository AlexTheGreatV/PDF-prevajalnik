"use client";

import { useState } from "react";

import { createCoverageRevisionKey } from "../lib/document-coverage";
import type { OcrReviewItem } from "../lib/ocr-review";
import { downloadPdfExport, preparePdfExport, type PdfExportMode, type PdfExportPlan } from "../lib/pdf-export-client";
import type { PdfAnalysis } from "../lib/pdf-types";
import type { TranslationJob } from "../lib/translation-types";
import { DocumentCoverageSummary } from "./document-coverage-summary";

const REASON_LABELS: Record<string, string> = {
  not_reviewed: "prevod ni potrjen",
  empty_translation: "prazen prevod",
  missing_bbox: "manjkajoče območje",
  invalid_page: "neveljavna stran",
  bbox_outside_page: "območje je zunaj strani",
  non_digital_source: "ni digitalno besedilo",
  ocr_only_source: "OCR območje",
  ocr_not_accepted: "OCR ali njegov prevod ni ročno potrjen",
  ocr_provenance_mismatch: "OCR vir ali območje se je po potrditvi spremenilo",
  ocr_region_too_small: "OCR območje je premajhno",
  ocr_background_unsafe: "ozadje OCR območja ni dokazljivo enotno",
  image_overlap: "območje se prekriva z vdelano sliko",
  complex_table: "kompleksna tabela",
  technical_value: "tehnična vrednost ostane nespremenjena",
  empty_cell: "prazna celica",
  duplicate_table_content: "podvojena tabelna vsebina",
  overlapping_table_replacement: "prekrivajoči se tabelni zamenjavi",
  ambiguous_cell_membership: "dvoumna pripadnost celice",
  merged_cell_ambiguous: "dvoumna združena celica",
  missing_cell_bbox: "manjkajoča geometrija celice",
  duplicate_cell_geometry: "podvojena geometrija celice",
  unsupported_table_structure: "nezanesljiva struktura tabele",
  cell_overflow: "prevod se ne prilega celici",
  rotated_page: "rotirana stran",
  source_text_mismatch: "izvorno besedilo se ne ujema",
  overlap_conflict: "prekrivanje območij",
  missing_glyph: "manjkajoč znak v pisavi",
  overflow: "prevod se ne prilega",
};

export function PdfExportPanel({ sourceFile, job, analysis, ocrItems }: {
  sourceFile: File;
  job: TranslationJob;
  analysis: PdfAnalysis;
  ocrItems: OcrReviewItem[];
}) {
  const [mode, setMode] = useState<PdfExportMode>("diagnostic");
  const [planned, setPlanned] = useState<{ revisionKey: string; plan: PdfExportPlan } | null>(null);
  const [busy, setBusy] = useState<"plan" | "download" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadedRevisionKey, setDownloadedRevisionKey] = useState<string | null>(null);
  const revisionKey = createCoverageRevisionKey({ analysis, ocrItems, job, mode });
  const plan = planned?.revisionKey === revisionKey ? planned.plan : null;
  const downloadCompleted = downloadedRevisionKey === revisionKey;

  async function prepare() {
    setBusy("plan");
    setError(null);
    setDownloadedRevisionKey(null);
    try {
      setPlanned({ revisionKey, plan: await preparePdfExport(sourceFile, job, mode) });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Načrta izvoza ni bilo mogoče pripraviti.");
    } finally {
      setBusy(null);
    }
  }

  async function download() {
    setBusy("download");
    setError(null);
    setDownloadedRevisionKey(null);
    try {
      const result = await downloadPdfExport(sourceFile, job, mode);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
      setDownloadedRevisionKey(revisionKey);
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
      <button className="button secondary" disabled={!plan || plan.summary.eligible + plan.ocr_summary.eligible_regions === 0 || busy !== null} onClick={() => void download()}>{busy === "download" ? "Pripravljam datoteko …" : "Prenesi novi PDF"}</button>
    </div>
    <DocumentCoverageSummary analysis={analysis} ocrItems={ocrItems} job={job} plan={plan} downloadCompleted={downloadCompleted} />
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
      {plan.tables.length > 0 && <section className="export-table-section">
        <h4>Tabelne celice</h4>
        <div className="export-summary">
          <ExportMetric label="Tabele" value={plan.table_summary.detected_tables} />
          <ExportMetric label="Potrjene celice" value={plan.table_summary.reviewed_cells} />
          <ExportMetric label="Upravičene" value={plan.table_summary.eligible_cells} />
          <ExportMetric label="Izvožene" value={plan.table_summary.exported_cells} />
          <ExportMetric label="Tehnične" value={plan.table_summary.technical_cells} />
          <ExportMetric label="Overflow" value={plan.table_summary.overflow_cells} />
          <ExportMetric label="Dvoumne" value={plan.table_summary.ambiguous_cells} />
          <ExportMetric label="Image-only" value={plan.table_summary.image_only_cells} />
          <ExportMetric label="Konfliktne" value={plan.table_summary.conflicting_cells} />
        </div>
        {plan.table_summary.partial_tables > 0 && <p className="translation-warning">Del tabel bo izvožen samo delno. Preskočene celice ostanejo nespremenjene.</p>}
        {plan.tables.map((table) => <details key={table.table_id} className="export-table-detail">
          <summary>Stran {table.page_number} · {table.table_id} · {table.row_count} × {table.column_count}{table.partial_export ? " · delni izvoz" : ""}</summary>
          {table.cells.filter((cell) => !cell.eligible).map((cell) => <p key={cell.segment_id}>
            <strong>r{cell.row_index + 1} c{cell.column_index + 1}</strong> · {cell.source_text || "prazna celica"}: {REASON_LABELS[cell.skip_reason ?? ""] ?? cell.skip_reason ?? "ni upravičena"}
          </p>)}
        </details>)}
      </section>}
      {plan.ocr_replacements.length > 0 && <section className="export-table-section">
        <h4>Ročno potrjena OCR območja</h4>
        <div className="export-summary">
          <ExportMetric label="Potrjena" value={plan.ocr_summary.reviewed_regions} />
          <ExportMetric label="Upravičena" value={plan.ocr_summary.eligible_regions} />
          <ExportMetric label="Varno izvožena" value={plan.ocr_summary.exported_regions} />
          <ExportMetric label="Nevarna" value={plan.ocr_summary.unsafe_regions} />
          <ExportMetric label="Zastarela" value={plan.ocr_summary.stale_regions} />
        </div>
        {plan.ocr_replacements.map((item) => <p key={item.segment_id}>
          <strong>Stran {item.page_number} · {item.ocr_review_id ?? item.segment_id}</strong>: {item.eligible ? "varen selektivni izvoz" : REASON_LABELS[item.skip_reason ?? ""] ?? item.skip_reason ?? "ni upravičeno"}
        </p>)}
        {plan.ocr_replacements.some((item) => !item.eligible) && <p className="translation-warning">Preskočena OCR območja ostanejo v izvornem PDF-ju nespremenjena.</p>}
      </section>}
      <details className="export-typography">
        <summary>Tipografske podrobnosti</summary>
        {[...plan.replacements, ...plan.ocr_replacements].map((item) => <div key={item.segment_id} className="export-typography-row">
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
      {[...plan.replacements, ...plan.ocr_replacements].some((item) => !item.eligible) && <details className="export-skips">
        <summary>Razlogi za preskok</summary>
        {[...plan.replacements, ...plan.ocr_replacements].filter((item) => !item.eligible).map((item) => <p key={item.segment_id}><strong>{item.segment_id}</strong>: {REASON_LABELS[item.skip_reason ?? ""] ?? item.skip_reason ?? "ni upravičen"}</p>)}
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
  table_content_background_checked: "celotno območje celice je ponovno preverjeno",
  table_content_background_not_safe: "ozadje celotnega območja celice ni varno",
};

function formatSize(value: number): string {
  return `${value.toLocaleString("sl-SI", { maximumFractionDigits: 2 })} pt`;
}
