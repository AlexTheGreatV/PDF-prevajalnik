import { apiBaseUrl } from "./pdf-client";
import type { BBox, TableGeometryEvidence, TableOwnership } from "./pdf-types";
import type { TranslationJob, TranslationSegment } from "./translation-types";
import { isTranslationUnitSegment } from "./translation-units";
import { exportTranslationJob } from "./translation-job";

export class PdfExportQcError extends Error {
  constructor(public readonly blockers: TranslationSegment[]) {
    super(`Aktualni QC blokira celoten PDF izvoz: ${blockers.map((segment) => segment.segment_id).join(", ")}. Geometrijski načrt še ni izdelan; druge enote niso bile geometrijsko zavrnjene.`);
    this.name = "PdfExportQcError";
  }
}

export type PdfExportMode = "diagnostic" | "replacement_preview";
export type PdfTypographyProfile = "original" | "document-role-typography-pilot-v1";
export function pdfTypographyRevisionKey(base: string, profile: PdfTypographyProfile): string {
  return JSON.stringify([base, profile]);
}
export const FINAL_PDF_EXPORT_MODE: PdfExportMode = "replacement_preview";
export type RevisionBoundValue<T> = { revisionKey: string; value: T } | null;

export function currentRevisionValue<T>(state: RevisionBoundValue<T>, revisionKey: string): T | null {
  return state?.revisionKey === revisionKey ? state.value : null;
}

export function isCurrentPdfExportRevision(requestRevision: string, currentRevision: string): boolean {
  return requestRevision === currentRevision;
}
export type ImageOverlapKind = "none" | "simple_background" | "complex_background" | "image_only_text" | "uncertain";
export type PdfExportResolution =
  | "replacement_planned"
  | "preserved_identical"
  | "not_applicable"
  | "blocked_overflow"
  | "blocked_image_safety"
  | "blocked_vector_safety"
  | "blocked_overlap_conflict"
  | "blocked_table_structure"
  | "blocked_ocr_safety"
  | "blocked_other";

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
  resolution: PdfExportResolution;
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
  list_marker_preserved: boolean;
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
  technical_value: boolean;
  cell_id: string | null;
  source_span_ids: string[];
  geometry_evidence: TableGeometryEvidence | null;
  proven_edges: string[];
  ownership: TableOwnership | null;
  provenance_block_reason: string | null;
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
  typography_revision?: string | null;
  typography_coverage?: {
    profile: string; translated: number; source_formatted: number; untouched: number;
    groups: { group: string; role: string; pages: number[]; total: number; unified: number;
      partial: boolean; excluded_reasons: Record<string, number> }[];
  } | null;
  schema_version: 2;
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
  resolution_summary: {
    total_segments: number;
    replacement_planned: number;
    preserved_identical: number;
    not_applicable: number;
    blocked_overflow: number;
    blocked_image_safety: number;
    blocked_vector_safety: number;
    blocked_overlap_conflict: number;
    blocked_table_structure: number;
    blocked_ocr_safety: number;
    blocked_other: number;
    blocked_total: number;
    resolved_if_exported: number;
  };
};

export async function preparePdfExport(file: File, job: TranslationJob, mode: PdfExportMode, profile: PdfTypographyProfile = "original"): Promise<PdfExportPlan> {
  const response = await postExportRequest("/api/pdf/export-translated/plan", file, job, mode, profile);
  if (!response.ok) throw new Error(await exportError(response));
  return parsePdfExportPlan(await response.json());
}

export function parsePdfExportPlan(value: unknown): PdfExportPlan {
  if (!isRecord(value)) throw new Error("Strežnik je vrnil neveljaven načrt PDF izvoza.");
  if (value.typography_coverage !== undefined && value.typography_coverage !== null) {
    const coverage = value.typography_coverage;
    if (!isRecord(coverage) || coverage.profile !== "document-role-typography-pilot-v1"
        || typeof value.typography_revision !== "string" || !/^[a-f0-9]{64}$/.test(value.typography_revision)
        || ![coverage.translated, coverage.source_formatted, coverage.untouched].every(v => Number.isSafeInteger(v) && Number(v) >= 0)
        || !Array.isArray(coverage.groups) || !coverage.groups.every(group => isRecord(group)
          && typeof group.group === "string" && typeof group.role === "string"
          && Array.isArray(group.pages) && group.pages.every(page => Number.isSafeInteger(page) && page > 0)
          && Number.isSafeInteger(group.total) && Number.isSafeInteger(group.unified)
          && Number(group.unified) >= 0 && Number(group.unified) <= Number(group.total)
          && group.partial === (group.unified !== group.total) && isRecord(group.excluded_reasons)
          && Object.values(group.excluded_reasons).every(count => Number.isSafeInteger(count) && Number(count) >= 0))) {
      throw new Error("Strežnik je vrnil neveljavno tipografsko pokritost.");
    }
  }
  validatePlanEnvelope(value);
  const version = value.schema_version;
  if (version === 2) {
    const replacements = validateCurrentReplacements(value.replacements);
    const ocrReplacements = validateCurrentReplacements(value.ocr_replacements);
    validateResolutionSummary(value.resolution_summary, [...replacements, ...ocrReplacements]);
    return value as PdfExportPlan;
  }
  if (version !== undefined && version !== 1) {
    throw new Error("Različica načrta PDF izvoza ni podprta.");
  }

  const replacements = normalizeLegacyReplacements(value.replacements);
  const ocrReplacements = normalizeLegacyReplacements(value.ocr_replacements);
  const all = [...replacements, ...ocrReplacements];
  const replacementPlanned = all.filter((item) => item.resolution === "replacement_planned").length;
  const blockedOther = all.length - replacementPlanned;
  return {
    ...value,
    schema_version: 2,
    replacements,
    ocr_replacements: ocrReplacements,
    resolution_summary: {
      total_segments: all.length,
      replacement_planned: replacementPlanned,
      preserved_identical: 0,
      not_applicable: 0,
      blocked_overflow: 0,
      blocked_image_safety: 0,
      blocked_vector_safety: 0,
      blocked_overlap_conflict: 0,
      blocked_table_structure: 0,
      blocked_ocr_safety: 0,
      blocked_other: blockedOther,
      blocked_total: blockedOther,
      resolved_if_exported: replacementPlanned,
    },
  } as PdfExportPlan;
}

function normalizeLegacyReplacements(value: unknown): PdfExportReplacement[] {
  if (!Array.isArray(value)) throw new Error("Stari načrt PDF izvoza nima veljavnega seznama zamenjav.");
  return value.map((item) => {
    if (
      !isRecord(item)
      || typeof item.segment_id !== "string"
      || (item.eligible !== undefined && typeof item.eligible !== "boolean")
    ) {
      throw new Error("Stari načrt PDF izvoza vsebuje neveljavno zamenjavo.");
    }
    const eligible = item.eligible === true;
    return {
      ...item,
      eligible,
      resolution: eligible ? "replacement_planned" : "blocked_other",
      list_marker_preserved: false,
    } as PdfExportReplacement;
  });
}

const RESOLUTIONS: PdfExportResolution[] = [
  "replacement_planned", "preserved_identical", "not_applicable", "blocked_overflow",
  "blocked_image_safety", "blocked_vector_safety", "blocked_overlap_conflict",
  "blocked_table_structure", "blocked_ocr_safety", "blocked_other",
];

function validatePlanEnvelope(value: Record<string, unknown>): void {
  if (
    typeof value.filename !== "string"
    || (value.mode !== "diagnostic" && value.mode !== "replacement_preview")
    || typeof value.document_sha256 !== "string"
    || !isRecord(value.summary)
    || !isRecord(value.table_summary)
    || !Array.isArray(value.tables)
    || !isRecord(value.ocr_summary)
  ) {
    throw new Error("Strežnik je vrnil nepopoln načrt PDF izvoza.");
  }
}

function validateCurrentReplacements(value: unknown): PdfExportReplacement[] {
  if (!Array.isArray(value)) throw new Error("Načrt PDF izvoza nima veljavnega seznama zamenjav.");
  return value.map((item) => {
    if (
      !isRecord(item)
      || typeof item.segment_id !== "string"
      || typeof item.eligible !== "boolean"
      || typeof item.resolution !== "string"
      || !RESOLUTIONS.includes(item.resolution as PdfExportResolution)
      || item.eligible !== (item.resolution === "replacement_planned")
    ) {
      throw new Error("Načrt PDF izvoza vsebuje nedosledno zamenjavo.");
    }
    return item as PdfExportReplacement;
  });
}

function validateResolutionSummary(value: unknown, replacements: PdfExportReplacement[]): void {
  if (!isRecord(value)) throw new Error("Načrt PDF izvoza nima povzetka rezultatov.");
  const counts = Object.fromEntries(RESOLUTIONS.map((resolution) => [resolution, 0])) as Record<PdfExportResolution, number>;
  for (const replacement of replacements) counts[replacement.resolution] += 1;
  const blockedTotal = RESOLUTIONS
    .filter((resolution) => resolution.startsWith("blocked_"))
    .reduce((total, resolution) => total + counts[resolution], 0);
  const expected: Record<string, number> = {
    total_segments: replacements.length,
    ...counts,
    blocked_total: blockedTotal,
    resolved_if_exported: counts.replacement_planned + counts.preserved_identical,
  };
  for (const [key, expectedValue] of Object.entries(expected)) {
    if (!Number.isInteger(value[key]) || value[key] !== expectedValue) {
      throw new Error("Povzetek načrta PDF izvoza se ne ujema s posameznimi segmenti.");
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export type PdfExportDownload = {
  typography_profile?: PdfTypographyProfile;
  typography_revision?: string;
  source_formatted?: number;
  blob: Blob;
  filename: string;
  mode: PdfExportMode;
  applied_replacements: number;
};

export function validatePdfTypographyDownload(result: Pick<PdfExportDownload, "typography_revision" | "source_formatted">,
  profile: PdfTypographyProfile, plan: PdfExportPlan | null): void {
  if (profile !== "original" && (!plan?.typography_revision || result.typography_revision !== plan.typography_revision
      || result.source_formatted !== plan.typography_coverage?.source_formatted)) {
    throw new Error("Tipografski načrt ni več aktualen. Znova preverite izvoz.");
  }
}

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

export async function downloadPdfExport(file: File, job: TranslationJob, mode: PdfExportMode, profile: PdfTypographyProfile = "original"): Promise<PdfExportDownload> {
  const response = await postExportRequest("/api/pdf/export-translated", file, job, mode, profile);
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
  const revision = response.headers.get("x-pdf-typography-revision");
  const formatted = response.headers.get("x-pdf-source-formatted");
  if (profile !== "original" && (response.headers.get("x-pdf-typography-profile") !== profile
      || !revision || !/^[a-f0-9]{64}$/.test(revision) || formatted === null || !/^\d+$/.test(formatted))) {
    throw new Error("Strežnik ni potrdil izbranega tipografskega profila.");
  }
  return {
    typography_profile: profile,
    typography_revision: revision ?? undefined,
    source_formatted: formatted === null ? 0 : Number(formatted),
    blob: await response.blob(),
    filename,
    mode: responseMode,
    applied_replacements: Number(applied),
  };
}

function postExportRequest(path: string, file: File, job: TranslationJob, mode: PdfExportMode, profile: PdfTypographyProfile): Promise<Response> {
  const form = createPdfExportForm(file, job, mode, profile);
  form.append("mode", mode);
  return fetch(`${apiBaseUrl}${path}`, { method: "POST", body: form });
}

export function createPdfExportForm(file: File, job: TranslationJob, mode: PdfExportMode, profile: PdfTypographyProfile): FormData {
  const currentJob = JSON.parse(exportTranslationJob(job)) as TranslationJob;
  const blocked = currentJob.segments.filter((segment) => segment.status === "reviewed" && segment.current_qc?.status === "blocked");
  if (mode === FINAL_PDF_EXPORT_MODE && blocked.length) {
    throw new PdfExportQcError(blocked);
  }
  const form = new FormData();
  form.append("file", file);
  form.append("translation_job", JSON.stringify({
    ...currentJob,
    typography_profile: profile,
    segments: currentJob.segments.filter(isTranslationUnitSegment),
  }));
  return form;
}

export async function exportError(response: Response): Promise<string> {
  try {
    const payload = await response.json() as { detail?: string | { message?: string } };
    if (typeof payload.detail === "string") return payload.detail;
    if (payload.detail?.message) return payload.detail.message;
  } catch {
    // The API may return a non-JSON proxy error.
  }
  return response.statusText || "Izvoz PDF ni uspel.";
}
