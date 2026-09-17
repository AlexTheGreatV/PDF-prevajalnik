import { findExactSegmentGlossaryMatch, findGlossaryMatches, orderedGlossary } from "./glossary";
import type { OcrReviewItem } from "./ocr-review";
import type { PdfAnalysis, StructuredBlock, StructuredRegion } from "./pdf-types";
import { protectTechnicalTokens, restoreTechnicalTokens } from "./technical-tokens";
import { TECHNICAL_RANGE_RULES_VERSION } from "./technical-ranges";
import {
  createActiveTranslationProfileMetadata,
  refreshTranslationProfileMetadata,
} from "./translation-profile-evaluation";
import type {
  GlossaryEntry,
  PlaceholderAttempt,
  PlaceholderStrategy,
  ProtectedToken,
  TranslationChunk,
  TranslationJob,
  TranslationProvider,
  TranslationProviderResult,
  TranslationRequest,
  TranslationSegment,
} from "./translation-types";

export const TRANSLATION_JOB_SCHEMA_VERSION = 5 as const;

export function createTranslationJob(
  analysis: PdfAnalysis,
  ocrItems: OcrReviewItem[],
  options: { source_language: string; target_language: string; glossary?: GlossaryEntry[]; created_at?: string },
): TranslationJob {
  const created_at = options.created_at ?? new Date().toISOString();
  const glossary = cloneGlossary(options.glossary ?? []);
  const segments: TranslationSegment[] = [];
  const pages = [...(analysis.document?.pages ?? [])].sort((a, b) => a.page_number - b.page_number);

  for (const page of pages) {
    for (const region of [...page.regions].sort((a, b) => a.reading_order - b.reading_order)) {
      for (const block of [...region.blocks].sort((a, b) => a.reading_order - b.reading_order)) {
        const blockId = `p${page.page_number}-r${region.reading_order}-b${block.reading_order}`;
        if (block.kind === "table" && block.table_cells) {
          block.table_cells.forEach((row, rowIndex) => row.forEach((cell, columnIndex) => {
            segments.push(createSegment(cell, {
              segment_id: `${blockId}-c${rowIndex + 1}-${columnIndex + 1}`,
              block_id: blockId,
              block_type: "table_cell",
              page_number: page.page_number,
              page_type: page.page_type,
              page_rotation: page.rotation,
              region,
              block,
              source_origin: "native",
              source_language: options.source_language,
              target_language: options.target_language,
              glossary,
              created_at,
            }));
          }));
          continue;
        }
        const acceptedOcr = ocrItems.find((item) =>
          item.page_number === page.page_number &&
          item.linked_block_reading_order === block.reading_order &&
          item.status === "accepted" &&
          item.selected_text_source === "ocr" &&
          item.effective_text.trim(),
        );
        segments.push(createSegment(acceptedOcr?.effective_text ?? block.original_text, {
          segment_id: blockId,
          block_id: blockId,
          block_type: block.kind,
          page_number: page.page_number,
          page_type: page.page_type,
          page_rotation: page.rotation,
          region,
          block,
          source_origin: acceptedOcr ? "accepted_ocr" : "native",
          accepted_ocr_source: acceptedOcr ? acceptedOcrSource(acceptedOcr) : null,
          source_bbox: acceptedOcr?.pdf_bbox,
          source_language: options.source_language,
          target_language: options.target_language,
          glossary,
          created_at,
        }));
      }
    }
  }

  for (const item of ocrItems.filter(isAcceptedStandaloneOcr)) {
    const page = pages.find((candidate) => candidate.page_number === item.page_number);
    if (!page) continue;
    const region: StructuredRegion = {
      kind: "unknown", bbox: { ...item.pdf_bbox }, reading_order: Number.MAX_SAFE_INTEGER, blocks: [],
    };
    const block: StructuredBlock = {
      kind: "paragraph", bbox: { ...item.pdf_bbox }, reading_order: Number.MAX_SAFE_INTEGER,
      original_text: item.effective_text, normalized_text: item.effective_text, lines: [],
      line_boundaries: [], confidence: item.confidence ?? 0,
      style: { font: null, font_size: null, color: null, flags: 0, bold: false, italic: false },
      warnings: [], needs_ocr_check: false,
    };
    segments.push(createSegment(item.effective_text, {
      segment_id: `ocr-${item.id}`,
      block_id: `ocr-${item.id}`,
      block_type: "paragraph",
      page_number: page.page_number,
      page_type: page.page_type,
      page_rotation: page.rotation,
      region,
      block,
      source_origin: "accepted_ocr",
      accepted_ocr_source: acceptedOcrSource(item),
      source_language: options.source_language,
      target_language: options.target_language,
      glossary,
      created_at,
    }));
  }

  const job: TranslationJob = {
    schema_version: TRANSLATION_JOB_SCHEMA_VERSION,
    source_filename: analysis.filename,
    source_language: options.source_language,
    target_language: options.target_language,
    created_at,
    document_identity: {
      sha256: analysis.sha256,
      page_count: analysis.page_count,
      document_model_version: analysis.document_model_version,
      pages: analysis.pages.map((page) => ({
        page_number: page.page_number,
        width: page.dimensions.width,
        height: page.dimensions.height,
        rotation: page.rotation,
      })),
    },
    glossary,
    glossary_snapshot: cloneGlossary(glossary),
    segments,
    ...(options.target_language === "sl"
      ? createActiveTranslationProfileMetadata()
      : { translation_profile: null, quality_checklist: [] }),
  };
  return refreshTranslationProfileMetadata(job);
}

export function invalidateStaleOcrApprovals(job: TranslationJob, ocrItems: OcrReviewItem[]): TranslationJob {
  let changed = false;
  const items = new Map(ocrItems.map((item) => [item.id, item]));
  const segments = job.segments.map((segment) => {
    const provenance = segment.accepted_ocr_source;
    if (segment.source_text_origin !== "accepted_ocr" || !provenance) return segment;
    const current = items.get(provenance.review_id);
    const accepted = current?.status === "accepted" && current.selected_text_source === "ocr" && current.effective_text.trim();
    const next = accepted ? acceptedOcrSource(current) : null;
    if (next && JSON.stringify(next) === JSON.stringify(provenance)) return segment;
    changed = true;
    return {
      ...segment,
      source_text: next?.accepted_text ?? segment.source_text,
      source_bbox: next?.bbox ?? segment.source_bbox,
      accepted_ocr_source: next,
      status: segment.edited_translation.trim() ? "translated" as const : "pending" as const,
      needs_review: true,
      warnings: [...new Set([...segment.warnings, "ocr_source_changed_after_review"])],
    };
  });
  return changed ? refreshTranslationProfileMetadata({ ...job, segments }) : job;
}

type SegmentInput = {
  segment_id: string;
  block_id: string;
  block_type: TranslationSegment["block_type"];
  page_number: number;
  page_type: TranslationSegment["source_page_type"];
  page_rotation: number;
  region: StructuredRegion;
  block: StructuredBlock;
  source_origin: TranslationSegment["source_text_origin"];
  accepted_ocr_source?: TranslationSegment["accepted_ocr_source"];
  source_bbox?: TranslationSegment["source_bbox"];
  source_language: string;
  target_language: string;
  glossary: GlossaryEntry[];
  created_at: string;
};

function createSegment(sourceText: string, input: SegmentInput): TranslationSegment {
  const source_text = sourceText.trim();
  const skip_reason = getSkipReason(source_text, input.block_type, input.region.kind);
  const segment: TranslationSegment = {
    segment_id: input.segment_id,
    page_number: input.page_number,
    block_id: input.block_id,
    block_type: input.block_type,
    source_language: input.source_language,
    target_language: input.target_language,
    source_text,
    source_text_origin: input.source_origin,
    source_bbox: input.block_type === "table_cell" ? null : { ...(input.source_bbox ?? input.block.bbox) },
    source_page_type: input.page_type,
    source_rotation: input.page_rotation,
    source_style: input.block_type === "table_cell" ? null : { ...input.block.style },
    accepted_ocr_source: input.accepted_ocr_source ?? null,
    translated_text: "",
    edited_translation: "",
    status: skip_reason ? "skipped" : "pending",
    quality: null,
    error: null,
    skip_reason,
    protected_tokens: protectForTranslation(source_text, input.glossary).tokens,
    glossary_matches: findGlossaryMatches(source_text, input.glossary),
    translation_origin: null,
    glossary_entry_id: null,
    model_id: null,
    provider_id: null,
    placeholder_attempts: [],
    needs_review: false,
    warnings: [],
    chunks: [],
    forced_chunking: false,
    translation_rules_version: null,
    translation_rule_adjustments: [],
    created_at: input.created_at,
  };
  return skip_reason ? segment : applyExactSegmentGlossary(segment, input.glossary);
}

function isAcceptedStandaloneOcr(item: OcrReviewItem): boolean {
  return item.status === "accepted" && item.selected_text_source === "ocr" && item.effective_text.trim().length > 0 &&
    item.linked_block_reading_order === undefined && ["manual", "image", "page"].includes(item.source);
}

function acceptedOcrSource(item: OcrReviewItem): TranslationSegment["accepted_ocr_source"] {
  return {
    revision_id: item.server_revision_id ?? "",
    review_id: item.id,
    accepted_text: item.effective_text,
    bbox: { ...item.pdf_bbox },
    source: item.source,
    reason: item.reason,
  };
}

export function getSkipReason(
  text: string,
  blockType: TranslationSegment["block_type"],
  regionKind: StructuredRegion["kind"] = "body",
): string | null {
  const value = text.trim();
  if (!value) return "Prazno besedilo.";
  if (blockType === "list") return "Vsebnik seznama; prevajajo se posamezne alineje.";
  if (blockType === "table") return "Tabela brez strukturiranih celic.";
  if (!["heading", "paragraph", "list_item", "table_cell", "caption"].includes(blockType) && !["header", "footer"].includes(regionKind)) return "Vrsta bloka ni vključena v prevod.";
  if (/^(?:page\s*)?\d+(?:\s*\/\s*\d+)?$/i.test(value)) return "Samostojna številka strani.";
  if (/^(?:https?:\/\/|www\.|[\w.+-]+@[\w.-]+\.)/i.test(value) && !/\s/.test(value)) return "Samostojen URL ali e-poštni naslov.";
  if (!/\p{L}/u.test(value)) return "Besedilo ne vsebuje črk.";
  if (/^(?:RAL\s+\d+|[A-Z]{1,8}[-/]?(?=[A-Z0-9/-]*\d)[A-Z0-9/-]{1,20}|CO[₂2]|°C|MPa)$/u.test(value)) return "Samostojen tehnični identifikator.";
  return null;
}

export function protectForTranslation(
  text: string,
  glossary: GlossaryEntry[],
  strategy: PlaceholderStrategy = "primary_tk",
) {
  const technical = protectTechnicalTokens(text, strategy);
  const tokens = [...technical.tokens];
  let protected_text = technical.protected_text;
  for (const entry of orderedGlossary(glossary).filter((item) =>
    item.enabled &&
    item.source_term.trim() &&
    (item.mode === "do_not_translate" || (item.mode === "locked_inline" && item.target_term.trim())),
  )) {
    const flags = entry.case_sensitive ? "gu" : "giu";
    const escaped = entry.source_term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(`(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])`, flags);
    protected_text = protected_text.replace(pattern, (value) => {
      const index = String(tokens.length).padStart(3, "0");
      const placeholder = strategy === "primary_tk" ? `GLS${index}GLS` : `PHX${index}XHP`;
      tokens.push({
        placeholder,
        value,
        replacement_value: entry.mode === "do_not_translate" ? value : entry.target_term,
        kind: "glossary",
      });
      return placeholder;
    });
  }
  return { protected_text, tokens };
}

export function restoreProtectedTokens(text: string, tokens: ProtectedToken[]): string {
  const technical = tokens.filter((token) => token.kind !== "glossary");
  let restored = restoreTechnicalTokens(text, technical);
  for (const token of tokens.filter((item) => item.kind === "glossary")) {
    if (restored.split(token.placeholder).length !== 2) throw new Error("Prevod vsebuje manjkajoč ali poškodovan slovarski placeholder.");
    restored = restored.replace(token.placeholder, token.replacement_value ?? token.value);
  }
  if (/(?:TK\d{3}TK|GLS\d{3}GLS|PHX\d{3}XHP)/.test(restored)) throw new Error("Prevod vsebuje neobnovljen placeholder.");
  return restored;
}

export function editTranslation(segment: TranslationSegment, text: string): TranslationSegment {
  return {
    ...segment,
    edited_translation: text,
    status: text.trim() ? "translated" : segment.skip_reason ? "skipped" : "pending",
    error: null,
    translation_origin: text.trim() ? "manual" : null,
    glossary_entry_id: null,
    needs_review: false,
    translation_rules_version: null,
    translation_rule_adjustments: [],
  };
}

export function reviewTranslation(segment: TranslationSegment): TranslationSegment {
  if (!segment.edited_translation.trim() && !segment.translated_text.trim()) return segment;
  return { ...segment, status: "reviewed", edited_translation: segment.edited_translation || segment.translated_text, error: null, needs_review: false };
}

export function resetTranslation(segment: TranslationSegment): TranslationSegment {
  return {
    ...segment,
    translated_text: "",
    edited_translation: "",
    status: segment.skip_reason ? "skipped" : "pending",
    quality: null,
    error: null,
    translation_origin: null,
    glossary_entry_id: null,
    model_id: null,
    provider_id: null,
    placeholder_attempts: [],
    needs_review: false,
    warnings: [],
    chunks: [],
    forced_chunking: false,
    translation_rules_version: null,
    translation_rule_adjustments: [],
  };
}

export function retryFailedTranslation(segment: TranslationSegment): TranslationSegment {
  return segment.status === "failed" ? { ...segment, status: "pending", error: null } : segment;
}

export function updateJobGlossary(job: TranslationJob, glossary: GlossaryEntry[]): TranslationJob {
  const snapshot = cloneGlossary(glossary);
  return refreshTranslationProfileMetadata({
    ...job,
    glossary: snapshot,
    glossary_snapshot: cloneGlossary(snapshot),
    segments: job.segments.map((segment) => {
      const refreshed = {
        ...segment,
        protected_tokens: protectForTranslation(segment.source_text, snapshot).tokens,
        glossary_matches: findGlossaryMatches(segment.source_text, snapshot),
      };
      if (segment.status === "reviewed" || segment.translation_origin === "manual" || segment.translation_origin === "provider") return refreshed;
      return applyExactSegmentGlossary(resetTranslation(refreshed), snapshot);
    }),
  });
}

export function updateTranslationJobSegment(
  job: TranslationJob,
  segmentId: string,
  update: (segment: TranslationSegment) => TranslationSegment,
): TranslationJob {
  return refreshTranslationProfileMetadata({
    ...job,
    segments: job.segments.map((segment) => segment.segment_id === segmentId ? update(segment) : segment),
  });
}

export async function translateJobBatch(
  job: TranslationJob,
  provider: TranslationProvider,
  signal?: AbortSignal,
  onProgress?: (completed: number, total: number) => void,
): Promise<TranslationJob> {
  if (signal?.aborted) return job;
  const glossary = cloneGlossary(job.glossary);
  const withGlossary = {
    ...job,
    glossary_snapshot: cloneGlossary(glossary),
    segments: job.segments.map((segment) =>
      ["pending", "failed"].includes(segment.status) && !segment.skip_reason
        ? applyExactSegmentGlossary(segment, glossary)
        : segment,
    ),
  };
  const selected = withGlossary.segments.filter((segment) => ["pending", "failed"].includes(segment.status) && !segment.skip_reason);
  const initialChunks = new Map(selected.map((segment) => [segment.segment_id, createTranslationChunks(segment)]));
  let translated = await translateChunkSet(initialChunks, selected, glossary, provider, job, "primary_tk", signal, onProgress);
  if (signal?.aborted) return job;

  const forced = new Map<string, TranslationChunk[]>();
  for (const segment of translated) {
    const replacements = segment.chunks.flatMap((chunk) =>
      chunk.status === "failed" && chunk.warnings.some((warning) => ["source_token_limit", "decoding_limit"].includes(warning))
        ? splitOversizedChunk(chunk) ?? [chunk]
        : [chunk],
    );
    if (replacements.length !== segment.chunks.length) forced.set(segment.segment_id, replacements);
  }
  if (forced.size) {
    const forcedSegments = translated.filter((segment) => forced.has(segment.segment_id));
    const retried = await translateChunkSet(forced, forcedSegments, glossary, provider, job, "primary_tk", signal);
    const byId = new Map(retried.map((segment) => [segment.segment_id, { ...segment, forced_chunking: true, needs_review: true, warnings: [...new Set([...segment.warnings, "forced_chunking"])] }]));
    translated = translated.map((segment) => byId.get(segment.segment_id) ?? segment);
  }
  if (signal?.aborted) return job;

  const placeholderRetry = new Map<string, TranslationChunk[]>();
  for (const segment of translated) {
    const failed = segment.chunks.filter((chunk) => chunk.status === "failed" && chunk.warnings.includes("placeholder_integrity"));
    if (failed.length) placeholderRetry.set(segment.segment_id, failed);
  }
  if (placeholderRetry.size) {
    const retrySegments = translated.filter((segment) => placeholderRetry.has(segment.segment_id));
    const retries = await translateChunkSet(placeholderRetry, retrySegments, glossary, provider, job, "retry_phx", signal);
    const retryBySegment = new Map(retries.map((segment) => [segment.segment_id, segment.chunks]));
    translated = translated.map((segment) => {
      const retryChunks = retryBySegment.get(segment.segment_id);
      if (!retryChunks) return segment;
      const byChunk = new Map(retryChunks.map((chunk) => [chunk.chunk_id, chunk]));
      return assembleSegment(segment, segment.chunks.map((chunk) => byChunk.get(chunk.chunk_id) ?? chunk), true);
    });
  }
  const translatedById = new Map(translated.map((segment) => [segment.segment_id, segment]));
  return refreshTranslationProfileMetadata({
    ...withGlossary,
    segments: withGlossary.segments.map((segment) => translatedById.get(segment.segment_id) ?? segment),
  });
}

type PreparedChunk = { chunk: TranslationChunk; request: TranslationRequest; tokens: ProtectedToken[] };

async function translateChunkSet(chunksBySegment: Map<string, TranslationChunk[]>, segments: TranslationSegment[], glossary: GlossaryEntry[], provider: TranslationProvider, job: TranslationJob, strategy: PlaceholderStrategy, signal?: AbortSignal, onProgress?: (completed: number, total: number) => void): Promise<TranslationSegment[]> {
  const segmentById = new Map(segments.map((segment) => [segment.segment_id, segment]));
  const prepared = [...chunksBySegment.values()].flat().map((chunk): PreparedChunk => {
    const protectedValue = protectForTranslation(chunk.source_text, glossary, strategy);
    const segment = segmentById.get(chunk.segment_id)!;
    return { chunk, tokens: protectedValue.tokens, request: { segment_id: chunk.chunk_id, text: protectedValue.protected_text, context: { segment_id: segment.segment_id, page_number: segment.page_number, block_type: segment.block_type, glossary_matches: segment.glossary_matches } } };
  });
  let results: TranslationProviderResult[];
  try {
    results = await provider.translateBatch(prepared.map((item) => item.request), { source_language: job.source_language, target_language: job.target_language, signal, onProgress });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return segments.map((segment) => assembleSegment(segment, (chunksBySegment.get(segment.segment_id) ?? []).map((chunk) => ({ ...chunk, status: "failed", warnings: [message] }))));
  }
  const resultCounts = new Map<string, number>();
  for (const result of results) resultCounts.set(result.segment_id, (resultCounts.get(result.segment_id) ?? 0) + 1);
  const byId = new Map(results.map((result) => [result.segment_id, result]));
  const completed = new Map<string, TranslationChunk[]>();
  for (const item of prepared) {
    const result = byId.get(item.chunk.chunk_id);
    const chunk = resultCounts.get(item.chunk.chunk_id) === 1
      ? applyChunkResult(item, result, strategy)
      : { ...item.chunk, status: "failed" as const, warnings: [result ? "duplicate_chunk_result" : "missing_chunk_result"] };
    completed.set(chunk.segment_id, [...(completed.get(chunk.segment_id) ?? []), chunk]);
  }
  return segments.map((segment) => {
    const firstResult = prepared
      .filter((item) => item.chunk.segment_id === segment.segment_id)
      .map((item) => byId.get(item.chunk.chunk_id))
      .find(Boolean);
    const lockedInline = segment.glossary_matches.some((match) => match.mode === "locked_inline");
    return assembleSegment({
      ...segment,
      model_id: firstResult?.model_id ?? segment.model_id,
      provider_id: firstResult?.provider_id ?? provider.id,
      needs_review: segment.needs_review || lockedInline,
      warnings: lockedInline ? [...new Set([...segment.warnings, "locked_inline_glossary"])] : segment.warnings,
    }, completed.get(segment.segment_id) ?? []);
  });
}

function applyChunkResult(prepared: PreparedChunk, result: TranslationProviderResult | undefined, strategy: PlaceholderStrategy): TranslationChunk {
  const decoding = result?.max_decoding_length == null ? null : { max_decoding_length: result.max_decoding_length, output_token_count: result.output_token_count ?? 0, reached_limit: result.reached_decoding_limit === true, has_end_token: result.has_end_token ?? null, beam_size: result.beam_size ?? 2 };
  const metadata = { source_token_count: result?.source_token_count ?? null, decoding };
  if (!result) return { ...prepared.chunk, ...metadata, status: "failed", warnings: ["missing_chunk_result"] };
  if (result.error || result.translated_text === null) return { ...prepared.chunk, ...metadata, status: "failed", warnings: [result.error?.code ?? "empty_chunk_result"] };
  if (result.reached_decoding_limit) return { ...prepared.chunk, ...metadata, status: "failed", warnings: ["decoding_limit"] };
  try {
    const translated_text = restoreProtectedTokens(result.translated_text, prepared.tokens);
    const retainedWarnings = prepared.chunk.warnings.filter((warning) => warning === "forced_chunking");
    return { ...prepared.chunk, ...metadata, translated_text, status: translated_text.trim() ? "translated" : "failed", warnings: translated_text.trim() ? retainedWarnings : [...retainedWarnings, "empty_chunk_result"], placeholder_attempts: [...prepared.chunk.placeholder_attempts, placeholderAttempt(strategy, result.translated_text, true, null)] };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { ...prepared.chunk, ...metadata, status: "failed", warnings: ["placeholder_integrity"], placeholder_attempts: [...prepared.chunk.placeholder_attempts, placeholderAttempt(strategy, result.translated_text, false, message)] };
  }
}

function assembleSegment(segment: TranslationSegment, chunks: TranslationChunk[], placeholderRetry = false): TranslationSegment {
  const complete = chunks.length > 0 && chunks.every((chunk) => chunk.status === "translated" && chunk.translated_text.trim());
  const technical = protectTechnicalTokens(segment.source_text);
  const adjustments = technical.tokens.flatMap((token) => {
    const values: TranslationSegment["translation_rule_adjustments"] = [];
    if (token.rule_id && token.rule_version && token.replacement_value !== undefined) values.push({
      rule_id: token.rule_id,
      rule_version: token.rule_version,
      kind: token.rule_id === "decimal_separator" ? "decimal_number" : token.technical_range?.kind ?? "unit_spacing",
      source_text: token.value,
      replacement_text: token.replacement_value,
    });
    if (token.rule_id !== "decimal_separator" && token.profile_rule_ids?.includes("SL-SSKJ-05-001")) values.push({
      rule_id: "decimal_separator",
      rule_version: "sl-general-sskj-v1",
      kind: "decimal_number",
      source_text: token.value,
      replacement_text: token.replacement_value ?? token.value,
    });
    return values;
  });
  const ruleWarnings = complete && adjustments.length > 0 ? ["technical_translation_rule_applied"] : [];
  const warnings = [...new Set([...segment.warnings, ...technical.warnings, ...ruleWarnings, ...chunks.flatMap((chunk) => chunk.warnings)])];
  if (placeholderRetry && complete) warnings.push("placeholder_retry_succeeded");
  if (placeholderRetry && !complete) warnings.push("placeholder_retry_failed");
  const translated = complete ? chunks.map((chunk) => chunk.translated_text + chunk.separator_after).join("").trimEnd() : "";
  return {
    ...segment,
    chunks,
    translated_text: translated,
    edited_translation: translated,
    status: complete ? "translated" : "failed",
    error: complete ? null : "Vsaj en modelni del segmenta ni bil uspešno preveden.",
    translation_origin: complete ? "provider" : segment.translation_origin,
    placeholder_attempts: chunks.flatMap((chunk) => chunk.placeholder_attempts),
    needs_review: segment.needs_review || placeholderRetry || segment.forced_chunking || adjustments.length > 0 || technical.warnings.length > 0,
    warnings,
    model_id: segment.model_id,
    provider_id: segment.provider_id,
    translation_rules_version: complete && adjustments.length > 0 ? TECHNICAL_RANGE_RULES_VERSION : null,
    translation_rule_adjustments: complete ? adjustments : [],
  };
}

function placeholderAttempt(strategy: PlaceholderStrategy, provider_text: string | null, valid: boolean, error: string | null): PlaceholderAttempt {
  return { strategy, provider_text, valid, error };
}

export function createTranslationChunks(segment: TranslationSegment): TranslationChunk[] {
  const parts = ["paragraph", "list_item"].includes(segment.block_type)
    ? sentenceRanges(segment.source_text)
    : [{ source_text: segment.source_text, source_start: 0, source_end: segment.source_text.length, separator_after: "" }];
  return parts.map((part, index) => ({
    chunk_id: `${segment.segment_id}-c${index + 1}`,
    segment_id: segment.segment_id,
    ...part,
    source_token_count: null,
    translated_text: "",
    status: "pending",
    warnings: [],
    placeholder_attempts: [],
    decoding: null,
  }));
}

function sentenceRanges(text: string): Array<Pick<TranslationChunk, "source_text" | "source_start" | "source_end" | "separator_after">> {
  const ranges: Array<Pick<TranslationChunk, "source_text" | "source_start" | "source_end" | "separator_after">> = [];
  let start = 0;
  const boundary = /[.!?](?=(?:\r?\n|[ \t])+\S)/g;
  for (const match of text.matchAll(boundary)) {
    const end = match.index! + 1;
    const candidate = text.slice(start, end);
    if (/(?:\be\.g\.|\bi\.e\.|(?:^|\s)[A-Z]\.)$/i.test(candidate)) continue;
    let next = end;
    while (next < text.length && /\s/.test(text[next])) next += 1;
    ranges.push({ source_text: text.slice(start, end), source_start: start, source_end: end, separator_after: text.slice(end, next) });
    start = next;
  }
  if (start < text.length) ranges.push({ source_text: text.slice(start), source_start: start, source_end: text.length, separator_after: "" });
  return ranges.length ? ranges : [{ source_text: text, source_start: 0, source_end: text.length, separator_after: "" }];
}

export function splitOversizedChunk(chunk: TranslationChunk): TranslationChunk[] | null {
  const candidates = [...chunk.source_text.matchAll(/[;:,](?=\s)/g)].map((match) => match.index! + 1);
  if (!candidates.length) return null;
  const middle = chunk.source_text.length / 2;
  const split = candidates.sort((a, b) => Math.abs(a - middle) - Math.abs(b - middle))[0];
  let right = split;
  while (right < chunk.source_text.length && /\s/.test(chunk.source_text[right])) right += 1;
  const leftText = chunk.source_text.slice(0, split);
  const rightText = chunk.source_text.slice(right);
  if (!leftText.trim() || !rightText.trim() || /(?:TK|GLS|PHX)\d*$/.test(leftText)) return null;
  return [
    { ...chunk, chunk_id: `${chunk.chunk_id}.1`, source_text: leftText, source_end: chunk.source_start + split, separator_after: chunk.source_text.slice(split, right), status: "pending", translated_text: "", warnings: ["forced_chunking"], placeholder_attempts: [], decoding: null },
    { ...chunk, chunk_id: `${chunk.chunk_id}.2`, source_text: rightText, source_start: chunk.source_start + right, separator_after: chunk.separator_after, status: "pending", translated_text: "", warnings: ["forced_chunking"], placeholder_attempts: [], decoding: null },
  ];
}

function applyExactSegmentGlossary(segment: TranslationSegment, glossary: GlossaryEntry[]): TranslationSegment {
  const entry = findExactSegmentGlossaryMatch(segment.source_text, glossary);
  if (!entry?.target_term.trim()) return segment;
  return {
    ...segment,
    translated_text: entry.target_term,
    edited_translation: entry.target_term,
    status: "translated",
    quality: null,
    error: null,
    glossary_matches: findGlossaryMatches(segment.source_text, glossary),
    translation_origin: "glossary",
    glossary_entry_id: entry.id,
    model_id: null,
    provider_id: null,
    placeholder_attempts: [],
    needs_review: false,
    warnings: [],
  };
}

export function exportTranslationJob(job: TranslationJob): string {
  const refreshed = refreshTranslationProfileMetadata(job);
  return JSON.stringify({ ...refreshed, glossary_snapshot: cloneGlossary(refreshed.glossary) }, null, 2);
}

export function importTranslationJob(value: string): TranslationJob {
  let parsed: unknown;
  try { parsed = JSON.parse(value); }
  catch { throw new Error("Datoteka ni veljaven JSON."); }
  if (!isRecord(parsed)) throw new Error("JSON nima veljavne strukture prevajalskega opravila.");
  const migrated = parsed.schema_version === 1
    ? migrateSchemaFour(migrateSchemaThree(migrateSchemaTwo(migrateSchemaOne(parsed))))
    : parsed.schema_version === 2
      ? migrateSchemaFour(migrateSchemaThree(migrateSchemaTwo(parsed)))
      : parsed.schema_version === 3
        ? migrateSchemaFour(migrateSchemaThree(parsed))
        : parsed.schema_version === 4 ? migrateSchemaFour(parsed) : migrateSchemaFiveProfileMetadata(parsed);
  if (!isRecord(migrated) || migrated.schema_version !== TRANSLATION_JOB_SCHEMA_VERSION) throw new Error("Različica prevajalskega opravila ni podprta.");
  validateJobShape(migrated);
  return refreshTranslationProfileMetadata(migrated as TranslationJob);
}

export function validateTranslationJobCompatibility(imported: TranslationJob, expected: TranslationJob): void {
  if (imported.source_filename !== expected.source_filename) throw new Error("Opravilo pripada drugemu izvornemu dokumentu.");
  if (imported.segments.length !== expected.segments.length) throw new Error("Opravilo ni združljivo s trenutnim dokumentnim modelom.");
  const expectedById = new Map(expected.segments.map((segment) => [segment.segment_id, segment]));
  for (const segment of imported.segments) {
    const source = expectedById.get(segment.segment_id);
    if (!source ||
      segment.page_number !== source.page_number ||
      segment.block_id !== source.block_id ||
      segment.block_type !== source.block_type ||
      segment.source_text !== source.source_text ||
      segment.source_text_origin !== source.source_text_origin ||
      JSON.stringify(segment.accepted_ocr_source ?? null) !== JSON.stringify(source.accepted_ocr_source ?? null)) {
      throw new Error("Opravilo vsebuje spremenjene ali nezdružljive izvorne segmente.");
    }
  }
  if (imported.document_identity && expected.document_identity &&
    JSON.stringify(imported.document_identity) !== JSON.stringify(expected.document_identity)) {
    throw new Error("Opravilo pripada drugi različici izvornega PDF-ja.");
  }
}

export function bindTranslationJobToAnalysis(imported: TranslationJob, expected: TranslationJob): TranslationJob {
  validateTranslationJobCompatibility(imported, expected);
  const expectedById = new Map(expected.segments.map((segment) => [segment.segment_id, segment]));
  return {
    ...imported,
    document_identity: expected.document_identity,
    segments: imported.segments.map((segment) => {
      const source = expectedById.get(segment.segment_id)!;
      return {
        ...segment,
        source_bbox: source.source_bbox,
        source_page_type: source.source_page_type,
        source_rotation: source.source_rotation,
        source_style: source.source_style,
        accepted_ocr_source: source.accepted_ocr_source ?? null,
      };
    }),
  };
}

function migrateSchemaOne(job: Record<string, unknown>): Record<string, unknown> {
  validateBaseJob(job);
  const glossary = (job.glossary as unknown[]).map((value, index) => migrateGlossaryEntry(value, index));
  const segments = (job.segments as unknown[]).map((value) => {
    if (!isRecord(value) || typeof value.source_text !== "string") throw new Error("JSON vsebuje neveljaven prevajalski segment.");
    const translated =
      (typeof value.edited_translation === "string" && value.edited_translation.trim()) ||
      (typeof value.translated_text === "string" && value.translated_text.trim());
    return {
      ...value,
      protected_tokens: protectForTranslation(value.source_text, glossary).tokens,
      glossary_matches: findGlossaryMatches(value.source_text, glossary),
      translation_origin: translated ? "manual" : null,
      glossary_entry_id: null,
      model_id: null,
      provider_id: null,
      placeholder_attempts: [],
      needs_review: false,
      warnings: [],
    };
  });
  return { ...job, schema_version: 2, glossary, glossary_snapshot: cloneGlossary(glossary), segments };
}

function migrateSchemaTwo(job: Record<string, unknown>): Record<string, unknown> {
  validateBaseJob(job);
  return {
    ...job,
    schema_version: 3,
    segments: (job.segments as unknown[]).map((value) => {
      if (!isRecord(value)) throw new Error("JSON vsebuje neveljaven prevajalski segment.");
      return { ...value, chunks: [], forced_chunking: false };
    }),
  };
}

function migrateSchemaThree(job: Record<string, unknown>): Record<string, unknown> {
  validateBaseJob(job);
  return {
    ...job,
    schema_version: 4,
    document_identity: null,
    segments: (job.segments as unknown[]).map((value) => {
      if (!isRecord(value)) throw new Error("JSON vsebuje neveljaven prevajalski segment.");
      return {
        ...value,
        source_bbox: null,
        source_page_type: null,
        source_rotation: null,
        source_style: null,
      };
    }),
  };
}

function migrateSchemaFour(job: Record<string, unknown>): Record<string, unknown> {
  validateBaseJob(job);
  return {
    ...job,
    schema_version: 5,
    translation_profile: null,
    quality_checklist: [],
  };
}

function migrateSchemaFiveProfileMetadata(job: Record<string, unknown>): Record<string, unknown> {
  if (job.schema_version !== 5 || !isRecord(job.translation_profile)) return job;
  const profile = job.translation_profile;
  return {
    ...job,
    translation_profile: {
      ...profile,
      evaluation_status: typeof profile.evaluation_status === "string" ? profile.evaluation_status : "stale",
      rule_evaluations: Array.isArray(profile.rule_evaluations) ? profile.rule_evaluations : [],
      diagnostics: Array.isArray(profile.diagnostics) ? profile.diagnostics.map((item) => isRecord(item) ? {
        ...item,
        segment_id: typeof item.segment_id === "string" ? item.segment_id : null,
        source_text: typeof item.source_text === "string" ? item.source_text : null,
        required_source_type: typeof item.required_source_type === "string" ? item.required_source_type : null,
        source_status: ["not_required", "not_supplied", "supplied_unconfirmed", "user_confirmed"].includes(String(item.source_status)) ? item.source_status : "not_required",
      } : item) : [],
    },
  };
}

function migrateGlossaryEntry(value: unknown, index: number): GlossaryEntry {
  if (!isRecord(value) || typeof value.source_term !== "string" || typeof value.target_term !== "string") throw new Error("JSON vsebuje neveljaven slovarski vnos.");
  return {
    id: typeof value.id === "string" ? value.id : `migrated-${index}`,
    source_term: value.source_term,
    target_term: value.target_term,
    case_sensitive: value.case_sensitive === true,
    mode: value.do_not_translate === true ? "do_not_translate" : "locked_inline",
    enabled: true,
    origin: "user",
  };
}

function validateJobShape(job: Record<string, unknown>): void {
  validateBaseJob(job);
  if (!isTranslationProfileSnapshot(job.translation_profile)) throw new Error("JSON vsebuje neveljavne podatke prevajalskega profila.");
  if (!Array.isArray(job.quality_checklist) || !job.quality_checklist.every(isQualityCheck)) throw new Error("JSON vsebuje neveljaven kontrolni seznam prevoda.");
  if (job.document_identity !== null && !isDocumentIdentity(job.document_identity)) throw new Error("JSON vsebuje neveljavno identiteto PDF-ja.");
  if (!Array.isArray(job.glossary_snapshot)) throw new Error("JSON nima posnetka uporabljenega slovarja.");
  for (const entry of [...job.glossary as unknown[], ...job.glossary_snapshot]) validateGlossaryEntry(entry);
  for (const segment of job.segments as unknown[]) {
    if (!isRecord(segment) ||
      typeof segment.segment_id !== "string" ||
      typeof segment.page_number !== "number" ||
      typeof segment.block_id !== "string" ||
      typeof segment.block_type !== "string" ||
      typeof segment.source_language !== "string" ||
      typeof segment.target_language !== "string" ||
      typeof segment.source_text !== "string" ||
      !["native", "accepted_ocr"].includes(String(segment.source_text_origin)) ||
      (segment.source_bbox !== null && !isBBox(segment.source_bbox)) ||
      ![null, "digital", "scanned", "mixed", "empty"].includes(segment.source_page_type as null | string) ||
      !(segment.source_rotation === null || typeof segment.source_rotation === "number") ||
      !(segment.source_style === null || isRecord(segment.source_style)) ||
      !isAcceptedOcrSource(segment.accepted_ocr_source, segment.source_text_origin, segment.source_text, segment.source_bbox) ||
      typeof segment.translated_text !== "string" ||
      typeof segment.edited_translation !== "string" ||
      !["pending", "translating", "translated", "reviewed", "failed", "skipped"].includes(String(segment.status)) ||
      ![null, "glossary", "provider", "manual"].includes(segment.translation_origin as null | string) ||
      !isStringOrNull(segment.glossary_entry_id) ||
      !isStringOrNull(segment.model_id) ||
      !isStringOrNull(segment.provider_id) ||
      typeof segment.needs_review !== "boolean" ||
      !Array.isArray(segment.placeholder_attempts) ||
      !segment.placeholder_attempts.every(isPlaceholderAttempt) ||
      !Array.isArray(segment.chunks) ||
      !segment.chunks.every(isTranslationChunk) ||
      typeof segment.forced_chunking !== "boolean" ||
      !isTranslationRuleMetadata(segment.translation_rules_version, segment.translation_rule_adjustments) ||
      !Array.isArray(segment.warnings) ||
      !segment.warnings.every((warning) => typeof warning === "string")) throw new Error("JSON vsebuje neveljaven prevajalski segment.");
  }
}

function isTranslationProfileSnapshot(value: unknown): boolean {
  if (value === null) return true;
  return isRecord(value) &&
    typeof value.profile_id === "string" && value.profile_id.length > 0 &&
    typeof value.profile_version === "number" && Number.isInteger(value.profile_version) && value.profile_version > 0 &&
    value.target_language === "sl" &&
    typeof value.source_sha256 === "string" && /^[a-f0-9]{64}$/.test(value.source_sha256) &&
    typeof value.canonical_sha256 === "string" && /^[a-f0-9]{64}$/.test(value.canonical_sha256) &&
    typeof value.canonical_date === "string" &&
    [value.applied_rule_ids, value.manual_review_rule_ids, value.unverifiable_rule_ids].every((items) =>
      Array.isArray(items) && items.every((item) => typeof item === "string"),
    ) &&
    ["current", "stale", "unknown_profile"].includes(String(value.evaluation_status)) &&
    Array.isArray(value.rule_evaluations) && value.rule_evaluations.every(isRuleEvaluation) &&
    Array.isArray(value.diagnostics) && value.diagnostics.every((item) =>
      isRecord(item) && typeof item.message === "string" &&
      (item.segment_id === null || typeof item.segment_id === "string") &&
      (item.source_text === null || typeof item.source_text === "string") &&
      (item.required_source_type === null || typeof item.required_source_type === "string") &&
      ["not_required", "not_supplied", "supplied_unconfirmed", "user_confirmed"].includes(String(item.source_status)),
    ) &&
    Array.isArray(value.conflicts) && value.conflicts.every((item) => isRecord(item) && typeof item.winner === "string" && typeof item.reason === "string") &&
    Array.isArray(value.terminology_sources) && value.terminology_sources.every((item) =>
      isRecord(item) && typeof item.selected_term === "string" && typeof item.domain === "string" &&
      (item.source_or_citation === null || typeof item.source_or_citation === "string") &&
      (item.confidence === null || ["low", "medium", "high"].includes(String(item.confidence))) &&
      typeof item.user_confirmed === "boolean",
    );
}

function isRuleEvaluation(value: unknown): boolean {
  return isRecord(value) &&
    typeof value.rule_id === "string" &&
    ["document", "segment"].includes(String(value.scope)) &&
    ["applicable", "not_applicable", "unknown"].includes(String(value.applicability)) &&
    ["passed", "failed", "needs_review", "not_checkable", "not_applicable"].includes(String(value.status)) &&
    Array.isArray(value.segment_ids) && value.segment_ids.every((item) => typeof item === "string") &&
    typeof value.message === "string";
}

function isQualityCheck(value: unknown): boolean {
  return isRecord(value) &&
    typeof value.check_id === "string" &&
    typeof value.source_rule_id === "string" &&
    typeof value.label === "string" &&
    ["passed", "failed", "needs_review", "not_checkable", "not_applicable"].includes(String(value.status)) &&
    typeof value.message === "string";
}

function isTranslationRuleMetadata(version: unknown, adjustments: unknown): boolean {
  if (version === undefined && adjustments === undefined) return true;
  if (!(version === null || typeof version === "string") || !Array.isArray(adjustments)) return false;
  return adjustments.every((item) =>
    isRecord(item) &&
    ["technical_range", "technical_unit_spacing", "decimal_separator"].includes(String(item.rule_id)) &&
    typeof item.rule_version === "string" &&
    ["temperature", "measurement", "percentage", "time", "unit_spacing", "decimal_number"].includes(String(item.kind)) &&
    typeof item.source_text === "string" &&
    typeof item.replacement_text === "string",
  );
}

function isAcceptedOcrSource(value: unknown, origin: unknown, text: unknown, bbox: unknown): boolean {
  if (origin !== "accepted_ocr") return value === undefined || value === null;
  if (value === undefined || value === null) return true;
  if (!isRecord(value) || !(value.revision_id === null || ["string", "undefined"].includes(typeof value.revision_id)) ||
    typeof value.review_id !== "string" || !value.review_id ||
    typeof value.accepted_text !== "string" || value.accepted_text !== text ||
    !isBBox(value.bbox) || JSON.stringify(value.bbox) !== JSON.stringify(bbox) ||
    !["block", "page", "image", "manual"].includes(String(value.source)) || typeof value.reason !== "string") return false;
  return true;
}

function isDocumentIdentity(value: unknown): boolean {
  return isRecord(value) &&
    typeof value.sha256 === "string" && /^[a-f0-9]{64}$/.test(value.sha256) &&
    typeof value.page_count === "number" &&
    typeof value.document_model_version === "number" &&
    Array.isArray(value.pages) && value.pages.every((page) =>
      isRecord(page) &&
      typeof page.page_number === "number" &&
      typeof page.width === "number" &&
      typeof page.height === "number" &&
      typeof page.rotation === "number",
    );
}

function isBBox(value: unknown): boolean {
  return isRecord(value) && [value.x0, value.y0, value.x1, value.y1].every((coordinate) => typeof coordinate === "number" && Number.isFinite(coordinate));
}

function validateGlossaryEntry(value: unknown): void {
  if (!isRecord(value) ||
    typeof value.id !== "string" ||
    typeof value.source_term !== "string" ||
    typeof value.target_term !== "string" ||
    typeof value.case_sensitive !== "boolean" ||
    !["exact_segment", "do_not_translate", "preferred", "locked_inline"].includes(String(value.mode)) ||
    typeof value.enabled !== "boolean" ||
    !["built_in", "user"].includes(String(value.origin)) ||
    (value.default_target_term !== undefined && typeof value.default_target_term !== "string")) {
    throw new Error("JSON vsebuje neveljaven slovarski vnos.");
  }
}

function isPlaceholderAttempt(value: unknown): boolean {
  return isRecord(value) &&
    ["primary_tk", "retry_phx"].includes(String(value.strategy)) &&
    isStringOrNull(value.provider_text) &&
    typeof value.valid === "boolean" &&
    isStringOrNull(value.error);
}

function isTranslationChunk(value: unknown): boolean {
  return isRecord(value) &&
    typeof value.chunk_id === "string" &&
    typeof value.segment_id === "string" &&
    typeof value.source_text === "string" &&
    typeof value.source_start === "number" &&
    typeof value.source_end === "number" &&
    typeof value.separator_after === "string" &&
    ["pending", "translated", "failed"].includes(String(value.status)) &&
    Array.isArray(value.warnings) &&
    Array.isArray(value.placeholder_attempts);
}

function isStringOrNull(value: unknown): boolean {
  return value === null || typeof value === "string";
}

function validateBaseJob(job: Record<string, unknown>): void {
  if (typeof job.source_language !== "string" ||
    typeof job.target_language !== "string" ||
    typeof job.source_filename !== "string" ||
    !Array.isArray(job.glossary) ||
    !Array.isArray(job.segments)) throw new Error("JSON nima veljavne strukture prevajalskega opravila.");
}

function cloneGlossary(entries: GlossaryEntry[]): GlossaryEntry[] {
  return entries.map((entry) => ({ ...entry }));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
