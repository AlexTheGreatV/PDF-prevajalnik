import assert from "node:assert/strict";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfAnalysis, StructuredBlock, StructuredRegion } from "../lib/pdf-types";
import { createDefaultGlossary } from "../lib/default-glossary";
import { findGlossaryMatches } from "../lib/glossary";
import { protectTechnicalTokens, restoreTechnicalTokens } from "../lib/technical-tokens";
import {
  createTranslationJob,
  createTranslationChunks,
  editTranslation,
  exportTranslationJob,
  getSkipReason,
  importTranslationJob,
  protectForTranslation,
  resetTranslation,
  restoreProtectedTokens,
  retryFailedTranslation,
  reviewTranslation,
  splitOversizedChunk,
  translateJobBatch,
  validateTranslationJobCompatibility,
} from "../lib/translation-job";
import type { GlossaryEntry, TranslationProvider, TranslationProviderResult, TranslationRequest } from "../lib/translation-types";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

function block(reading_order: number, kind: StructuredBlock["kind"], text: string, table_cells?: string[][]): StructuredBlock {
  return { kind, bbox: { x0: 0, y0: reading_order * 10, x1: 100, y1: reading_order * 10 + 8 }, original_text: text, normalized_text: text, lines: [], line_boundaries: [], style: { font: null, font_size: 10, color: 0, flags: 0, bold: false, italic: false }, reading_order, confidence: 1, warnings: [], needs_ocr_check: false, table_cells };
}

function fixture(): PdfAnalysis {
  const regions: StructuredRegion[] = [
    { kind: "footer", bbox: { x0: 0, y0: 90, x1: 100, y1: 100 }, reading_order: 3, blocks: [block(8, "paragraph", "2")] },
    { kind: "body", bbox: { x0: 0, y0: 10, x1: 100, y1: 90 }, reading_order: 2, blocks: [
      block(2, "paragraph", "Use CO₂ at +25 °C and 2.5 MPa with 50 % coverage."),
      block(3, "list", "Features"), block(4, "list_item", "Fast curing"),
      block(5, "table", "", [["Package", "290 ml"], ["Colour", "RAL 9001"]]),
      block(6, "paragraph", "https://example.test"), block(7, "unknown", "MS65"),
    ] },
    { kind: "header", bbox: { x0: 0, y0: 0, x1: 100, y1: 10 }, reading_order: 1, blocks: [block(1, "heading", "qPPLICATIONS")] },
  ];
  return { filename: "fixture.pdf", page_count: 1, kind: "digital", extracted_text_chars: 120, pages: [], document: { metadata: {}, pages: [{ page_number: 1, width: 100, height: 100, page_type: "digital", regions, images: [] }] } };
}

function acceptedOcr(): OcrReviewItem {
  return { id: "ocr", page_number: 1, reason: "suspicious_glyph", pdf_bbox: { x0: 0, y0: 0, x1: 10, y1: 10 }, pixel_bbox: null, native_text: "qPPLICATIONS", ocr_text: "APPLICATIONS", confidence: 49, status: "accepted", selected_text_source: "ocr", effective_text: "APPLICATIONS", error: null, source: "block", linked_block_reading_order: 1, warnings: [], attempts: [], crop_image_url: null, crop_debug: null };
}

class DeterministicProvider implements TranslationProvider {
  readonly id = "test-only";
  calls = 0;
  requests: TranslationRequest[][] = [];
  constructor(private readonly result: (text: string, index: number, call: number, request: TranslationRequest) => string | Error = (text) => `SL:${text}`) {}
  async translateBatch(requests: Array<{ segment_id: string; text: string }>, options: { signal?: AbortSignal }): Promise<TranslationProviderResult[]> {
    this.calls += 1;
    this.requests.push(requests as TranslationRequest[]);
    if (options.signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    return requests.map((request, index) => {
      const value = this.result(request.text, index, this.calls, request as TranslationRequest);
      return value instanceof Error
        ? { segment_id: request.segment_id, translated_text: null, confidence: null, error: { code: "test_error", message: value.message, retryable: true } }
        : { segment_id: request.segment_id, translated_text: value, confidence: 0.9, error: null, provider_id: this.id, model_id: "test/model" };
    });
  }
}

test("creates stable document-order segments including table cells", () => {
  const first = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl", created_at: "2026-01-01T00:00:00Z" });
  const second = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl", created_at: "2026-01-01T00:00:00Z" });
  assert.deepEqual(first.segments.map((segment) => segment.segment_id), second.segments.map((segment) => segment.segment_id));
  assert.deepEqual(first.segments.slice(0, 3).map((segment) => segment.block_id), ["p1-r1-b1", "p1-r2-b2", "p1-r2-b3"]);
  assert.deepEqual(first.segments.filter((segment) => segment.block_type === "table_cell").map((segment) => segment.source_text), ["Package", "290 ml", "Colour", "RAL 9001"]);
});

test("uses accepted OCR effective text and retains native origin otherwise", () => {
  const native = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" }).segments[0];
  const ocr = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl" }).segments[0];
  assert.equal(native.source_text, "qPPLICATIONS");
  assert.equal(native.source_text_origin, "native");
  assert.equal(ocr.source_text, "APPLICATIONS");
  assert.equal(ocr.source_text_origin, "accepted_ocr");
  assert.equal(acceptedOcr().native_text, "qPPLICATIONS");
});

test("skips non-translatable content with reasons", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  assert.equal(job.segments.find((segment) => segment.source_text === "2")?.skip_reason, "Samostojna številka strani.");
  assert.ok(job.segments.find((segment) => segment.source_text.includes("https://"))?.skip_reason);
  assert.ok(job.segments.find((segment) => segment.source_text === "MS65")?.skip_reason);
  assert.ok(job.segments.find((segment) => segment.block_type === "list")?.skip_reason);
  assert.equal(getSkipReason("MS65", "paragraph"), "Samostojen tehnični identifikator.");
  assert.equal(getSkipReason("Useful text", "paragraph"), null);
});

test("protects and restores technical values exactly", () => {
  const source = "CO₂, +5 °C to +25 °C, 2.5 MPa, 50 %, RAL 9001, 290 ml, 600 ml, 20 L and 200 L.";
  const protectedValue = protectTechnicalTokens(source);
  for (const value of ["CO₂", "+5 °C", "+25 °C", "2.5 MPa", "50 %", "RAL 9001", "290 ml", "600 ml", "20 L", "200 L"]) {
    assert.ok(protectedValue.tokens.some((token) => token.value === value), `Missing protected token: ${value}`);
  }
  assert.equal(restoreTechnicalTokens(protectedValue.protected_text, protectedValue.tokens), source);
  assert.throws(() => restoreTechnicalTokens(protectedValue.protected_text.replace("TK000TK", "broken"), protectedValue.tokens), /placeholder/);
  assert.throws(() => restoreTechnicalTokens(`${protectedValue.protected_text} TK000TK`, protectedValue.tokens), /placeholder/);
});

test("finds glossary rules and restores target and do-not-translate terms", () => {
  const glossary: GlossaryEntry[] = [
    { id: "g1", source_term: "Panel Bond", target_term: "lepilo za panele", case_sensitive: true, mode: "locked_inline", enabled: true, origin: "user" },
    { id: "g2", source_term: "Zettex", target_term: "", case_sensitive: false, mode: "do_not_translate", enabled: true, origin: "user" },
  ];
  assert.equal(findGlossaryMatches("Zettex Panel Bond", glossary).length, 2);
  const protectedValue = protectForTranslation("Zettex Panel Bond", glossary);
  assert.equal(protectedValue.tokens.filter((token) => token.kind === "glossary").length, 2);
  assert.equal(restoreProtectedTokens(protectedValue.protected_text, protectedValue.tokens), "Zettex lepilo za panele");
  assert.equal(protectedValue.tokens.find((token) => token.value === "Panel Bond")?.value, "Panel Bond");
});

test("manual edit, review and reset never change source text", () => {
  const segment = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" }).segments[0];
  const edited = editTranslation(segment, "UPORABA");
  const reviewed = reviewTranslation(edited);
  assert.equal(reviewed.status, "reviewed");
  assert.equal(reviewed.source_text, segment.source_text);
  assert.equal(resetTranslation(reviewed).source_text, segment.source_text);
  assert.equal(resetTranslation(reviewed).status, "pending");
});

test("batch handles partial failure and retry without network access", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = (() => { fetchCalls += 1; throw new Error("Network forbidden"); }) as typeof fetch;
  try {
    const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
    const provider = new DeterministicProvider((text, index) => index === 1 ? new Error("Delna napaka") : `SL:${text}`);
    const translated = await translateJobBatch(job, provider);
    assert.ok(translated.segments.some((segment) => segment.status === "failed"));
    assert.ok(translated.segments.some((segment) => segment.status === "translated"));
    const failed = translated.segments.find((segment) => segment.status === "failed")!;
    assert.equal(retryFailedTranslation(failed).status, "pending");
    assert.equal(fetchCalls, 0);
  } finally { globalThis.fetch = originalFetch; }
});

test("cancelled batch returns the unchanged job", async () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const controller = new AbortController();
  controller.abort();
  const provider = new DeterministicProvider();
  assert.equal(await translateJobBatch(job, provider, controller.signal), job);
  assert.equal(provider.calls, 0);
});

test("JSON roundtrip retains work and rejects unsupported versions", () => {
  const base = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl" });
  const job = { ...base, segments: base.segments.map((segment, index) => index === 0 ? reviewTranslation(editTranslation(segment, "UPORABA")) : segment) };
  assert.deepEqual(importTranslationJob(exportTranslationJob(job)), job);
  assert.throws(() => importTranslationJob("not-json"), /veljaven JSON/);
  assert.throws(() => importTranslationJob('{"schema_version":4}'), /ni podprta/);
});

test("import compatibility rejects changes to immutable source text", () => {
  const expected = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl" });
  const imported = importTranslationJob(exportTranslationJob(expected));
  validateTranslationJobCompatibility(imported, expected);
  const tampered = { ...imported, segments: imported.segments.map((segment, index) => index === 0 ? { ...segment, source_text: "spremenjen vir" } : segment) };
  assert.throws(() => validateTranslationJobCompatibility(tampered, expected), /spremenjene ali nezdružljive/);
});

test("built-in exact segment glossary translates without automatic review", () => {
  const glossary = createDefaultGlossary();
  assert.equal(glossary.some((entry) => entry.source_term === "APPLICATION"), false);
  const job = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl", glossary });
  const applications = job.segments.find((segment) => segment.source_text === "APPLICATIONS")!;
  assert.equal(applications.translated_text, "PODROČJA UPORABE");
  assert.equal(applications.translation_origin, "glossary");
  assert.equal(applications.glossary_entry_id, "builtin-applications");
  assert.equal(applications.model_id, null);
  assert.equal(applications.status, "translated");
  assert.equal(applications.needs_review, false);
});

test("exact segment matching is case-sensitive and disabled entries use the provider", async () => {
  const glossary = createDefaultGlossary();
  const lower = glossary.map((entry) => entry.id === "builtin-applications" ? { ...entry, source_term: "applications", case_sensitive: true } : entry);
  const lowerJob = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl", glossary: lower });
  assert.equal(lowerJob.segments.find((segment) => segment.source_text === "APPLICATIONS")?.status, "pending");

  const disabled = glossary.map((entry) => entry.id === "builtin-applications" ? { ...entry, enabled: false } : entry);
  const disabledJob = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl", glossary: disabled });
  const provider = new DeterministicProvider();
  const translated = await translateJobBatch(disabledJob, provider);
  const applications = translated.segments.find((segment) => segment.source_text === "APPLICATIONS")!;
  assert.equal(applications.translation_origin, "provider");
  assert.equal(applications.model_id, "test/model");
  assert.ok(provider.requests.flat().some((request) => request.text === "APPLICATIONS"));
});

test("user exact segment override has priority over the built-in entry", () => {
  const glossary: GlossaryEntry[] = [
    ...createDefaultGlossary(),
    { id: "user-applications", source_term: "APPLICATIONS", target_term: "UPORABA PO MERI", case_sensitive: true, mode: "exact_segment", enabled: true, origin: "user" },
  ];
  const job = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl", glossary });
  const applications = job.segments.find((segment) => segment.source_text === "APPLICATIONS")!;
  assert.equal(applications.translated_text, "UPORABA PO MERI");
  assert.equal(applications.glossary_entry_id, "user-applications");
});

test("preferred glossary is advisory while do-not-translate and locked-inline are explicit", async () => {
  const glossary: GlossaryEntry[] = [
    { id: "preferred", source_term: "Panel", target_term: "plošča", case_sensitive: true, mode: "preferred", enabled: true, origin: "user" },
    { id: "keep", source_term: "Zettex", target_term: "", case_sensitive: true, mode: "do_not_translate", enabled: true, origin: "user" },
    { id: "locked", source_term: "Fast curing", target_term: "hitro utrjevanje", case_sensitive: true, mode: "locked_inline", enabled: true, origin: "user" },
  ];
  const advisory = protectForTranslation("Panel", glossary);
  assert.equal(advisory.protected_text, "Panel");
  assert.equal(advisory.tokens.length, 0);
  const protectedValue = protectForTranslation("Zettex Fast curing", glossary);
  assert.match(protectedValue.protected_text, /GLS\d{3}GLS/);
  assert.equal(restoreProtectedTokens(protectedValue.protected_text, protectedValue.tokens), "Zettex hitro utrjevanje");

  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl", glossary });
  const translated = await translateJobBatch(job, new DeterministicProvider());
  const listItem = translated.segments.find((segment) => segment.source_text === "Fast curing")!;
  assert.equal(listItem.needs_review, true);
  assert.ok(listItem.warnings.includes("locked_inline_glossary"));
});

test("placeholder retry runs once for only the damaged segment and keeps both attempts", async () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const targetId = "p1-r2-b2";
  const provider = new DeterministicProvider((text, _index, call, request) => {
    if (!request.segment_id.startsWith(targetId)) return `SL:${text}`;
    if (call === 1) return `SL:${text.replace("TK000TK", "BROKEN")}`;
    return `SL:${text}`;
  });
  const translated = await translateJobBatch(job, provider);
  const target = translated.segments.find((segment) => segment.segment_id === targetId)!;
  assert.equal(provider.calls, 2);
  assert.deepEqual(provider.requests[1].map((request) => request.segment_id), [`${targetId}-c1`]);
  assert.deepEqual(target.placeholder_attempts.map((attempt) => attempt.strategy), ["primary_tk", "retry_phx"]);
  assert.deepEqual(target.placeholder_attempts.map((attempt) => attempt.valid), [false, true]);
  assert.equal(target.status, "translated");
  assert.equal(target.needs_review, true);
  assert.ok(target.warnings.includes("placeholder_retry_succeeded"));
  assert.equal(target.source_text, job.segments.find((segment) => segment.segment_id === targetId)?.source_text);
});

test("failed placeholder retry never guesses values and remains failed", async () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const targetId = "p1-r2-b2";
  const provider = new DeterministicProvider((text, _index, _call, request) =>
    request.segment_id.startsWith(targetId)
      ? `SL:${text.replace(/(?:TK000TK|PHX000XHP)/, "BROKEN")}`
      : `SL:${text}`,
  );
  const translated = await translateJobBatch(job, provider);
  const target = translated.segments.find((segment) => segment.segment_id === targetId)!;
  assert.equal(provider.calls, 2);
  assert.equal(target.status, "failed");
  assert.equal(target.translated_text, "");
  assert.equal(target.placeholder_attempts.length, 2);
  assert.ok(target.warnings.includes("placeholder_retry_failed"));
});

test("schema one import migrates explicitly and retains immutable source identity", () => {
  const current = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const legacy = JSON.parse(exportTranslationJob(current)) as Record<string, unknown>;
  legacy.schema_version = 1;
  delete legacy.glossary_snapshot;
  legacy.glossary = [{ id: "legacy", source_term: "Zettex", target_term: "", case_sensitive: false, exact_match: true, do_not_translate: true }];
  legacy.segments = (legacy.segments as Array<Record<string, unknown>>).map((segment) => {
    const copy = { ...segment };
    for (const key of ["translation_origin", "glossary_entry_id", "model_id", "provider_id", "placeholder_attempts", "needs_review", "warnings"]) delete copy[key];
    return copy;
  });
  const migrated = importTranslationJob(JSON.stringify(legacy));
  assert.equal(migrated.schema_version, 3);
  assert.equal(migrated.glossary[0].mode, "do_not_translate");
  assert.equal(migrated.segments[0].source_text, current.segments[0].source_text);
  assert.deepEqual(migrated.glossary_snapshot, migrated.glossary);
  const tampered = { ...migrated, segments: migrated.segments.map((segment, index) => index === 0 ? { ...segment, source_text: "changed" } : segment) };
  assert.throws(() => validateTranslationJobCompatibility(tampered, current), /spremenjene/);
});

test("schema two import rejects malformed glossary provenance", () => {
  const current = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl", glossary: createDefaultGlossary() });
  const malformed = JSON.parse(exportTranslationJob(current)) as { glossary: Array<Record<string, unknown>> };
  malformed.glossary[0].mode = "replace_everywhere";
  assert.throws(() => importTranslationJob(JSON.stringify(malformed)), /neveljaven slovarski vnos/);
});

test("splits paragraph sentences deterministically and preserves separators", () => {
  const base = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" }).segments[1];
  const segment = { ...base, segment_id: "sentences", block_id: "sentences", source_text: "One. Two.\nThree." };
  const chunks = createTranslationChunks(segment);
  assert.deepEqual(chunks.map((chunk) => chunk.source_text), ["One.", "Two.", "Three."]);
  assert.deepEqual(chunks.map((chunk) => chunk.separator_after), [" ", "\n", ""]);
});

test("sentence splitting keeps abbreviations decimals URLs email and placeholders intact", () => {
  const base = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" }).segments[1];
  const source_text = "Use e.g. 2.5 MPa at https://example.test and a.b@example.test with TK004TK. Continue.";
  const chunks = createTranslationChunks({ ...base, source_text });
  assert.equal(chunks.length, 2);
  assert.equal(chunks[0].source_text, "Use e.g. 2.5 MPa at https://example.test and a.b@example.test with TK004TK.");
});

test("oversized single sentence splits only at a safe punctuation boundary", () => {
  const base = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" }).segments[1];
  const chunk = createTranslationChunks({ ...base, segment_id: "long", source_text: "First long clause, second long clause without an unsafe token split" })[0];
  const split = splitOversizedChunk(chunk)!;
  assert.equal(split.length, 2);
  assert.equal(split.map((item) => item.source_text + item.separator_after).join(""), chunk.source_text);
  assert.ok(split.every((item) => item.warnings.includes("forced_chunking")));
});

test("missing or empty chunk prevents a successful segment", async () => {
  const base = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const missing: TranslationProvider = { id: "missing", async translateBatch(requests) { return requests.slice(1).map((request) => ({ segment_id: request.segment_id, translated_text: "Prevod", confidence: null, error: null })); } };
  const missingResult = await translateJobBatch(base, missing);
  assert.ok(missingResult.segments.some((segment) => segment.status === "failed" && segment.chunks.some((chunk) => chunk.warnings.includes("missing_chunk_result"))));
  const empty: TranslationProvider = { id: "empty", async translateBatch(requests) { return requests.map((request) => ({ segment_id: request.segment_id, translated_text: "", confidence: null, error: null })); } };
  const emptyResult = await translateJobBatch(base, empty);
  assert.ok(emptyResult.segments.some((segment) => segment.status === "failed" && segment.chunks.some((chunk) => chunk.warnings.includes("empty_chunk_result"))));
});

test("duplicate chunk result prevents a successful segment", async () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const provider: TranslationProvider = {
    id: "duplicate-result",
    async translateBatch(requests) {
      const result = { segment_id: requests[0].segment_id, translated_text: "Prevod.", confidence: null, error: null };
      return [result, result];
    },
  };
  const translated = await translateJobBatch(job, provider);
  const segment = translated.segments.find((item) => item.status === "failed");
  assert.ok(segment);
  assert.ok(segment.chunks.some((chunk) => chunk.warnings.includes("duplicate_chunk_result")));
});

test("decoding limit triggers one safe split and keeps the segment for review", async () => {
  const base = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const target = base.segments.find((segment) => segment.block_type === "paragraph" && !segment.skip_reason)!;
  const job = { ...base, segments: base.segments.map((segment) => segment.segment_id === target.segment_id ? { ...segment, source_text: "First clause, second clause without a full stop" } : segment) };
  let calls = 0;
  const provider: TranslationProvider = {
    id: "limit",
    async translateBatch(requests) {
      calls += 1;
      return requests.map((request) => calls === 1 && request.segment_id.startsWith(target.segment_id)
        ? { segment_id: request.segment_id, translated_text: null, confidence: null, error: { code: "decoding_limit", message: "limit", retryable: true }, source_token_count: 400, output_token_count: 64, max_decoding_length: 64, reached_decoding_limit: true }
        : { segment_id: request.segment_id, translated_text: `SL:${request.text}`, confidence: null, error: null, source_token_count: 10, output_token_count: 12, max_decoding_length: 64, reached_decoding_limit: false, has_end_token: true });
    },
  };
  const translated = await translateJobBatch(job, provider);
  const result = translated.segments.find((segment) => segment.segment_id === target.segment_id)!;
  assert.equal(calls, 2);
  assert.equal(result.status, "translated");
  assert.equal(result.forced_chunking, true);
  assert.equal(result.needs_review, true);
  assert.equal(result.chunks.length, 2);
});

test("exact segment glossary bypasses model chunks", async () => {
  const job = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl", glossary: createDefaultGlossary() });
  const provider = new DeterministicProvider();
  const translated = await translateJobBatch(job, provider);
  const exact = translated.segments.find((segment) => segment.source_text === "APPLICATIONS")!;
  assert.equal(exact.translation_origin, "glossary");
  assert.equal(exact.chunks.length, 0);
  assert.ok(!provider.requests.flat().some((request) => request.context.segment_id === exact.segment_id));
});

test("schema two job migrates chunk fields without changing source identity", () => {
  const current = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const legacy = JSON.parse(exportTranslationJob(current)) as Record<string, unknown>;
  legacy.schema_version = 2;
  legacy.segments = (legacy.segments as Array<Record<string, unknown>>).map((segment) => {
    const legacySegment = { ...segment };
    delete legacySegment.chunks;
    delete legacySegment.forced_chunking;
    return legacySegment;
  });
  const migrated = importTranslationJob(JSON.stringify(legacy));
  assert.equal(migrated.schema_version, 3);
  assert.equal(migrated.segments[0].source_text, current.segments[0].source_text);
  assert.deepEqual(migrated.segments[0].chunks, []);
});

test("cancellation during chunk translation returns the unchanged job", async () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const controller = new AbortController();
  const provider: TranslationProvider = { id: "cancel-midway", async translateBatch(requests) { controller.abort(); return requests.map((request) => ({ segment_id: request.segment_id, translated_text: request.text, confidence: null, error: null })); } };
  assert.equal(await translateJobBatch(job, provider, controller.signal), job);
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
