import { apiBaseUrl } from "./pdf-client";
import type { BBox } from "./pdf-types";
import type { TranslationJob } from "./translation-types";
import { isTranslationUnitSegment } from "./translation-units";

export type PdfExportMode = "diagnostic" | "replacement_preview";
export type ImageOverlapKind = "none" | "simple_background" | "complex_background" | "image_only_text" | "uncertain";

export type PdfExportReplacement = {
  segment_id: string;
  page_number: number;
  source_bbox: BBox | null;
  original_bbox: BBox | null;
  fitted_bbox: BBox | null;
  translated_text: string;
  reviewed: boolean;
  fit_status: "not_checked" | "fits" | "overflow" | "skipped";
  eligible: boolean;
  warnings: string[];
  skip_reason: string | null;
  requested_font_size: number;
  fitted_font_size: number | null;
  scale: number | null;
  original_line_count: number;
  fitted_line_count: number | null;
  font_family: string;
  font_weight: "regular" | "bold";
  font_style: "normal" | "italic";
  color: string;
  fit_strategy: "not_checked" | "exact" | "expanded_bbox" | "scaled" | "overflow" | "unsupported_style" | "missing_glyph" | "skipped";
  typography_warnings: string[];
  image_overlap_kind: ImageOverlapKind;
  overlapping_image_xrefs: number[];
  overlap_ratio: number;
  background_patch_used: boolean;
  background_color: string | null;
  patch_rects: BBox[];
  overlap_confidence: number;
  background_metrics: {
    dpi: number;
    sample_count: number;
    color_variance: number | null;
    edge_density: number | null;
    texture_score: number | null;
    side_color_delta: number | null;
    near_threshold: boolean;
  } | null;
  image_overlap_warnings: string[];
};

export type PdfTableCellReplacement = PdfExportReplacement & {
  table_id: string;
  row_index: number;
  column_index: number;
  row_span: number;
  column_span: number;
  cell_bbox: BBox | null;
  content_bbox: BBox | null;
  source_text: string;
  source_match_status: "matched" | "mismatch" | "not_checked";
  background_fill: string | null;
  border_clearance: number;
};

export type PdfTableExportPlan = {
  table_id: string;
  page_number: number;
  bbox: BBox | null;
  row_count: number;
  column_count: number;
  reliable: boolean;
  partial_export: boolean;
  warnings: string[];
  cells: PdfTableCellReplacement[];
};

export type PdfOcrReplacement = PdfExportReplacement & {
  ocr_review_id: string | null;
  ocr_source: "block" | "page" | "image" | "manual" | null;
  ocr_reason: string | null;
};

export type PdfExportPlan = {
  filename: string;
  mode: PdfExportMode;
  document_sha256: string;
  summary: {
    total_segments: number;
    reviewed: number;
    eligible: number;
    skipped: number;
    overflow: number;
    conflicts: number;
    exact: number;
    expanded: number;
    scaled: number;
    background_patches: number;
    image_overlap_skipped: number;
  };
  replacements: PdfExportReplacement[];
  table_summary: {
    detected_tables: number;
    reviewed_cells: number;
    eligible_cells: number;
    exported_cells: number;
    technical_cells: number;
    overflow_cells: number;
    ambiguous_cells: number;
    image_only_cells: number;
    conflicting_cells: number;
    partial_tables: number;
  };
  tables: PdfTableExportPlan[];
  ocr_summary: {
    reviewed_regions: number;
    eligible_regions: number;
    exported_regions: number;
    unsafe_regions: number;
    stale_regions: number;
  };
  ocr_replacements: PdfOcrReplacement[];
};

export async function preparePdfExport(file: File, job: TranslationJob, mode: PdfExportMode): Promise<PdfExportPlan> {
  const response = await postExportRequest("/api/pdf/export-translated/plan", file, job, mode);
  if (!response.ok) throw new Error(await exportError(response));
  return response.json() as Promise<PdfExportPlan>;
}

export async function downloadPdfExport(file: File, job: TranslationJob, mode: PdfExportMode): Promise<{ blob: Blob; filename: string }> {
  const response = await postExportRequest("/api/pdf/export-translated", file, job, mode);
  if (!response.ok) throw new Error(await exportError(response));
  const disposition = response.headers.get("content-disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  return { blob: await response.blob(), filename: encoded ? decodeURIComponent(encoded) : "document-sl-preview.pdf" };
}

function postExportRequest(path: string, file: File, job: TranslationJob, mode: PdfExportMode): Promise<Response> {
  const form = new FormData();
  form.append("file", file);
  form.append("translation_job", JSON.stringify({
    ...job,
    segments: job.segments.filter(isTranslationUnitSegment),
  }));
  form.append("mode", mode);
  return fetch(`${apiBaseUrl}${path}`, { method: "POST", body: form });
}

async function exportError(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { detail?: string | { message?: string } };
    if (typeof payload.detail === "string") return payload.detail;
    if (payload.detail?.message) return payload.detail.message;
  } catch {
    // The API may return a non-JSON proxy error.
  }
  return response.statusText || "Izvoz PDF ni uspel.";
}
