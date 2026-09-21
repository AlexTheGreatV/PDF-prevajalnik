import { apiBaseUrl } from "./pdf-client";
import type { TranslationJob, TranslationSegment } from "./translation-types";

export type TranslationMemoryStatus = "proposed" | "approved" | "rejected" | "archived";
export type TranslationMemoryEntry = {
  id: string;
  source_text: string;
  target_text: string;
  normalized_source: string;
  normalized_source_sha256: string;
  source_language: string;
  target_language: string;
  domain: string;
  status: TranslationMemoryStatus;
  origin: "manual_review";
  source_document_sha256: string | null;
  source_segment_id: string | null;
  source_kind: "native" | "accepted_ocr";
  note: string;
  revision: number;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
  usage_count: number;
  last_used_at: string | null;
};

export type TranslationMemoryDraft = Pick<TranslationMemoryEntry,
  "source_text" | "target_text" | "source_language" | "target_language" | "domain" |
  "origin" | "source_document_sha256" | "source_segment_id" | "source_kind" | "note"
> & { status: "proposed" };

export type TranslationMemoryListResponse = { entries: TranslationMemoryEntry[]; total: number; active_set_hash: string };
export type TranslationMemoryLoadState =
  | { status: "loading" }
  | { status: "ready"; entries: TranslationMemoryEntry[]; active_set_hash: string }
  | { status: "error"; message: string }
  | { status: "explicitly_skipped" };

type RequiredResourceLoadStatus = "loading" | "ready" | "error" | "explicitly_skipped";

export function canInitializeTranslationJob(
  documentReady: boolean,
  glossaryStatus: RequiredResourceLoadStatus,
  translationMemoryStatus: RequiredResourceLoadStatus,
): boolean {
  const settled = (status: RequiredResourceLoadStatus) => status === "ready" || status === "explicitly_skipped";
  return documentReady && settled(glossaryStatus) && settled(translationMemoryStatus);
}

export function createLatestTranslationMemoryRequestCoordinator() {
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

export function normalizeTranslationMemorySource(value: string): string {
  return value.normalize("NFC").replace(/\r\n?/g, "\n").trim();
}

export function selectExactTranslationMemoryMatch(
  sourceText: string,
  sourceLanguage: string,
  targetLanguage: string,
  domain: string,
  entries: TranslationMemoryEntry[],
): { status: "none" | "match" | "conflict"; match: TranslationMemoryEntry | null; candidates: TranslationMemoryEntry[] } {
  const normalized = normalizeTranslationMemorySource(sourceText);
  const eligible = entries.filter((entry) => entry.status === "approved" &&
    entry.source_language === sourceLanguage && entry.target_language === targetLanguage &&
    entry.normalized_source === normalized && (entry.domain === domain || entry.domain === ""));
  const specific = domain ? eligible.filter((entry) => entry.domain === domain) : [];
  const candidates = specific.length ? specific : eligible.filter((entry) => entry.domain === "");
  if (!candidates.length) return { status: "none", match: null, candidates: [] };
  if (new Set(candidates.map((entry) => entry.target_text)).size > 1) return { status: "conflict", match: null, candidates };
  return { status: "match", match: candidates[0], candidates };
}

export async function listTranslationMemoryEntries(status: TranslationMemoryStatus | "" = ""): Promise<TranslationMemoryListResponse> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return request(`/api/translation-memory/entries${query}`);
}

export async function createTranslationMemoryEntry(draft: TranslationMemoryDraft): Promise<TranslationMemoryEntry> {
  return request("/api/translation-memory/entries", { method: "POST", body: JSON.stringify(draft) });
}

export async function transitionTranslationMemoryEntry(entry: TranslationMemoryEntry, action: "approve" | "reject" | "archive" | "restore"): Promise<TranslationMemoryEntry> {
  return request(`/api/translation-memory/entries/${entry.id}/${action}`, {
    method: "POST", body: JSON.stringify({ expected_revision: entry.revision }),
  });
}

export async function getTranslationMemoryHistory(entryId: string): Promise<Array<{ action: string; revision: number; created_at: string }>> {
  return request(`/api/translation-memory/entries/${entryId}/history`);
}

export async function recordTranslationMemoryUsage(entryIds: string[]): Promise<void> {
  const unique = [...new Set(entryIds)];
  if (!unique.length) return;
  await request<void>("/api/translation-memory/usage", { method: "POST", body: JSON.stringify({ entry_ids: unique }) });
}

export function createTranslationMemoryDraft(segment: TranslationSegment, job: TranslationJob, domain = ""): TranslationMemoryDraft {
  if (segment.status !== "reviewed" || segment.translation_origin !== "manual" || !segment.edited_translation.trim()) {
    throw new Error("Samo ročno popravljen in potrjen prevod je mogoče predlagati za prevajalski spomin.");
  }
  return {
    source_text: segment.source_text,
    target_text: segment.edited_translation,
    source_language: segment.source_language === "auto" ? "en" : segment.source_language,
    target_language: segment.target_language,
    domain,
    status: "proposed",
    origin: "manual_review",
    source_document_sha256: job.document_identity?.sha256 ?? null,
    source_segment_id: segment.segment_id,
    source_kind: segment.source_text_origin,
    note: "",
  };
}

export function isTranslationMemorySnapshotStale(job: TranslationJob, activeSetHash: string): boolean {
  return Boolean(job.translation_memory_set_hash && activeSetHash && job.translation_memory_set_hash !== activeSetHash);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(new URL(path, `${apiBaseUrl}/`), {
      ...init,
      headers: init.body ? { "content-type": "application/json", ...init.headers } : init.headers,
    });
  } catch (error) {
    throw new Error("Povezava z lokalnim prevajalskim spominom ni uspela.", { cause: error });
  }
  if (response.ok) return (response.status === 204 ? undefined : response.json()) as Promise<T>;
  let message = "Zahteva prevajalskemu spominu ni uspela.";
  try {
    const payload = await response.json() as { detail?: string | { message?: string } };
    message = typeof payload.detail === "string" ? payload.detail : payload.detail?.message ?? message;
  } catch { /* safe fallback */ }
  throw new Error(message);
}
