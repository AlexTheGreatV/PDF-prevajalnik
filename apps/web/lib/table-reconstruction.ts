import { apiBaseUrl } from "./pdf-client";
import { createPdfExportForm, exportError, parsePdfExportPlan, type PdfTypographyProfile, type PdfExportPlan } from "./pdf-export-client";
import type { TranslationJob } from "./translation-types";

export type ReconstructionSelection = { page_number: number; table_id: string };
export type ReconstructionCounts = {
  ordinary_replacements: number; reconstructed_cells: number; reconstructed_translations: number;
  reconstructed_identical: number; reconstructed_technical: number; translated_units: number;
  preserved_identical: number; not_applicable: number; blocked: number; total_units: number;
};
export type ReconstructionPreview = {
  ready: boolean; token: string | null; source_preview: string; table_preview: string | null;
  row_count: number; cell_count: number; issues: string[];
  counts?: ReconstructionCounts; ordinary_plan?: PdfExportPlan;
};

export function reconstructionRevision(base: string, selection: ReconstructionSelection | null): string {
  return JSON.stringify([base, selection?.page_number ?? null, selection?.table_id ?? null]);
}

export function validateReconstructionCounts(value: unknown): ReconstructionCounts {
  if (!value || typeof value !== "object") throw new Error("Manjkajo preverjeni števci rekonstrukcije.");
  const c = value as ReconstructionCounts;
  const keys: (keyof ReconstructionCounts)[] = ["ordinary_replacements", "reconstructed_cells", "reconstructed_translations",
    "reconstructed_identical", "reconstructed_technical", "translated_units", "preserved_identical", "not_applicable", "blocked", "total_units"];
  if (!keys.every(key => Number.isSafeInteger(c[key]) && c[key] >= 0)
    || c.reconstructed_cells !== c.reconstructed_translations + c.reconstructed_identical + c.reconstructed_technical
    || c.translated_units !== c.ordinary_replacements + c.reconstructed_translations
    || c.total_units !== c.translated_units + c.preserved_identical + c.not_applicable + c.blocked) {
    throw new Error("Števci rekonstrukcije se ne ujemajo; prenos ni potrjen.");
  }
  return c;
}

async function request(path: string, file: File, job: TranslationJob, profile: PdfTypographyProfile,
  selection: ReconstructionSelection, token?: string): Promise<Response> {
  const form = createPdfExportForm(file, job, "replacement_preview", profile);
  form.append("selection", JSON.stringify({ page_number: selection.page_number, table_id: selection.table_id,
    ...(token ? { token } : {}) }));
  const response = await fetch(`${apiBaseUrl}/api/pdf/export-translated/reconstruction/${path}`, { method: "POST", body: form });
  if (!response.ok) throw new Error(await exportError(response));
  return response;
}

export async function prepareReconstruction(file: File, job: TranslationJob, profile: PdfTypographyProfile,
  selection: ReconstructionSelection): Promise<ReconstructionPreview> {
  const response = await request("preview", file, job, profile, selection);
  const preview = await response.json() as ReconstructionPreview;
  if (typeof preview.ready !== "boolean" || !Number.isSafeInteger(preview.row_count) || preview.row_count < 1
    || !Number.isSafeInteger(preview.cell_count) || preview.cell_count < 1 || !Array.isArray(preview.issues)
    || !preview.issues.every(issue => typeof issue === "string") || !preview.source_preview?.startsWith("data:image/png;base64,")) {
    throw new Error("Neveljaven predogled rekonstrukcije.");
  }
  if (preview.ready) {
    if (!preview.token || !preview.table_preview?.startsWith("data:image/png;base64,")) throw new Error("Nepopoln predogled rekonstrukcije.");
    validateReconstructionCounts(preview.counts);
    preview.ordinary_plan = parsePdfExportPlan(preview.ordinary_plan);
  }
  return preview;
}

export function validateReconstructionDownload(response: Response, preview: ReconstructionPreview): string {
  if (!preview.ready || !preview.token || response.headers.get("x-pdf-reconstruction-token") !== preview.token
    || response.headers.get("x-pdf-export-mode") !== "replacement_preview"
    || response.headers.get("content-type")?.split(";", 1)[0] !== "application/pdf") {
    throw new Error("Strežnik ni potrdil aktualnega rekonstruiranega PDF-ja.");
  }
  const actual = validateReconstructionCounts(JSON.parse(response.headers.get("x-pdf-reconstruction-counts") ?? "null"));
  const expected = validateReconstructionCounts(preview.counts);
  if (Object.keys(expected).some(key => actual[key as keyof ReconstructionCounts] !== expected[key as keyof ReconstructionCounts])) {
    throw new Error("Izvedene zamenjave ne ustrezajo predogledu.");
  }
  const encoded = response.headers.get("content-disposition")?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded) : "";
  if (!filename.endsWith(".pdf") || /[\\/\u0000-\u001f\u007f]/.test(filename)) throw new Error("Neveljavno ime PDF-ja.");
  return filename;
}

export async function downloadReconstruction(file: File, job: TranslationJob, profile: PdfTypographyProfile,
  selection: ReconstructionSelection, preview: ReconstructionPreview): Promise<{ filename: string; blob: Blob }> {
  const response = await request("download", file, job, profile, selection, preview.token ?? undefined);
  const filename = validateReconstructionDownload(response, preview);
  const blob = await response.blob();
  if (await blob.slice(0, 5).text() !== "%PDF-") throw new Error("Odgovor nima veljavne PDF glave.");
  return { filename, blob };
}
