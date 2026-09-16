import type { BBox, PdfKind, StructuredBlockKind, StructuredStyle } from "./pdf-types";
import type { TechnicalRange, TechnicalRangeKind } from "./technical-ranges";

export type TranslationStatus = "pending" | "translating" | "translated" | "reviewed" | "failed" | "skipped";
export type TranslationSourceOrigin = "native" | "accepted_ocr";
export type AcceptedOcrSource = {
  revision_id?: string | null;
  review_id: string;
  accepted_text: string;
  bbox: BBox;
  source: "block" | "page" | "image" | "manual";
  reason: string;
};
export type TranslationOrigin = "glossary" | "provider" | "manual" | null;
export type GlossaryMode = "exact_segment" | "do_not_translate" | "preferred" | "locked_inline";
export type GlossaryOrigin = "built_in" | "user";
export type PlaceholderStrategy = "primary_tk" | "retry_phx";

export type ProtectedToken = {
  placeholder: string;
  value: string;
  replacement_value?: string;
  kind: "url" | "email" | "measurement" | "number" | "product_code" | "technical_term" | "technical_range" | "technical_unit_boundary" | "glossary";
  technical_range?: TechnicalRange;
  unit_start_placeholder?: string;
  unit_end_placeholder?: string;
  rule_id?: "technical_range" | "technical_unit_spacing";
  rule_version?: string;
};

export type TranslationRuleAdjustment = {
  rule_id: "technical_range" | "technical_unit_spacing";
  rule_version: string;
  kind: TechnicalRangeKind | "unit_spacing";
  source_text: string;
  replacement_text: string;
};

export type GlossaryEntry = {
  id: string;
  source_term: string;
  target_term: string;
  case_sensitive: boolean;
  mode: GlossaryMode;
  enabled: boolean;
  origin: GlossaryOrigin;
  default_target_term?: string;
};

export type GlossaryMatch = {
  glossary_id: string;
  source_term: string;
  target_term: string;
  matched_text: string;
  mode: GlossaryMode;
  origin: GlossaryOrigin;
};

export type PlaceholderAttempt = {
  strategy: PlaceholderStrategy;
  provider_text: string | null;
  valid: boolean;
  error: string | null;
};

export type TranslationChunk = {
  chunk_id: string;
  segment_id: string;
  source_text: string;
  source_start: number;
  source_end: number;
  separator_after: string;
  source_token_count: number | null;
  translated_text: string;
  status: "pending" | "translated" | "failed";
  warnings: string[];
  placeholder_attempts: PlaceholderAttempt[];
  decoding: {
    max_decoding_length: number;
    output_token_count: number;
    reached_limit: boolean;
    has_end_token: boolean | null;
    beam_size: number;
  } | null;
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
  source_bbox: BBox | null;
  source_page_type: PdfKind | null;
  source_rotation: number | null;
  source_style: StructuredStyle | null;
  accepted_ocr_source?: AcceptedOcrSource | null;
  translated_text: string;
  edited_translation: string;
  status: TranslationStatus;
  quality: number | null;
  error: string | null;
  skip_reason: string | null;
  protected_tokens: ProtectedToken[];
  glossary_matches: GlossaryMatch[];
  translation_origin: TranslationOrigin;
  glossary_entry_id: string | null;
  model_id: string | null;
  provider_id: string | null;
  placeholder_attempts: PlaceholderAttempt[];
  needs_review: boolean;
  warnings: string[];
  chunks: TranslationChunk[];
  forced_chunking: boolean;
  translation_rules_version?: string | null;
  translation_rule_adjustments?: TranslationRuleAdjustment[];
  created_at: string;
};

export type TranslationJob = {
  schema_version: 4;
  source_filename: string;
  source_language: string;
  target_language: string;
  created_at: string;
  document_identity: PdfDocumentIdentity | null;
  glossary: GlossaryEntry[];
  glossary_snapshot: GlossaryEntry[];
  segments: TranslationSegment[];
};

export type PdfPageIdentity = {
  page_number: number;
  width: number;
  height: number;
  rotation: number;
};

export type PdfDocumentIdentity = {
  sha256: string;
  page_count: number;
  document_model_version: number;
  pages: PdfPageIdentity[];
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
  provider_id?: string | null;
  model_id?: string | null;
  source_token_count?: number | null;
  output_token_count?: number | null;
  max_decoding_length?: number | null;
  reached_decoding_limit?: boolean;
  has_end_token?: boolean | null;
  beam_size?: number | null;
};

export interface TranslationProvider {
  readonly id: string;
  translateBatch(
    requests: TranslationRequest[],
    options: {
      source_language: string;
      target_language: string;
      signal?: AbortSignal;
      onProgress?: (completed: number, total: number) => void;
    },
  ): Promise<TranslationProviderResult[]>;
}
