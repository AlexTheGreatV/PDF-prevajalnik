import { apiBaseUrl } from "./pdf-client";
import type { TranslationProvider, TranslationProviderResult, TranslationRequest } from "./translation-types";

const MAX_BATCH_SIZE = 32;
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

export type LocalTranslationStatus = {
  available: boolean;
  loaded: boolean;
  provider_id: string;
  model_id: string;
  device: string;
  compute_type: string;
  message: string | null;
  load_time_ms: number | null;
};

type LocalTranslationResponse = {
  provider_id: string;
  model_id: string;
  processing_time_ms: number;
  results: TranslationProviderResult[];
};

export function localTranslationUrl(path: string, baseUrl = apiBaseUrl): string {
  const base = new URL(baseUrl);
  if (!LOCAL_HOSTS.has(base.hostname)) {
    throw new Error("Lokalni prevajalnik je lahko povezan samo z lokalnim FastAPI strežnikom.");
  }
  return new URL(path, `${base.origin}/`).toString();
}

export async function getLocalTranslationStatus(signal?: AbortSignal): Promise<LocalTranslationStatus> {
  const response = await fetch(localTranslationUrl("/api/translation/providers/local/status"), { signal });
  return parseResponse<LocalTranslationStatus>(response);
}

export class LocalTranslationProvider implements TranslationProvider {
  readonly id = "local-opus-ctranslate2";

  async translateBatch(
    requests: TranslationRequest[],
    options: {
      source_language: string;
      target_language: string;
      signal?: AbortSignal;
      onProgress?: (completed: number, total: number) => void;
    },
  ): Promise<TranslationProviderResult[]> {
    const sourceLanguage = options.source_language === "auto" ? "en" : options.source_language;
    if (sourceLanguage !== "en" || options.target_language !== "sl") {
      throw new Error("Lokalni OPUS-MT prototip podpira samo prevod iz angleščine v slovenščino.");
    }
    const results: TranslationProviderResult[] = [];
    for (let offset = 0; offset < requests.length; offset += MAX_BATCH_SIZE) {
      if (options.signal?.aborted) throw new DOMException("Prevod je bil preklican.", "AbortError");
      const chunk = requests.slice(offset, offset + MAX_BATCH_SIZE);
      const response = await fetch(localTranslationUrl("/api/translation/providers/local/translate"), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          source_language: sourceLanguage,
          target_language: options.target_language,
          segments: chunk.map((request) => ({ segment_id: request.segment_id, text: request.text })),
        }),
        signal: options.signal,
      });
      const payload = await parseResponse<LocalTranslationResponse>(response);
      results.push(...payload.results);
      options.onProgress?.(results.length, requests.length);
    }
    const byId = new Map(results.map((result) => [result.segment_id, result]));
    return requests.map((request) => byId.get(request.segment_id) ?? {
      segment_id: request.segment_id,
      translated_text: null,
      confidence: null,
      error: { code: "missing_result", message: "Lokalni ponudnik ni vrnil rezultata.", retryable: true },
    });
  }
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.ok) return (await response.json()) as T;
  let message = "Zahteva lokalnemu prevajalniku ni uspela.";
  try {
    const payload = await response.json() as { detail?: string | { message?: string } };
    if (typeof payload.detail === "string") message = payload.detail;
    else if (payload.detail?.message) message = payload.detail.message;
  } catch {
    if (response.statusText) message = response.statusText;
  }
  throw new Error(message);
}
