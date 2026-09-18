import { apiBaseUrl } from "./pdf-client";
import type { GlossaryEntry } from "./translation-types";
import type { TranslationJob, TranslationSegment } from "./translation-types";

export type PersistentGlossaryMode = "exact_segment" | "preferred_term" | "do_not_translate";
export type PersistentGlossaryStatus = "proposed" | "approved" | "rejected" | "archived";
export type PersistentGlossaryOrigin = "manual" | "reviewed_correction" | "import" | "built_in";

export type PersistentGlossaryEntry = {
  id: string;
  source_term: string;
  target_term: string;
  source_language: string;
  target_language: string;
  domain: string;
  match_mode: PersistentGlossaryMode;
  case_sensitive: boolean;
  status: PersistentGlossaryStatus;
  origin: PersistentGlossaryOrigin;
  note: string;
  source_label: string | null;
  document_hash: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
  usage_count: number;
  last_used_at: string | null;
};

export type GlossaryEntryDraft = Pick<PersistentGlossaryEntry,
  "source_term" | "target_term" | "source_language" | "target_language" | "domain" |
  "match_mode" | "case_sensitive" | "status" | "origin" | "note" | "source_label" | "document_hash"
>;

export type GlossaryHistoryRecord = {
  history_id: string;
  entry_id: string;
  revision: number;
  action: "create" | "update" | "approve" | "reject" | "archive" | "restore" | "import";
  snapshot: PersistentGlossaryEntry;
  created_at: string;
};

export type GlossaryListResponse = {
  entries: PersistentGlossaryEntry[];
  total: number;
  active_set_hash: string;
};

export type GlossaryImportRow = GlossaryEntryDraft & { row_number: number };
export type GlossaryImportPreview = {
  valid_rows: GlossaryImportRow[];
  new_count: number;
  duplicate_count: number;
  conflict_count: number;
  invalid_count: number;
  override_count: number;
  issues: Array<{ row_number: number; kind: "duplicate" | "conflict" | "invalid" | "override"; message: string; existing_entry_id: string | null }>;
};

export type GlossaryFilters = {
  q?: string;
  source_language?: string;
  target_language?: string;
  domain?: string;
  status?: PersistentGlossaryStatus | "";
  match_mode?: PersistentGlossaryMode | "";
};

export type PersistentGlossaryLoadState =
  | { status: "loading" }
  | { status: "ready"; entries: GlossaryEntry[]; active_set_hash: string }
  | { status: "error"; message: string }
  | { status: "explicitly_skipped" };

export type LatestGlossaryRequestCoordinator = {
  run<T>(
    loader: () => Promise<T>,
    handlers: {
      onLoading: () => void;
      onReady: (value: T) => void;
      onError: (error: unknown) => void;
    },
  ): Promise<"applied" | "stale">;
  invalidate(): void;
};

export function createLatestGlossaryRequestCoordinator(): LatestGlossaryRequestCoordinator {
  let generation = 0;
  return {
    async run<T>(loader: () => Promise<T>, handlers: {
      onLoading: () => void;
      onReady: (value: T) => void;
      onError: (error: unknown) => void;
    }): Promise<"applied" | "stale"> {
      const requestGeneration = ++generation;
      handlers.onLoading();
      try {
        const value = await loader();
        if (requestGeneration !== generation) return "stale";
        handlers.onReady(value);
        return "applied";
      } catch (error) {
        if (requestGeneration !== generation) return "stale";
        handlers.onError(error);
        return "applied";
      }
    },
    invalidate() { generation += 1; },
  };
}

export function resolveGlossarySnapshot(
  documentEntries: GlossaryEntry[],
  state: PersistentGlossaryLoadState,
  builtInEntries: GlossaryEntry[],
): GlossaryEntry[] | null {
  if (state.status === "loading" || state.status === "error") return null;
  const persistentEntries = state.status === "ready" ? state.entries : [];
  return [...documentEntries, ...persistentEntries, ...builtInEntries].map((entry) => ({ ...entry }));
}

export const EMPTY_GLOSSARY_DRAFT: GlossaryEntryDraft = {
  source_term: "", target_term: "", source_language: "en", target_language: "sl", domain: "",
  match_mode: "preferred_term", case_sensitive: false, status: "proposed", origin: "manual",
  note: "", source_label: null, document_hash: null,
};

export async function getGlossaryStatus(): Promise<{ available: boolean; message: string | null; schema_version: number | null }> {
  return glossaryRequest("/api/glossary/status");
}

export async function listGlossaryEntries(filters: GlossaryFilters = {}): Promise<GlossaryListResponse> {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value) parameters.set(key, value);
  return glossaryRequest(`/api/glossary/entries${parameters.size ? `?${parameters}` : ""}`);
}

export async function createGlossaryEntry(draft: GlossaryEntryDraft): Promise<PersistentGlossaryEntry> {
  return glossaryRequest("/api/glossary/entries", { method: "POST", body: JSON.stringify(draft) });
}

export async function updateGlossaryEntry(entry: PersistentGlossaryEntry, changes: Partial<GlossaryEntryDraft>): Promise<PersistentGlossaryEntry> {
  return glossaryRequest(`/api/glossary/entries/${entry.id}`, {
    method: "PUT", body: JSON.stringify({ expected_revision: entry.revision, ...changes }),
  });
}

export async function transitionGlossaryEntry(
  entry: PersistentGlossaryEntry,
  action: "approve" | "reject" | "archive" | "restore",
): Promise<PersistentGlossaryEntry> {
  return glossaryRequest(`/api/glossary/entries/${entry.id}/${action}`, {
    method: "POST", body: JSON.stringify({ expected_revision: entry.revision }),
  });
}

export async function getGlossaryHistory(entryId: string): Promise<GlossaryHistoryRecord[]> {
  return glossaryRequest(`/api/glossary/entries/${entryId}/history`);
}

export async function recordGlossaryUsage(entryIds: string[]): Promise<void> {
  const uniqueIds = [...new Set(entryIds)];
  if (uniqueIds.length === 0) return;
  await glossaryRequest<void>("/api/glossary/usage", {
    method: "POST", body: JSON.stringify({ entry_ids: uniqueIds }),
  });
}

export async function previewGlossaryCsv(file: File): Promise<GlossaryImportPreview> {
  const form = new FormData();
  form.append("file", file);
  return glossaryRequest("/api/glossary/imports/csv/preview", { method: "POST", body: form }, false);
}

export async function confirmGlossaryCsv(preview: GlossaryImportPreview, overwriteConflicts: boolean): Promise<{ created: number; updated: number; skipped: number }> {
  return glossaryRequest("/api/glossary/imports/csv/confirm", {
    method: "POST", body: JSON.stringify({ rows: preview.valid_rows, overwrite_conflicts: overwriteConflicts }),
  });
}

export function glossaryExportUrl(format: "csv" | "json"): string {
  return new URL(`/api/glossary/export.${format}`, `${apiBaseUrl}/`).toString();
}

export function persistentToRuntimeEntry(entry: PersistentGlossaryEntry): GlossaryEntry {
  return {
    id: entry.id,
    source_term: entry.source_term,
    target_term: entry.target_term,
    case_sensitive: entry.case_sensitive,
    mode: entry.match_mode === "preferred_term" ? "preferred" : entry.match_mode,
    enabled: entry.status === "approved",
    origin: "user",
    scope: "persistent_user",
    revision: entry.revision,
    status: entry.status,
    source: entry.origin,
  };
}

export function createReviewedCorrectionDraft(segment: TranslationSegment, domain = ""): GlossaryEntryDraft {
  if (segment.status !== "reviewed" || segment.translation_origin !== "manual" || !segment.edited_translation.trim()) {
    throw new Error("Samo ročno popravljen in potrjen prevod je mogoče predlagati za glosar.");
  }
  return {
    ...EMPTY_GLOSSARY_DRAFT,
    source_term: segment.source_text,
    target_term: segment.edited_translation,
    source_language: segment.source_language === "auto" ? "en" : segment.source_language,
    target_language: segment.target_language,
    domain,
    origin: "reviewed_correction",
    status: "proposed",
  };
}

export function isGlossarySnapshotStale(job: TranslationJob, activeSetHash: string): boolean {
  return Boolean(job.glossary_set_hash && activeSetHash && job.glossary_set_hash !== activeSetHash);
}

async function glossaryRequest<T>(path: string, init: RequestInit = {}, json = true): Promise<T> {
  let response: Response;
  try {
    response = await fetch(new URL(path, `${apiBaseUrl}/`), {
      ...init,
      headers: json && init.body ? { "content-type": "application/json", ...init.headers } : init.headers,
    });
  } catch (error) {
    throw new Error("Povezava z lokalnim glosarjem ni uspela.", { cause: error });
  }
  if (response.ok) return (response.status === 204 ? undefined : response.json()) as Promise<T>;
  let message = "Zahteva glosarju ni uspela.";
  try {
    const payload = await response.json() as { detail?: string | { message?: string } };
    message = typeof payload.detail === "string" ? payload.detail : payload.detail?.message ?? message;
  } catch { /* Keep the safe fallback. */ }
  if (response.status === 409) message = `Konflikt revizije: ${message}`;
  throw new Error(message);
}
