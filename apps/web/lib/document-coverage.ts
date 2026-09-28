import type { OcrReviewItem } from "./ocr-review";
import type { PdfExportPlan } from "./pdf-export-client";
import type { PdfAnalysis, PdfKind, StructuredImage } from "./pdf-types";
import type { TranslationJob, TranslationSegment } from "./translation-types";
import { isTranslationUnitSegment } from "./translation-units";

export function createCoverageRevisionKey(input: {
  analysis: PdfAnalysis;
  ocrItems: OcrReviewItem[];
  job: TranslationJob;
  mode: string;
}): string {
  return JSON.stringify({
    document_sha256: input.analysis.sha256,
    mode: input.mode,
    job: input.job,
    ocr_sources: input.ocrItems.map((item) => ({
      id: item.id,
      status: item.status,
      selected_text_source: item.selected_text_source,
      effective_text: item.effective_text,
      pdf_bbox: item.pdf_bbox,
      server_revision_id: item.server_revision_id,
    })),
  });
}

export type DocumentCoverageState =
  | "text_missing"
  | "ocr_review_required"
  | "partially_translated"
  | "translated_not_reviewed"
  | "partially_exported"
  | "known_text_export_complete";

export type CoverageSkip = { reason: string; count: number };

export type PageCoverage = {
  page_number: number;
  page_type: PdfKind;
  digital_segments: number;
  accepted_ocr_segments: number;
  unresolved_ocr_candidates: number;
  unreviewed_image_areas: number;
  decorative_image_areas: number;
  export_safety_image_areas: number;
  translatable_segments: number;
  translated_segments: number;
  reviewed_segments: number;
  export_ready_segments: number;
  exported_segments: number;
  skips: CoverageSkip[];
  has_acquired_text: boolean;
  warnings: string[];
};

export type DocumentCoverage = {
  pages: PageCoverage[];
  states: DocumentCoverageState[];
  text: { acquired_pages: number; total_pages: number; missing_pages: number[] };
  translation: { translated: number; translatable: number };
  review: { reviewed: number; translated: number };
  pdf_export: { ready: number; exported: number; reviewed: number; plan_available: boolean; download_completed: boolean };
  unresolved_ocr_candidates: number;
  unreviewed_image_areas: number;
  warnings: string[];
};

export function calculateDocumentCoverage(input: {
  analysis: PdfAnalysis;
  ocrItems: OcrReviewItem[];
  job: TranslationJob;
  plan: PdfExportPlan | null;
  downloadCompleted: boolean;
}): DocumentCoverage {
  const { analysis, ocrItems, job, plan, downloadCompleted } = input;
  const exportItems = new Map(
    [...(plan?.replacements ?? []), ...(plan?.ocr_replacements ?? [])]
      .map((item) => [item.segment_id, item] as const),
  );

  const pages = analysis.pages.map((analysisPage) => {
    const pageNumber = analysisPage.page_number;
    const structuredPage = analysis.document?.pages.find((page) => page.page_number === pageNumber);
    const segments = job.segments.filter((segment) =>
      segment.page_number === pageNumber && isTranslationUnitSegment(segment),
    );
    const pageOcrItems = ocrItems.filter((item) => item.page_number === pageNumber);
    const digitalSegments = segments.filter((segment) =>
      segment.source_text_origin === "native" && Boolean(segment.source_text.trim()),
    ).length;
    const acceptedOcrSegments = segments.filter(hasCurrentAcceptedOcrSource).length;
    const unresolvedOcrIds = new Set(pageOcrItems.filter(isUnresolvedOcrItem).map((item) => item.id));
    for (const segment of segments.filter((item) => item.source_text_origin === "accepted_ocr" && !hasCurrentAcceptedOcrSource(item))) {
      const reviewId = segment.accepted_ocr_source?.review_id;
      if (reviewId) unresolvedOcrIds.add(reviewId);
      else if (unresolvedOcrIds.size === 0) unresolvedOcrIds.add(segment.segment_id);
    }
    const unresolvedOcrCandidates = unresolvedOcrIds.size;
    const unreviewedImageAreas = (structuredPage?.images ?? []).filter((image) =>
      imageNeedsTextAcquisition(image, analysisPage.kind) && !hasAcceptedOcrForImage(image, pageOcrItems),
    ).length;
    const decorativeImageAreas = (structuredPage?.images ?? []).filter((image) => image.ocr_relevance === "decorative").length;
    const exportSafetyImageAreas = (structuredPage?.images ?? []).filter((image) =>
      (image.ocr_relevance ?? (analysisPage.kind === "digital" ? "export_safety_only" : "text_candidate")) === "export_safety_only",
    ).length;
    const translatable = segments.filter(isTranslatable);
    const translated = translatable.filter(isTranslated);
    const reviewed = translatable.filter((segment) => segment.status === "reviewed");
    const exportReady = reviewed.filter((segment) => exportItems.get(segment.segment_id)?.eligible).length;
    const exported = downloadCompleted ? exportReady : 0;
    const hasAcquiredText = digitalSegments + acceptedOcrSegments > 0;
    const warnings: string[] = [];
    if (!hasAcquiredText) warnings.push("Na tej strani besedilo ni bilo pridobljeno.");
    if (unresolvedOcrCandidates > 0) warnings.push(`Nerešeni OCR kandidati: ${unresolvedOcrCandidates}.`);
    if (unreviewedImageAreas > 0) warnings.push(`Nepregledana slikovna območja: ${unreviewedImageAreas}.`);
    if (plan && reviewed.length > exportReady) warnings.push(`Potrjeni segmenti, ki niso varno izvozljivi: ${reviewed.length - exportReady}.`);

    return {
      page_number: pageNumber,
      page_type: structuredPage?.page_type ?? analysisPage.kind,
      digital_segments: digitalSegments,
      accepted_ocr_segments: acceptedOcrSegments,
      unresolved_ocr_candidates: unresolvedOcrCandidates,
      unreviewed_image_areas: unreviewedImageAreas,
      decorative_image_areas: decorativeImageAreas,
      export_safety_image_areas: exportSafetyImageAreas,
      translatable_segments: translatable.length,
      translated_segments: translated.length,
      reviewed_segments: reviewed.length,
      export_ready_segments: exportReady,
      exported_segments: exported,
      skips: collectPageSkips(segments, exportItems),
      has_acquired_text: hasAcquiredText,
      warnings,
    } satisfies PageCoverage;
  });

  const acquiredPages = pages.filter((page) => page.has_acquired_text).length;
  const missingPages = pages.filter((page) => !page.has_acquired_text).map((page) => page.page_number);
  const translatable = pages.reduce((total, page) => total + page.translatable_segments, 0);
  const translated = pages.reduce((total, page) => total + page.translated_segments, 0);
  const reviewed = pages.reduce((total, page) => total + page.reviewed_segments, 0);
  const ready = pages.reduce((total, page) => total + page.export_ready_segments, 0);
  const exported = pages.reduce((total, page) => total + page.exported_segments, 0);
  const unresolved = pages.reduce((total, page) => total + page.unresolved_ocr_candidates, 0);
  const imageGaps = pages.reduce((total, page) => total + page.unreviewed_image_areas, 0);
  const hasUnknownGaps = missingPages.length > 0 || unresolved > 0 || imageGaps > 0;
  const states: DocumentCoverageState[] = [];

  if (acquiredPages === 0) states.push("text_missing");
  if (hasUnknownGaps) states.push("ocr_review_required");
  if (translated > 0 && translated < translatable) states.push("partially_translated");
  if (translatable > 0 && translated === translatable && reviewed < translated) states.push("translated_not_reviewed");
  if (downloadCompleted && reviewed > exported) states.push("partially_exported");
  if (downloadCompleted && translatable > 0 && reviewed === translatable && exported === reviewed && !hasUnknownGaps) {
    states.push("known_text_export_complete");
  }

  const warnings: string[] = [];
  if (missingPages.length > 0) warnings.push(`Besedilo ni pridobljeno na straneh: ${missingPages.join(", ")}.`);
  if (unresolved > 0) warnings.push(`Nerešeni ali neveljavni OCR kandidati: ${unresolved}.`);
  if (imageGaps > 0) warnings.push(`Nepregledana zaznana slikovna območja: ${imageGaps}.`);
  if (plan && reviewed > ready) warnings.push("Načrt PDF izvoza je delen; preskočena območja ostanejo nespremenjena.");

  return {
    pages,
    states,
    text: { acquired_pages: acquiredPages, total_pages: pages.length, missing_pages: missingPages },
    translation: { translated, translatable },
    review: { reviewed, translated },
    pdf_export: { ready, exported, reviewed, plan_available: Boolean(plan), download_completed: downloadCompleted },
    unresolved_ocr_candidates: unresolved,
    unreviewed_image_areas: imageGaps,
    warnings,
  };
}

function imageNeedsTextAcquisition(image: StructuredImage, pageKind: PdfKind): boolean {
  const relevance = image.ocr_relevance ?? (pageKind === "digital" ? "export_safety_only" : "text_candidate");
  return relevance === "text_candidate" && image.needs_ocr_check;
}

function isTranslatable(segment: TranslationSegment): boolean {
  return !segment.skip_reason;
}

function isTranslated(segment: TranslationSegment): boolean {
  return Boolean(segment.edited_translation.trim()) && ["translated", "reviewed"].includes(segment.status);
}

function hasCurrentAcceptedOcrSource(segment: TranslationSegment): boolean {
  return segment.source_text_origin === "accepted_ocr" && Boolean(segment.accepted_ocr_source?.revision_id?.trim());
}

function isUnresolvedOcrItem(item: OcrReviewItem): boolean {
  if (item.status === "accepted") return !item.server_revision_id?.trim();
  if (item.status === "rejected" && item.native_text.trim()) return false;
  return item.status !== "rejected" || !item.native_text.trim();
}

function hasAcceptedOcrForImage(image: StructuredImage, items: OcrReviewItem[]): boolean {
  if (!image.bbox) return false;
  const imageBBox = image.bbox;
  return items.some((item) =>
    item.status === "accepted" &&
    Boolean(item.server_revision_id?.trim()) &&
    ["image", "manual"].includes(item.source) &&
    overlapRatio(imageBBox, item.pdf_bbox) >= 0.8,
  );
}

function overlapRatio(first: { x0: number; y0: number; x1: number; y1: number }, second: { x0: number; y0: number; x1: number; y1: number }): number {
  const width = Math.max(0, Math.min(first.x1, second.x1) - Math.max(first.x0, second.x0));
  const height = Math.max(0, Math.min(first.y1, second.y1) - Math.max(first.y0, second.y0));
  const firstArea = Math.max(1, (first.x1 - first.x0) * (first.y1 - first.y0));
  return (width * height) / firstArea;
}

function collectPageSkips(
  segments: TranslationSegment[],
  exportItems: Map<string, { eligible: boolean; skip_reason: string | null }>,
): CoverageSkip[] {
  const counts = new Map<string, number>();
  for (const segment of segments) {
    const exportItem = exportItems.get(segment.segment_id);
    const reason = segment.skip_reason ?? (exportItem && !exportItem.eligible ? exportItem.skip_reason : null);
    if (reason) counts.set(reason, (counts.get(reason) ?? 0) + 1);
  }
  return [...counts.entries()].map(([reason, count]) => ({ reason, count }));
}
