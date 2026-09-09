import assert from "node:assert/strict";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfAnalysis, StructuredBlock, StructuredRegion } from "../lib/pdf-types";
import { findGlossaryMatches } from "../lib/glossary";
import { protectTechnicalTokens, restoreTechnicalTokens } from "../lib/technical-tokens";
import {
  createTranslationJob,
  editTranslation,
  exportTranslationJob,
  getSkipReason,
  importTranslationJob,
  protectForTranslation,
  resetTranslation,
  retryFailedTranslation,
  reviewTranslation,
  translateJobBatch,
  validateTranslationJobCompatibility,
} from "../lib/translation-job";
import type { GlossaryEntry, TranslationProvider, TranslationProviderResult } from "../lib/translation-types";

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
  constructor(private readonly result: (text: string, index: number) => string | Error = (text) => `SL:${text}`) {}
  async translateBatch(requests: Array<{ segment_id: string; text: string }>, options: { signal?: AbortSignal }): Promise<TranslationProviderResult[]> {
    this.calls += 1;
    if (options.signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    return requests.map((request, index) => {
      const value = this.result(request.text, index);
      return value instanceof Error
        ? { segment_id: request.segment_id, translated_text: null, confidence: null, error: { code: "test_error", message: value.message, retryable: true } }
        : { segment_id: request.segment_id, translated_text: value, confidence: 0.9, error: null };
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
  assert.throws(() => restoreTechnicalTokens(protectedValue.protected_text.replace("__TECH_000__", "broken"), protectedValue.tokens), /placeholder/);
  assert.throws(() => restoreTechnicalTokens(`${protectedValue.protected_text} __TECH_000__`, protectedValue.tokens), /placeholder/);
});

test("finds glossary rules and protects do-not-translate terms", () => {
  const glossary: GlossaryEntry[] = [{ id: "g1", source_term: "Panel Bond", target_term: "lepilo za panele", case_sensitive: true, exact_match: true, do_not_translate: false }, { id: "g2", source_term: "Zettex", target_term: "", case_sensitive: false, exact_match: true, do_not_translate: true }];
  assert.equal(findGlossaryMatches("Zettex Panel Bond", glossary).length, 2);
  const protectedValue = protectForTranslation("Zettex Panel Bond", glossary);
  assert.match(protectedValue.protected_text, /__GLOSSARY_/);
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

test("JSON roundtrip retains work and rejects invalid versions", () => {
  const base = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl" });
  const job = { ...base, segments: base.segments.map((segment, index) => index === 0 ? reviewTranslation(editTranslation(segment, "UPORABA")) : segment) };
  assert.deepEqual(importTranslationJob(exportTranslationJob(job)), job);
  assert.throws(() => importTranslationJob("not-json"), /veljaven JSON/);
  assert.throws(() => importTranslationJob('{"schema_version":2}'), /ni podprta/);
});

test("import compatibility rejects changes to immutable source text", () => {
  const expected = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl" });
  const imported = importTranslationJob(exportTranslationJob(expected));
  validateTranslationJobCompatibility(imported, expected);
  const tampered = { ...imported, segments: imported.segments.map((segment, index) => index === 0 ? { ...segment, source_text: "spremenjen vir" } : segment) };
  assert.throws(() => validateTranslationJobCompatibility(tampered, expected), /spremenjene ali nezdružljive/);
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
