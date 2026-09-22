import { apiBaseUrl } from "./pdf-client";
import type { PersistentGlossaryMode } from "./persistent-glossary";

export type CsvColumnMapping = {
  source_term: string;
  target_term: string;
  source_language?: string;
  target_language?: string;
  domain?: string;
  match_mode?: string;
  external_record_id?: string;
  confidence?: string;
  definition?: string;
  context?: string;
  note?: string;
  source_version?: string;
};

export type TerminologyImportOptions = {
  source_language: string;
  target_language: string;
  default_domain: string;
  default_match_mode: PersistentGlossaryMode;
  external_source: string;
  source_version?: string;
  license_info?: string;
  csv_mapping?: CsvColumnMapping;
};

export type TerminologyCandidate = {
  candidate_id: string;
  row_number: number;
  source_term: string;
  target_term: string;
  source_language: string;
  target_language: string;
  domain: string;
  match_mode: PersistentGlossaryMode;
  external_source: string;
  external_record_id: string | null;
  source_confidence: string | null;
  definition: string | null;
  context: string | null;
  note: string;
  source_version: string | null;
  license_info: string | null;
  classification: "new" | "duplicate" | "conflict" | "invalid";
  existing_entry_ids: string[];
};

export type TerminologyImportPreview = {
  preview_id: string;
  import_batch_id: string;
  expires_at: string;
  format: "csv" | "tbx";
  file_sha256: string;
  candidates: TerminologyCandidate[];
  valid_count: number;
  new_count: number;
  duplicate_count: number;
  conflict_count: number;
  invalid_count: number;
  missing_translation_count: number;
  missing_language_count: number;
  issues: { row_number: number; kind: string; message: string; candidate_id: string | null; existing_entry_ids: string[] }[];
  domains: string[];
  ignored_fields: string[];
  external_source: string;
  source_version: string | null;
  license_info: string | null;
};

export async function previewTerminologyImport(
  file: File,
  format: "csv" | "tbx",
  options: TerminologyImportOptions,
): Promise<TerminologyImportPreview> {
  if (file.size > 5 * 1024 * 1024) throw new Error("Datoteka presega omejitev 5 MB.");
  const form = new FormData();
  form.append("file", file);
  form.append("import_format", format);
  form.append("options_json", JSON.stringify(options));
  return request("/api/glossary/imports/terminology/preview", { method: "POST", body: form });
}

export async function confirmTerminologyImport(
  preview: TerminologyImportPreview,
  candidateIds: string[],
): Promise<{ import_batch_id: string; created: number; skipped_duplicates: number; skipped_conflicts: number; skipped_unselected: number }> {
  return request("/api/glossary/imports/terminology/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preview_id: preview.preview_id, file_sha256: preview.file_sha256, candidate_ids: candidateIds }),
  });
}

export async function readCsvHeaders(file: File): Promise<string[]> {
  const firstChunk = await file.slice(0, 64 * 1024).text();
  const line = firstChunk.replace(/^\uFEFF/, "").split(/\r?\n/, 1)[0] ?? "";
  return parseCsvLine(line);
}

function parseCsvLine(line: string): string[] {
  const values: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"') {
      if (quoted && line[index + 1] === '"') { current += '"'; index += 1; }
      else quoted = !quoted;
    } else if (character === "," && !quoted) {
      values.push(current.trim()); current = "";
    } else current += character;
  }
  values.push(current.trim());
  return values.filter(Boolean);
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(new URL(path, `${apiBaseUrl}/`), init);
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail ?? "Terminološkega uvoza ni bilo mogoče izvesti.");
  }
  return response.json() as Promise<T>;
}
