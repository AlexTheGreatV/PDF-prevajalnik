import { findGlossaryMatches } from "./glossary";
import type { OcrReviewItem } from "./ocr-review";
import type { PdfAnalysis, StructuredBlock, StructuredRegion } from "./pdf-types";
import { protectTechnicalTokens, restoreTechnicalTokens } from "./technical-tokens";
import type {
  GlossaryEntry,
  ProtectedToken,
  TranslationJob,
  TranslationProvider,
  TranslationRequest,
  TranslationSegment,
} from "./translation-types";

export const TRANSLATION_JOB_SCHEMA_VERSION = 1 as const;

export function createTranslationJob(
  analysis: PdfAnalysis,
  ocrItems: OcrReviewItem[],
  options: { source_language: string; target_language: string; glossary?: GlossaryEntry[]; created_at?: string },
): TranslationJob {
  const created_at = options.created_at ?? new Date().toISOString();
  const glossary = options.glossary ?? [];
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
          region,
          block,
          source_origin: acceptedOcr ? "accepted_ocr" : "native",
          source_language: options.source_language,
          target_language: options.target_language,
          glossary,
          created_at,
        }));
      }
    }
  }

  return {
    schema_version: TRANSLATION_JOB_SCHEMA_VERSION,
    source_filename: analysis.filename,
    source_language: options.source_language,
    target_language: options.target_language,
    created_at,
    glossary,
    segments,
  };
}

type SegmentInput = {
  segment_id: string;
  block_id: string;
  block_type: TranslationSegment["block_type"];
  page_number: number;
  region: StructuredRegion;
  block: StructuredBlock;
  source_origin: TranslationSegment["source_text_origin"];
  source_language: string;
  target_language: string;
  glossary: GlossaryEntry[];
  created_at: string;
};

function createSegment(sourceText: string, input: SegmentInput): TranslationSegment {
  const source_text = sourceText.trim();
  const skip_reason = getSkipReason(source_text, input.block_type, input.region.kind);
  const protected_tokens = protectForTranslation(source_text, input.glossary).tokens;
  return {
    segment_id: input.segment_id,
    page_number: input.page_number,
    block_id: input.block_id,
    block_type: input.block_type,
    source_language: input.source_language,
    target_language: input.target_language,
    source_text,
    source_text_origin: input.source_origin,
    translated_text: "",
    edited_translation: "",
    status: skip_reason ? "skipped" : "pending",
    quality: null,
    error: null,
    skip_reason,
    protected_tokens,
    glossary_matches: findGlossaryMatches(source_text, input.glossary),
    created_at: input.created_at,
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
  if (!["heading", "paragraph", "list_item", "table_cell", "caption"].includes(blockType) && !["header", "footer"].includes(regionKind)) {
    return "Vrsta bloka ni vključena v prevod.";
  }
  if (/^(?:page\s*)?\d+(?:\s*\/\s*\d+)?$/i.test(value)) return "Samostojna številka strani.";
  if (/^(?:https?:\/\/|www\.|[\w.+-]+@[\w.-]+\.)/i.test(value) && !/\s/.test(value)) return "Samostojen URL ali e-poštni naslov.";
  if (!/\p{L}/u.test(value)) return "Besedilo ne vsebuje črk.";
  if (/^(?:RAL\s+\d+|[A-Z]{1,8}[-/]?(?=[A-Z0-9/-]*\d)[A-Z0-9/-]{1,20}|CO[₂2]|°C|MPa)$/u.test(value)) {
    return "Samostojen tehnični identifikator.";
  }
  return null;
}

export function protectForTranslation(text: string, glossary: GlossaryEntry[]) {
  const technical = protectTechnicalTokens(text);
  const tokens = [...technical.tokens];
  let protected_text = technical.protected_text;
  for (const entry of glossary.filter((item) => item.source_term.trim() && (item.do_not_translate || item.target_term.trim()))) {
    const flags = entry.case_sensitive ? "gu" : "giu";
    const escaped = entry.source_term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(entry.exact_match ? `(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])` : escaped, flags);
    protected_text = protected_text.replace(pattern, (value) => {
      const placeholder = `GLS${String(tokens.length).padStart(3, "0")}GLS`;
      tokens.push({
        placeholder,
        value,
        replacement_value: entry.do_not_translate ? value : entry.target_term,
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
  const glossary = tokens.filter((token) => token.kind === "glossary");
  for (const token of glossary) {
    if (restored.split(token.placeholder).length !== 2) {
      throw new Error("Prevod vsebuje manjkajoč ali poškodovan slovarski placeholder.");
    }
    restored = restored.replace(token.placeholder, token.replacement_value ?? token.value);
  }
  if (/(?:TK\d{3}TK|GLS\d{3}GLS)/.test(restored)) throw new Error("Prevod vsebuje neobnovljen placeholder.");
  return restored;
}

export function editTranslation(segment: TranslationSegment, text: string): TranslationSegment {
  return { ...segment, edited_translation: text, status: text.trim() ? "translated" : segment.skip_reason ? "skipped" : "pending", error: null };
}

export function reviewTranslation(segment: TranslationSegment): TranslationSegment {
  if (!segment.edited_translation.trim() && !segment.translated_text.trim()) return segment;
  return { ...segment, status: "reviewed", edited_translation: segment.edited_translation || segment.translated_text, error: null };
}

export function resetTranslation(segment: TranslationSegment): TranslationSegment {
  return { ...segment, translated_text: "", edited_translation: "", status: segment.skip_reason ? "skipped" : "pending", quality: null, error: null };
}

export function retryFailedTranslation(segment: TranslationSegment): TranslationSegment {
  return segment.status === "failed" ? { ...segment, status: "pending", error: null } : segment;
}

export function updateJobGlossary(job: TranslationJob, glossary: GlossaryEntry[]): TranslationJob {
  return {
    ...job,
    glossary,
    segments: job.segments.map((segment) => ({
      ...segment,
      protected_tokens: protectForTranslation(segment.source_text, glossary).tokens,
      glossary_matches: findGlossaryMatches(segment.source_text, glossary),
    })),
  };
}

export async function translateJobBatch(
  job: TranslationJob,
  provider: TranslationProvider,
  signal?: AbortSignal,
  onProgress?: (completed: number, total: number) => void,
): Promise<TranslationJob> {
  const selected = job.segments.filter((segment) => ["pending", "failed"].includes(segment.status) && !segment.skip_reason);
  if (signal?.aborted) return job;
  const prepared = new Map<string, { request: TranslationRequest; tokens: ProtectedToken[] }>();
  for (const segment of selected) {
    const protectedValue = protectForTranslation(segment.source_text, job.glossary);
    prepared.set(segment.segment_id, {
      tokens: protectedValue.tokens,
      request: {
        segment_id: segment.segment_id,
        text: protectedValue.protected_text,
        context: { segment_id: segment.segment_id, page_number: segment.page_number, block_type: segment.block_type, glossary_matches: segment.glossary_matches },
      },
    });
  }
  let results;
  try {
    results = await provider.translateBatch([...prepared.values()].map((item) => item.request), {
      source_language: job.source_language,
      target_language: job.target_language,
      signal,
      onProgress,
    });
  } catch (error) {
    if (signal?.aborted) return job;
    const message = error instanceof Error ? error.message : String(error);
    return { ...job, segments: job.segments.map((segment) => prepared.has(segment.segment_id) ? { ...segment, status: "failed", error: message } : segment) };
  }
  if (signal?.aborted) return job;
  const byId = new Map(results.map((result) => [result.segment_id, result]));
  return {
    ...job,
    segments: job.segments.map((segment) => {
      const preparedItem = prepared.get(segment.segment_id);
      if (!preparedItem) return segment;
      const result = byId.get(segment.segment_id);
      if (!result) return { ...segment, status: "failed", error: "Ponudnik ni vrnil rezultata za segment." };
      if (result.error || result.translated_text === null) return { ...segment, status: "failed", error: result.error?.message ?? "Prevod ni uspel." };
      try {
        const translated_text = restoreProtectedTokens(result.translated_text, preparedItem.tokens);
        return { ...segment, translated_text, edited_translation: translated_text, status: "translated", quality: result.confidence, error: null };
      } catch (error) {
        return { ...segment, status: "failed", error: error instanceof Error ? error.message : String(error) };
      }
    }),
  };
}

export function exportTranslationJob(job: TranslationJob): string {
  return JSON.stringify(job, null, 2);
}

export function importTranslationJob(value: string): TranslationJob {
  let parsed: unknown;
  try { parsed = JSON.parse(value); }
  catch { throw new Error("Datoteka ni veljaven JSON."); }
  if (!isRecord(parsed) || parsed.schema_version !== TRANSLATION_JOB_SCHEMA_VERSION) throw new Error("Različica prevajalskega opravila ni podprta.");
  if (typeof parsed.source_language !== "string" || typeof parsed.target_language !== "string" || typeof parsed.source_filename !== "string" || !Array.isArray(parsed.glossary) || !Array.isArray(parsed.segments)) {
    throw new Error("JSON nima veljavne strukture prevajalskega opravila.");
  }
  for (const segment of parsed.segments) {
    if (!isRecord(segment) || typeof segment.segment_id !== "string" || typeof segment.block_id !== "string" || typeof segment.source_text !== "string" || typeof segment.edited_translation !== "string" || !["pending", "translating", "translated", "reviewed", "failed", "skipped"].includes(String(segment.status))) {
      throw new Error("JSON vsebuje neveljaven prevajalski segment.");
    }
  }
  return parsed as TranslationJob;
}

export function validateTranslationJobCompatibility(imported: TranslationJob, expected: TranslationJob): void {
  if (imported.source_filename !== expected.source_filename) {
    throw new Error("Opravilo pripada drugemu izvornemu dokumentu.");
  }
  if (imported.segments.length !== expected.segments.length) {
    throw new Error("Opravilo ni združljivo s trenutnim dokumentnim modelom.");
  }
  const expectedById = new Map(expected.segments.map((segment) => [segment.segment_id, segment]));
  for (const segment of imported.segments) {
    const source = expectedById.get(segment.segment_id);
    if (!source ||
      segment.page_number !== source.page_number ||
      segment.block_id !== source.block_id ||
      segment.block_type !== source.block_type ||
      segment.source_text !== source.source_text ||
      segment.source_text_origin !== source.source_text_origin) {
      throw new Error("Opravilo vsebuje spremenjene ali nezdružljive izvorne segmente.");
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
