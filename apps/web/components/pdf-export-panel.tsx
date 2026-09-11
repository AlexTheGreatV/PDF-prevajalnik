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
      </div>
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
