import { apiBaseUrl } from "./pdf-client";
import type { BBox } from "./pdf-types";
import type { TranslationJob } from "./translation-types";
import { isTranslationUnitSegment } from "./translation-units";

export type PdfExportMode = "diagnostic" | "replacement_preview";
export const FINAL_PDF_EXPORT_MODE: PdfExportMode = "replacement_preview";
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

export type PdfExportDownload = {
  blob: Blob;
  filename: string;
  mode: PdfExportMode;
  applied_replacements: number;
};

export function resolvePdfExportMode(simpleMode: boolean, selectedMode: PdfExportMode): PdfExportMode {
  return simpleMode ? FINAL_PDF_EXPORT_MODE : selectedMode;
}

export function validatePdfExportDownload(
  result: Pick<PdfExportDownload, "mode" | "applied_replacements">,
  requestedMode: PdfExportMode,
  expectedReplacements: number,
): boolean {
  if (result.mode !== requestedMode) {
    throw new Error("Strežnik je vrnil napačen način PDF izvoza.");
  }
  if (requestedMode !== FINAL_PDF_EXPORT_MODE) return false;
  if (expectedReplacements <= 0) {
    throw new Error("Končni PDF nima nobene potrjene varne zamenjave.");
  }
  if (result.applied_replacements !== expectedReplacements) {
    throw new Error("Končni PDF ne vsebuje pričakovanega števila varnih zamenjav.");
  }
  return true;
}

export async function downloadPdfExport(file: File, job: TranslationJob, mode: PdfExportMode): Promise<PdfExportDownload> {
  const response = await postExportRequest("/api/pdf/export-translated", file, job, mode);
  if (!response.ok) throw new Error(await exportError(response));
  const disposition = response.headers.get("content-disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const contentType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
  const responseMode = response.headers.get("x-pdf-export-mode");
  const applied = response.headers.get("x-pdf-replacements-applied");
  if (contentType !== "application/pdf") {
    throw new Error("Strežnik ni vrnil veljavnega PDF odgovora.");
  }
  if (!encoded) {
    throw new Error("Strežnik ni potrdil varnega imena PDF datoteke.");
  }
  let filename: string;
  try {
    filename = decodeURIComponent(encoded);
  } catch {
    throw new Error("Strežnik je vrnil neveljavno ime PDF datoteke.");
  }
  if (!filename.toLowerCase().endsWith(".pdf") || /[\\/\u0000-\u001f\u007f]/.test(filename)) {
    throw new Error("Strežnik je vrnil neveljavno ime PDF datoteke.");
  }
  if (responseMode !== "diagnostic" && responseMode !== "replacement_preview") {
    throw new Error("Strežnik ni potrdil načina PDF izvoza.");
  }
  if (applied === null || !/^\d+$/.test(applied)) {
    throw new Error("Strežnik ni potrdil števila zapisanih PDF zamenjav.");
  }
  return {
    blob: await response.blob(),
    filename,
    mode: responseMode,
    applied_replacements: Number(applied),
  };
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
