import type { StructuredBlockKind } from "./pdf-types";

export type TranslationStatus = "pending" | "translating" | "translated" | "reviewed" | "failed" | "skipped";
export type TranslationSourceOrigin = "native" | "accepted_ocr";

export type ProtectedToken = {
  placeholder: string;
  value: string;
  kind: "url" | "email" | "measurement" | "number" | "product_code" | "technical_term" | "glossary";
};

export type GlossaryEntry = {
  id: string;
  source_term: string;
  target_term: string;
  case_sensitive: boolean;
  exact_match: boolean;
  do_not_translate: boolean;
};

export type GlossaryMatch = {
  glossary_id: string;
  source_term: string;
  target_term: string;
  matched_text: string;
  do_not_translate: boolean;
};

export type TranslationSegment = {
  segment_id: string;
  page_number: number;
  block_id: string;
  block_type: StructuredBlockKind | "table_cell";
  source_language: string;
  target_language: string;
  source_text: string;
  source_text_origin: TranslationSourceOrigin;
  translated_text: string;
  edited_translation: string;
  status: TranslationStatus;
  quality: number | null;
  error: string | null;
  skip_reason: string | null;
  protected_tokens: ProtectedToken[];
  glossary_matches: GlossaryMatch[];
  created_at: string;
};

export type TranslationJob = {
  schema_version: 1;
  source_filename: string;
  source_language: string;
  target_language: string;
  created_at: string;
  glossary: GlossaryEntry[];
  segments: TranslationSegment[];
};

export type TranslationContext = {
  segment_id: string;
  page_number: number;
  block_type: TranslationSegment["block_type"];
  glossary_matches: GlossaryMatch[];
};

export type TranslationRequest = {
  segment_id: string;
  text: string;
  context: TranslationContext;
};

export type TranslationProviderResult = {
  segment_id: string;
  translated_text: string | null;
  confidence: number | null;
  error: { code: string; message: string; retryable: boolean } | null;
};

export interface TranslationProvider {
  readonly id: string;
  translateBatch(
    requests: TranslationRequest[],
    options: { source_language: string; target_language: string; signal?: AbortSignal },
  ): Promise<TranslationProviderResult[]>;
}
