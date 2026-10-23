"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { createCoverageRevisionKey } from "../lib/document-coverage";
import type { OcrReviewItem } from "../lib/ocr-review";
import {
  downloadPdfExport,
  currentRevisionValue,
  isCurrentPdfExportRevision,
  preparePdfExport,
  resolvePdfExportMode,
  validatePdfExportDownload,
  validatePdfTypographyDownload,
  pdfTypographyRevisionKey,
  PdfExportQcError,
  type PdfTypographyProfile,
  type PdfExportMode,
  type PdfExportPlan,
} from "../lib/pdf-export-client";
import type { PdfAnalysis } from "../lib/pdf-types";
import type { TranslationJob, TranslationSegment } from "../lib/translation-types";
import { DocumentCoverageSummary } from "./document-coverage-summary";
import { TableReconstructionPanel } from "./table-reconstruction-panel";
import { reconstructionRevision } from "../lib/table-reconstruction";

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
  missing_table_provenance: "manjka dokazljiva povezava z izvorno celico",
  stale_table_provenance: "tabelna provenance se ne ujema z izvornim PDF-jem",
  span_crosses_cell_boundary: "izvorno besedilo prečka mejo celice",
  graphic_cell: "celica vsebuje grafiko, polnilo ali barvni vzorec",
  rotated_cell: "rotirano besedilo v celici ni podprto",
  cell_overflow: "prevod se ne prilega celici",
  rotated_page: "rotirana stran",
  source_text_mismatch: "izvorno besedilo se ne ujema",
  overlap_conflict: "prekrivanje območij",
  missing_glyph: "manjkajoč znak v pisavi",
  overflow: "prevod se ne prilega",
};

export type PdfExportUiState = {
  plan: PdfExportPlan | null;
  downloadCompleted: boolean;
  appliedReplacements: number;
};

export function PdfExportPanel({ sourceFile, job, analysis, ocrItems, simpleMode = false, onStateChange, onNavigateSegment }: {
  sourceFile: File;
  job: TranslationJob;
  analysis: PdfAnalysis;
  ocrItems: OcrReviewItem[];
  simpleMode?: boolean;
  onStateChange?: (state: PdfExportUiState) => void;
  onNavigateSegment?: (segmentId: string) => void;
}) {
  const [mode, setMode] = useState<PdfExportMode>("diagnostic");
  const [typographyProfile, setTypographyProfile] = useState<PdfTypographyProfile>("original");
  const [reconstructionTable, setReconstructionTable] = useState("");
  const [reconstructedState, setReconstructedState] = useState<{ revisionKey: string; value: PdfExportUiState } | null>(null);
  const tableChoices = analysis.document?.pages.flatMap(page => page.regions.flatMap(region => region.blocks
    .filter(block => block.table_provenance)
    .map(block => ({ page_number: page.page_number, table_id: block.table_provenance!.table_id,
      rows: block.table_provenance!.row_count, columns: block.table_provenance!.column_count })))) ?? [];
  const selectedTable = effectiveTableChoice(tableChoices, reconstructionTable);
  const [planned, setPlanned] = useState<{ revisionKey: string; value: PdfExportPlan } | null>(null);
  const [busy, setBusy] = useState<"plan" | "download" | null>(null);
  const [error, setError] = useState<{ revisionKey: string; value: string; blockers?: TranslationSegment[] } | null>(null);
  const [downloadedRevisionKey, setDownloadedRevisionKey] = useState<string | null>(null);
  const [appliedReplacements, setAppliedReplacements] = useState(0);
  const [diagnosticDownloadedRevisionKey, setDiagnosticDownloadedRevisionKey] = useState<string | null>(null);
  const busyRef = useRef(false);
  const effectiveMode = resolvePdfExportMode(simpleMode, mode);
  const effectiveProfile = effectiveMode === "diagnostic" ? "original" : typographyProfile;
  const reconstruction = effectiveMode === "replacement_preview" ? selectedTable : null;
  const revisionKey = reconstructionRevision(pdfTypographyRevisionKey(createCoverageRevisionKey({ analysis, ocrItems, job, mode: effectiveMode }), effectiveProfile), reconstruction);
  const revisionKeyRef = useRef(revisionKey);
  useLayoutEffect(() => {
    revisionKeyRef.current = revisionKey;
    return () => { revisionKeyRef.current = ""; };
  }, [revisionKey]);
  const plan = currentRevisionValue(planned, revisionKey);
  const currentError = currentRevisionValue(error, revisionKey);
  const downloadCompleted = downloadedRevisionKey === revisionKey;
  const diagnosticDownloadCompleted = diagnosticDownloadedRevisionKey === revisionKey;
  const activeReconstruction = currentRevisionValue(reconstructedState, revisionKey);
  const activePlan = reconstruction ? activeReconstruction?.plan ?? null : plan;
  const activeDownloaded = reconstruction ? activeReconstruction?.downloadCompleted ?? false : downloadCompleted;
  const activeApplied = reconstruction ? activeReconstruction?.appliedReplacements ?? 0 : downloadCompleted ? appliedReplacements : 0;

  useEffect(() => {
    onStateChange?.({ plan: activePlan, downloadCompleted: activeDownloaded, appliedReplacements: activeApplied });
  }, [activeApplied, activeDownloaded, activePlan, onStateChange]);

  async function prepare() {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy("plan");
    setError(null);
    setPlanned(null);
    setDownloadedRevisionKey(null);
    setAppliedReplacements(0);
    setDiagnosticDownloadedRevisionKey(null);
    const requestRevision = revisionKey;
    try {
      const preparedPlan = await preparePdfExport(sourceFile, job, effectiveMode, effectiveProfile);
      if (effectiveProfile !== "original" && preparedPlan.typography_coverage?.profile !== effectiveProfile) {
        throw new Error("Načrt ne uporablja izbranega tipografskega profila.");
      }
      if (!isCurrentPdfExportRevision(requestRevision, revisionKeyRef.current)) return;
      setPlanned({ revisionKey: requestRevision, value: preparedPlan });
    } catch (caught) {
      if (isCurrentPdfExportRevision(requestRevision, revisionKeyRef.current)) setError({
        revisionKey: requestRevision,
        value: caught instanceof Error ? caught.message : "Načrta izvoza ni bilo mogoče pripraviti.",
        blockers: caught instanceof PdfExportQcError ? caught.blockers : undefined,
      });
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  async function download() {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy("download");
    setError(null);
    setDownloadedRevisionKey(null);
    setAppliedReplacements(0);
    setDiagnosticDownloadedRevisionKey(null);
    const requestRevision = revisionKey;
    try {
      const result = await downloadPdfExport(sourceFile, job, effectiveMode, effectiveProfile);
      validatePdfTypographyDownload(result, effectiveProfile, plan);
      const expectedReplacements = (plan?.summary.eligible ?? 0) + (plan?.ocr_summary.eligible_regions ?? 0);
      const finalDownload = validatePdfExportDownload(result, effectiveMode, expectedReplacements);
      if (!isCurrentPdfExportRevision(requestRevision, revisionKeyRef.current)) return;
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      anchor.click();
      URL.revokeObjectURL(url);
      if (finalDownload) {
        setAppliedReplacements(result.applied_replacements);
        setDownloadedRevisionKey(requestRevision);
      }
      else setDiagnosticDownloadedRevisionKey(requestRevision);
    } catch (caught) {
      if (isCurrentPdfExportRevision(requestRevision, revisionKeyRef.current)) setError({
        revisionKey: requestRevision,
        value: caught instanceof Error ? caught.message : "Izvoz PDF ni uspel.",
        blockers: caught instanceof PdfExportQcError ? caught.blockers : undefined,
      });
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  }

  return <section className={`pdf-export-panel${simpleMode ? " guided-export" : ""}`}>
    <div className="translation-subhead">
      <div><h3>{simpleMode ? (plan ? "Prenesi prevedeni PDF" : "Pripravi PDF") : effectiveMode === "diagnostic" ? "Diagnostični PDF" : "Končni prevedeni PDF"}</h3><p>{effectiveMode === "diagnostic" ? "Diagnostični PDF vsebuje označena območja in ni končni prevedeni dokument." : "Izvozijo se samo ročno potrjeni in geometrijsko preverjeni prevodi."}</p></div>
      <div className={`segmented${simpleMode ? " advanced-only" : ""}`} aria-label="Način izvoza PDF">
        <button className={mode === "diagnostic" ? "active" : ""} onClick={() => { setMode("diagnostic"); setError(null); }}>Diagnostični PDF</button>
        <button className={mode === "replacement_preview" ? "active" : ""} onClick={() => { setMode("replacement_preview"); setError(null); }}>Končni prevedeni PDF</button>
      </div>
    </div>
    {effectiveMode !== "diagnostic" && <div className="translation-languages"><label>Oblikovanje PDF-ja
      <select value={typographyProfile} onChange={(event) => setTypographyProfile(event.target.value as PdfTypographyProfile)}>
        <option value="original">Ohrani izvorne sloge</option>
        <option value="document-role-typography-pilot-v1">Poenoti tipografske skupine</option>
      </select>
      {typographyProfile !== "original" && <small>Poenotenje lahko spremeni prileganje. Varno oblikovano izvorno besedilo ni prevedena vsebina.</small>}
    </label></div>}
    {effectiveMode !== "diagnostic" && tableChoices.length > 0 && <label className="reconstruction-choice">Rekonstruiraj tabelo
      <select value={selectedTable ? reconstructionTable : ""} onChange={event => setReconstructionTable(event.target.value)}>
        <option value="">Brez rekonstrukcije — običajne zamenjave</option>
        {tableChoices.map(table => <option key={`${table.page_number}:${table.table_id}`} value={`${table.page_number}:${table.table_id}`}>Stran {table.page_number} · {table.rows} × {table.columns} · {table.table_id}</option>)}
      </select>
    </label>}
    {reconstruction && <TableReconstructionPanel key={revisionKey} file={sourceFile} job={job} profile={effectiveProfile} selection={reconstruction} revision={revisionKey} onStateChange={setReconstructedState} />}
    {!reconstruction && <><div className="translation-actions">
      {!plan && <button className="button" disabled={busy !== null || !job.document_identity} onClick={() => void prepare()}>{busy === "plan" ? "Preverjam izvoz …" : "Pripravi PDF"}</button>}
      {plan && <button className="button" disabled={downloadCompleted || diagnosticDownloadCompleted || plan.summary.eligible + plan.ocr_summary.eligible_regions === 0 || busy !== null} onClick={() => void download()}>{busy === "download" ? "Pripravljam datoteko …" : downloadCompleted ? "PDF je prenesen" : diagnosticDownloadCompleted ? "Diagnostični PDF je prenesen" : effectiveMode === "diagnostic" ? "Prenesi diagnostični PDF" : "Prenesi prevedeni PDF"}</button>}
      {!simpleMode && plan && <button className="button secondary" disabled={busy !== null} onClick={() => void prepare()}>Znova preveri izvoz</button>}
    </div>
    <div className="translation-state-grid" aria-label="Stanje PDF izvoza">
      <span><strong>Izvoz</strong>{currentError ? "Neuspešno" : downloadCompleted ? "Uspešno" : plan ? (plan.summary.eligible + plan.ocr_summary.eligible_regions > 0 ? "Primerno za varen delni izvoz" : "Blokirano") : "Načrt ni pripravljen"}</span>
    </div>
    <DocumentCoverageSummary analysis={analysis} ocrItems={ocrItems} job={job} plan={plan} downloadCompleted={downloadCompleted} appliedReplacements={appliedReplacements} />
    {!job.document_identity && <p className="translation-warning">Opravilo nima preverjene identitete PDF-ja. Znova ga povežite s trenutno analizo.</p>}
    {currentError && <p className="translation-error">{currentError}</p>}
    {currentError && error?.blockers && <PdfExportQcDiagnostics blockers={error.blockers} onNavigateSegment={onNavigateSegment} />}
    {plan?.typography_coverage && <section aria-label="Tipografska pokritost">
      <h4>Tipografska pokritost {downloadCompleted ? "izvedenega izvoza" : "načrta"}</h4>
      <p>Prevodi: {plan.typography_coverage.translated} · Samo oblikovani izvori: {plan.typography_coverage.source_formatted} · Nedotaknjeno: {plan.typography_coverage.untouched}</p>
      <details><summary>Slogovne skupine in izjeme</summary>
        {plan.typography_coverage.groups.map((group) => <p key={group.group}>
          {group.role} · strani {group.pages.join(", ")} · {group.unified}/{group.total} · {group.partial ? "delno" : "poenoteno"}
          {group.partial && <> · {Object.entries(group.excluded_reasons).map(([reason, count]) => `${reason}: ${count}`).join(", ")}</>}
        </p>)}
      </details>
    </section>}
    {plan && <div className={simpleMode ? "guided-export-details" : ""}>
      <div className="export-summary">
        <ExportMetric label={downloadCompleted ? "Dejansko zamenjani" : "Načrtovane zamenjave"} value={downloadCompleted ? appliedReplacements : plan.resolution_summary.replacement_planned} />
        <ExportMetric label="Pravilno ohranjeni" value={plan.resolution_summary.preserved_identical} />
        <ExportMetric label="Niso prevajalske enote" value={plan.resolution_summary.not_applicable} />
        <ExportMetric label="Potrjeni, vendar blokirani" value={plan.resolution_summary.blocked_total} />
        <ExportMetric label="Skupaj razrešeni" value={(downloadCompleted ? appliedReplacements : 0) + plan.resolution_summary.preserved_identical} />
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
            <small> · {cell.cell_id ?? "brez cell ID"} · {cell.geometry_evidence ?? "brez dokaza"} · spani: {cell.source_span_ids.join(", ") || "brez"}{cell.provenance_block_reason ? ` · ${cell.provenance_block_reason}` : ""}</small>
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
    </div>}</>}
  </section>;
}

function effectiveTableChoice(choices: { page_number: number; table_id: string }[], selected: string) {
  return choices.find(table => `${table.page_number}:${table.table_id}` === selected) ?? null;
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

export function PdfExportQcDiagnostics({ blockers, onNavigateSegment }: {
  blockers: TranslationSegment[];
  onNavigateSegment?: (segmentId: string) => void;
}) {
  return <>{blockers.map((segment) => <div key={segment.segment_id} className="translation-error">
    <a href={`#translation-${segment.segment_id}`} onClick={() => onNavigateSegment?.(segment.segment_id)}>Odpri segment {segment.segment_id}</a>
    <p>Izvor: {segment.source_text}</p>
    <p>Trenutni prevod: {segment.edited_translation || segment.translated_text}</p>
    {segment.current_qc?.findings.filter((finding) => finding.severity === "blocking").map((finding, index) => <p key={index}>{finding.message}</p>)}
  </div>)}</>;
}

function formatSize(value: number): string {
  return `${value.toLocaleString("sl-SI", { maximumFractionDigits: 2 })} pt`;
}
