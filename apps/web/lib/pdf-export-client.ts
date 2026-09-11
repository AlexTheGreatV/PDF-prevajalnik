import { apiBaseUrl } from "./pdf-client";
import type { BBox } from "./pdf-types";
import type { TranslationJob } from "./translation-types";

export type PdfExportMode = "diagnostic" | "replacement_preview";

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
  };
  replacements: PdfExportReplacement[];
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
  form.append("translation_job", JSON.stringify(job));
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
