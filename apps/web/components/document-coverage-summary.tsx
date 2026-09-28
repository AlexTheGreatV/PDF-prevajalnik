import { calculateDocumentCoverage, type DocumentCoverageState } from "../lib/document-coverage";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfExportPlan } from "../lib/pdf-export-client";
import type { PdfAnalysis } from "../lib/pdf-types";
import { formatPdfKind } from "../lib/pdf-view";
import type { TranslationJob } from "../lib/translation-types";

const STATE_LABELS: Record<DocumentCoverageState, string> = {
  text_missing: "Besedilo ni pridobljeno",
  ocr_review_required: "Potreben OCR oziroma ročni pregled",
  partially_translated: "Delno prevedeno",
  translated_not_reviewed: "Prevedeno, vendar še ni pregledano",
  partially_exported: "Delno izvoženo",
  known_text_export_complete: "Izvoz končan za vsa znana upravičena besedilna območja",
};

export function DocumentCoverageSummary({
  analysis,
  ocrItems,
  job,
  plan,
  downloadCompleted,
}: {
  analysis: PdfAnalysis;
  ocrItems: OcrReviewItem[];
  job: TranslationJob;
  plan: PdfExportPlan | null;
  downloadCompleted: boolean;
}) {
  const coverage = calculateDocumentCoverage({ analysis, ocrItems, job, plan, downloadCompleted });

  return <section className="document-coverage" aria-labelledby="document-coverage-title">
    <div className="translation-subhead">
      <div>
        <h4 id="document-coverage-title">Pokritost dokumenta</h4>
        <p>Meritve ločujejo pridobivanje besedila, prevajanje, ročni pregled in varen PDF izvoz.</p>
      </div>
      <div className="coverage-states">
        {coverage.states.map((state) => <span key={state}>{STATE_LABELS[state]}</span>)}
        {coverage.states.length === 0 && <span>Obdelava še ni zaključena</span>}
      </div>
    </div>

    <div className="coverage-metrics">
      <CoverageMetric label="Pridobljeno besedilo" value={`${coverage.text.acquired_pages} od ${coverage.text.total_pages} strani`} />
      <CoverageMetric label="Prevedeno" value={`${coverage.translation.translated} od ${coverage.translation.translatable} segmentov`} />
      <CoverageMetric label="Ročno potrjeno" value={`${coverage.review.reviewed} od ${coverage.review.translated} prevedenih`} />
      <CoverageMetric
        label="PDF izvoz"
        value={coverage.pdf_export.download_completed
          ? `${coverage.pdf_export.exported} od ${coverage.pdf_export.reviewed} potrjenih izvoženih`
          : coverage.pdf_export.plan_available
            ? `${coverage.pdf_export.ready} od ${coverage.pdf_export.reviewed} potrjenih varno pripravljenih`
            : "Načrt še ni pripravljen"}
      />
    </div>

    {coverage.warnings.map((warning) => <p className="translation-warning" key={warning}>{warning}</p>)}

    <div className="coverage-pages">
      {coverage.pages.map((page) => <article key={page.page_number} className={page.warnings.length ? "has-warning" : ""}>
        <header>
          <strong>Stran {page.page_number}</strong>
          <span>{formatPdfKind(page.page_type)}</span>
        </header>
        <dl>
          <div><dt>Digitalni segmenti</dt><dd>{page.digital_segments}</dd></div>
          <div><dt>Sprejeti OCR segmenti</dt><dd>{page.accepted_ocr_segments}</dd></div>
          <div><dt>Nerešeni OCR kandidati</dt><dd>{page.unresolved_ocr_candidates}</dd></div>
          <div><dt>Slikovna območja za OCR</dt><dd>{page.unreviewed_image_areas}</dd></div>
          <div><dt>Dekorativne slike</dt><dd>{page.decorative_image_areas}</dd></div>
          <div><dt>Slike za varnost izvoza</dt><dd>{page.export_safety_image_areas}</dd></div>
          <div><dt>Prevodljivi / prevedeni</dt><dd>{page.translatable_segments} / {page.translated_segments}</dd></div>
          <div><dt>Pregledani / za PDF</dt><dd>{page.reviewed_segments} / {page.export_ready_segments}</dd></div>
          <div><dt>Izvoženi</dt><dd>{page.exported_segments}</dd></div>
        </dl>
        {page.skips.length > 0 && <p>Preskoki: {page.skips.map((skip) => `${skip.count} × ${skip.reason}`).join("; ")}</p>}
        {page.warnings.map((warning) => <p className="translation-warning" key={warning}>{warning}</p>)}
      </article>)}
    </div>
  </section>;
}

function CoverageMetric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}
