import assert from "node:assert/strict";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfAnalysis, StructuredBlock, StructuredRegion } from "../lib/pdf-types";
import { createDefaultGlossary } from "../lib/default-glossary";
import { findGlossaryMatches } from "../lib/glossary";
import { protectTechnicalTokens, restoreTechnicalTokens } from "../lib/technical-tokens";
import { TECHNICAL_RANGE_RULES_VERSION } from "../lib/technical-ranges";
import {
  bindTranslationJobToAnalysis,
  createTranslationJob,
  createTranslationChunks,
  editTranslation,
  exportTranslationJob,
  getSkipReason,
  importTranslationJob,
  invalidateStaleOcrApprovals,
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
import { updateTerminologySources } from "../lib/translation-profile-evaluation";

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
  return {
    filename: "fixture.pdf",
    sha256: "a".repeat(64),
    page_count: 1,
    document_model_version: 1,
    kind: "digital",
    extracted_text_chars: 120,
    pages: [{ page_number: 1, kind: "digital", extracted_text_chars: 120, dimensions: { width: 100, height: 100, unit: "pt" }, rotation: 0, text_blocks: [], image_count: 0 }],
    document: { metadata: {}, pages: [{ page_number: 1, width: 100, height: 100, rotation: 0, page_type: "digital", regions, images: [] }] },
  };
}

function acceptedOcr(): OcrReviewItem {
  return { id: "ocr", page_number: 1, reason: "suspicious_glyph", pdf_bbox: { x0: 0, y0: 0, x1: 10, y1: 10 }, pixel_bbox: null, native_text: "qPPLICATIONS", ocr_text: "APPLICATIONS", confidence: 49, status: "accepted", selected_text_source: "ocr", effective_text: "APPLICATIONS", error: null, source: "block", linked_block_reading_order: 1, warnings: [], attempts: [], crop_image_url: null, crop_debug: null, server_revision_id: "server-revision" };
}

function acceptedManualOcr(): OcrReviewItem {
  return { ...acceptedOcr(), id: "manual-ocr", linked_block_reading_order: undefined, source: "manual", reason: "manual_selection", native_text: "", ocr_text: "Warranty period", effective_text: "Warranty period", pdf_bbox: { x0: 20, y0: 20, x1: 70, y1: 35 } };
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

test("adds an accepted manual OCR region as a separate unreviewed translation segment", () => {
  const job = createTranslationJob(fixture(), [acceptedManualOcr()], { source_language: "en", target_language: "sl" });
  const segment = job.segments.find((item) => item.segment_id === "ocr-manual-ocr")!;
  assert.equal(segment.source_text_origin, "accepted_ocr");
  assert.equal(segment.source_text, "Warranty period");
  assert.deepEqual(segment.source_bbox, acceptedManualOcr().pdf_bbox);
  assert.equal(segment.accepted_ocr_source?.review_id, "manual-ocr");
  assert.notEqual(segment.status, "reviewed");
});

test("changing or rejecting an OCR source invalidates its old translation approval", () => {
  const item = acceptedManualOcr();
  const base = createTranslationJob(fixture(), [item], { source_language: "en", target_language: "sl" });
  const reviewed = { ...base, segments: base.segments.map((segment) => segment.segment_id === "ocr-manual-ocr" ? reviewTranslation(editTranslation(segment, "Garancijsko obdobje")) : segment) };
  const changed = { ...item, effective_text: "Changed OCR", ocr_text: "Changed OCR", pdf_bbox: { ...item.pdf_bbox, x1: 75 } };
  const invalidated = invalidateStaleOcrApprovals(reviewed, [changed]);
  const segment = invalidated.segments.find((entry) => entry.segment_id === "ocr-manual-ocr")!;
  assert.equal(segment.status, "translated");
  assert.equal(segment.needs_review, true);
  assert.equal(segment.source_text, "Changed OCR");
  assert.equal(segment.edited_translation, "Garancijsko obdobje");
  assert.ok(segment.warnings.includes("ocr_source_changed_after_review"));

  const rejected = invalidateStaleOcrApprovals(reviewed, [{ ...item, status: "rejected", selected_text_source: "native" }]);
  assert.notEqual(rejected.segments.find((entry) => entry.segment_id === "ocr-manual-ocr")?.status, "reviewed");
  assert.equal(rejected.segments.find((entry) => entry.segment_id === "ocr-manual-ocr")?.accepted_ocr_source, null);
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
  for (const value of ["CO₂", "+5 °C to +25 °C", "2.5 MPa", "50 %", "RAL 9001", "290 ml", "600 ml", "20 L", "200 L"]) {
    assert.ok(protectedValue.tokens.some((token) => token.value === value), `Missing protected token: ${value}`);
  }
  assert.equal(restoreTechnicalTokens(protectedValue.protected_text, protectedValue.tokens), "CO₂, +5\u00a0°C do +25\u00a0°C, 2,5\u00a0MPa, 50\u00a0%, RAL 9001, 290\u00a0ml, 600\u00a0ml, 20\u00a0L and 200\u00a0L.");
  assert.throws(() => restoreTechnicalTokens(protectedValue.protected_text.replace("TK000TK", "broken"), protectedValue.tokens), /placeholder/);
  assert.throws(() => restoreTechnicalTokens(`${protectedValue.protected_text} TK000TK`, protectedValue.tokens), /placeholder/);
});

test("preserves technical symbols and applies nonbreaking unit spacing", () => {
  const source = "Strength 4 MPa, packs 290 ml and 20 L, CO₂, RAL 9001.";
  const result = protectTechnicalTokens(source);
  assert.equal(restoreTechnicalTokens(result.protected_text, result.tokens), "Strength 4\u00a0MPa, packs 290\u00a0ml and 20\u00a0L, CO₂, RAL 9001.");
  assert.equal(result.tokens.find((token) => token.value === "CO₂")?.replacement_value, "CO₂");
  assert.equal(result.tokens.find((token) => token.value === "RAL 9001")?.replacement_value, "RAL 9001");
});

test("normalizes only an unambiguous plain decimal and records the profile rule", async () => {
  const plain = protectTechnicalTokens("Ratio 3.14.");
  assert.equal(restoreTechnicalTokens(plain.protected_text, plain.tokens), "Ratio 3,14.");
  assert.equal(plain.tokens.find((token) => token.value === "3.14")?.rule_id, "decimal_separator");

  for (const source of ["version 1.2.3", "code AB-3.14", "identifier v1.2", "IP 192.168.1.59", "ISO 1.59", "DIN 3.14", "serial 1.59"]) {
    const protectedValue = protectTechnicalTokens(source);
    assert.equal(restoreTechnicalTokens(protectedValue.protected_text, protectedValue.tokens), source);
    assert.equal(protectedValue.tokens.some((token) => token.rule_id === "decimal_separator"), false);
  }

  const analysis = fixture();
  analysis.document!.pages[0].regions = [{ kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1, blocks: [block(1, "paragraph", "Ratio 3.14.")] }];
  const translated = await translateJobBatch(
    createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" }),
    new DeterministicProvider(() => "Razmerje TK000TK."),
  );
  assert.equal(translated.segments[0].translated_text, "Razmerje 3,14.");
  assert.ok(translated.translation_profile?.applied_rule_ids.includes("SL-SSKJ-05-001"));
  assert.notEqual(translated.segments[0].status, "reviewed");
});

test("protects formulas codes URLs email and source placeholders", () => {
  const source = "H₂O CO₂ MS65 RAL 9001 https://example.test a@example.test {name} ${value} %s %d {{variable}} [LINK]";
  const protectedValue = protectTechnicalTokens(source);
  assert.equal(restoreTechnicalTokens(protectedValue.protected_text, protectedValue.tokens), source);
  for (const expected of ["H₂O", "CO₂", "MS65", "RAL 9001", "https://example.test", "a@example.test", "{name}", "${value}", "%s", "%d", "{{variable}}", "[LINK]"]) {
    assert.ok(protectedValue.tokens.some((token) => token.value === expected), `Missing protected token: ${expected}`);
  }
  assert.equal(protectedValue.tokens.filter((token) => token.kind === "source_placeholder").length, 6);
});

test("parses and localizes only structured technical ranges", () => {
  const cases = [
    ["-40 °C to +90 °C", "−40\u00a0°C do +90\u00a0°C", "temperature"],
    ["from -40 °C to +90 °C", "od −40\u00a0°C do +90\u00a0°C", "temperature"],
    ["5 mm to 10 mm", "5\u00a0mm do 10\u00a0mm", "measurement"],
    ["10 % to 20 %", "10\u00a0% do 20\u00a0%", "percentage"],
    ["between 5 MPa and 10 MPa", "med 5\u00a0MPa in 10\u00a0MPa", "measurement"],
    ["5.5-10.25 cm", "5,5 do 10,25\u00a0cm", "measurement"],
  ] as const;
  for (const [source, expected, kind] of cases) {
    const result = protectTechnicalTokens(source);
    const range = result.tokens.find((token) => token.kind === "technical_range");
    assert.equal(range?.technical_range?.kind, kind);
    assert.equal(range?.rule_version, TECHNICAL_RANGE_RULES_VERSION);
    assert.equal(restoreTechnicalTokens(result.protected_text, result.tokens), expected);
  }
});

test("leaves word units to the model while protecting range semantics", () => {
  const cases = [
    ["10 to 20 minutes", "TK000TK TK001TK minutes TK002TK", "TK000TK TK001TK minut TK002TK", "10 do 20\u00a0minut", "end_only"],
    ["from 1 to 2 hours", "TK000TK TK001TK hours TK002TK", "TK000TK TK001TK ur TK002TK", "od 1 do 2\u00a0ur", "end_only"],
    ["between 5 and 10 days", "TK000TK TK001TK days TK002TK", "TK000TK TK001TK dnevi TK002TK", "med 5 in 10\u00a0dnevi", "end_only"],
    ["1 hour to 2 hours", "TK000TK TK001TK hours TK002TK", "TK000TK TK001TK uri TK002TK", "1 do 2\u00a0uri", "both"],
    ["1 to 1 day", "TK000TK TK001TK day TK002TK", "TK000TK TK001TK dan TK002TK", "1 do 1\u00a0dan", "end_only"],
    ["1-2 months", "TK000TK TK001TK months TK002TK", "TK000TK TK001TK meseca TK002TK", "1 do 2\u00a0meseca", "end_only"],
  ] as const;
  for (const [source, protectedText, providerText, expected, placement] of cases) {
    const result = protectTechnicalTokens(source);
    assert.equal(result.protected_text, protectedText);
    assert.equal(result.tokens[0].technical_range?.unit_kind, "translatable_word");
    assert.equal(result.tokens[0].technical_range?.unit_placement, placement);
    assert.equal(restoreTechnicalTokens(providerText, result.tokens), expected);
  }

  const sentence = protectTechnicalTokens("Curing takes 1 to 2 months in normal conditions.");
  assert.equal(sentence.protected_text, "Curing takes TK000TK TK001TK months TK002TK in normal conditions.");
  assert.equal(
    restoreTechnicalTokens("Strjevanje v običajnih pogojih traja TK000TK TK001TK meseca TK002TK.", sentence.tokens),
    "Strjevanje v običajnih pogojih traja 1 do 2\u00a0meseca.",
  );

  assert.equal(
    restoreTechnicalTokens("Meseca TK001TK mesecih TK002TK sta pred TK000TK.", sentence.tokens),
    "Meseca sta pred 1 do 2\u00a0mesecih.",
  );
});

test("rejects missing or untranslated word units instead of guessing", () => {
  const result = protectTechnicalTokens("10 to 20 minutes");
  assert.throws(() => restoreTechnicalTokens("TK000TK TK001TK TK002TK", result.tokens), /enota/);
  assert.throws(() => restoreTechnicalTokens("TK000TK TK001TK minutes TK002TK", result.tokens), /izvornem jeziku/);
  assert.throws(() => restoreTechnicalTokens("TK000TK TK001TK minute TK002TK", result.tokens), /izvornem jeziku/);

  const unknown = protectTechnicalTokens("10 to 20 fortnights");
  assert.equal(unknown.tokens.some((token) => token.kind === "technical_range"), false);
  assert.ok(unknown.warnings.includes("technical_range_ambiguous"));
});

test("does not rewrite ordinary to and flags ambiguous ranges for review", () => {
  const ordinary = protectTechnicalTokens("Apply the product to the clean surface.");
  assert.equal(ordinary.tokens.some((token) => token.kind === "technical_range"), false);
  assert.equal(restoreTechnicalTokens(ordinary.protected_text, ordinary.tokens), "Apply the product to the clean surface.");

  const ambiguous = protectTechnicalTokens("Pressure from 5 MPa to 10 kPa.");
  assert.equal(ambiguous.tokens.some((token) => token.kind === "technical_range"), false);
  assert.ok(ambiguous.warnings.includes("technical_range_ambiguous"));

  const unrelatedNumbers = protectTechnicalTokens("Move item 10 to shelf 20.");
  assert.equal(unrelatedNumbers.tokens.some((token) => token.kind === "technical_range"), false);
  assert.ok(unrelatedNumbers.warnings.includes("technical_range_ambiguous"));
});

test("fails a translated segment when a word unit is lost or left in English", async () => {
  const source = "Drying takes 10 to 20 minutes.";
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{ kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1, blocks: [block(1, "paragraph", source)] }];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  const provider = new DeterministicProvider(() => "Sušenje traja TK000TK minutes.");
  const translated = await translateJobBatch(job, provider);
  assert.equal(translated.segments[0].status, "failed");
  assert.equal(translated.segments[0].source_text, source);
  assert.ok(translated.segments[0].warnings.includes("placeholder_integrity"));
});

test("records versioned technical range adjustments without automatic review", async () => {
  const source = "Temperature resistance -40 °C to +90 °C";
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{ kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1, blocks: [block(1, "paragraph", source)] }];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  const provider = new DeterministicProvider(() => "Temperaturna odpornost TK000TK");
  const translated = await translateJobBatch(job, provider);
  const segment = translated.segments[0];
  assert.equal(segment.source_text, source);
  assert.equal(segment.translated_text, "Temperaturna odpornost −40\u00a0°C do +90\u00a0°C");
  assert.equal(segment.status, "translated");
  assert.equal(segment.needs_review, true);
  assert.equal(segment.translation_rules_version, TECHNICAL_RANGE_RULES_VERSION);
  assert.deepEqual(segment.translation_rule_adjustments, [{
    rule_id: "technical_range",
    rule_version: TECHNICAL_RANGE_RULES_VERSION,
    kind: "temperature",
    source_text: "-40 °C to +90 °C",
    replacement_text: "−40\u00a0°C do +90\u00a0°C",
  }]);
  assert.ok(segment.warnings.includes("technical_translation_rule_applied"));
  assert.equal(segment.status, "translated");
  assert.notEqual(segment.status, "reviewed");
  assert.equal(translated.translation_profile?.profile_id, "sl-general-sskj-v1");
  assert.ok(translated.translation_profile?.applied_rule_ids.includes("SL-SSKJ-05-004"));
  assert.ok(translated.translation_profile?.applied_rule_ids.includes("SL-SSKJ-05-006"));
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

test("protects complete do-not-translate names before technical values", async () => {
  const glossary: GlossaryEntry[] = [{
    id: "product-name",
    source_term: "ACME MS 65 Panel Bond",
    target_term: "ACME MS 65 Panel Bond",
    case_sensitive: false,
    mode: "do_not_translate",
    enabled: true,
    origin: "user",
    scope: "persistent_user",
    revision: 2,
  }];
  const source = "Acme MS 65 Panel Bond";
  for (const value of ["ACME MS 65 Panel Bond", source]) {
    for (const strategy of ["primary_tk", "retry_phx"] as const) {
      const protectedValue = protectForTranslation(value, glossary, strategy);
      assert.equal(protectedValue.tokens.filter((token) => token.kind === "glossary").length, 1);
      assert.equal(protectedValue.tokens.filter((token) => token.kind !== "glossary").length, 0);
      assert.equal(restoreProtectedTokens(protectedValue.protected_text, protectedValue.tokens), value);
    }
  }

  const sentence = protectForTranslation(`${source} at 2.5 MPa.`, glossary);
  assert.equal(sentence.tokens.filter((token) => token.kind === "glossary").length, 1);
  assert.equal(sentence.tokens.filter((token) => token.kind === "measurement").length, 1);
  assert.equal(restoreProtectedTokens(sentence.protected_text, sentence.tokens), `${source} at 2,5\u00a0MPa.`);

  const analysis = fixture();
  analysis.document!.pages[0].regions[1].blocks = [
    block(1, "heading", source),
    block(2, "paragraph", "Panel adhesive"),
  ];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl", glossary });
  const provider = new DeterministicProvider((text) => text.includes("Panel adhesive") ? "Panelno lepilo" : text);
  const translated = await translateJobBatch(job, provider);
  const product = translated.segments.find((segment) => segment.source_text === source)!;
  const description = translated.segments.find((segment) => segment.source_text === "Panel adhesive")!;
  assert.equal(product.translated_text, source);
  assert.equal(product.status, "translated");
  assert.equal(product.needs_review, true);
  assert.equal(product.source_text, source);
  assert.equal(product.source_text_origin, "native");
  assert.equal(product.accepted_ocr_source, null);
  assert.equal(product.glossary_matches[0].glossary_id, "product-name");
  assert.equal(product.glossary_matches[0].revision, 2);
  assert.equal(translated.glossary_usage.find((usage) => usage.glossary_id === "product-name")?.revision, 2);
  assert.equal(description.translated_text, "Panelno lepilo");
  assert.equal(description.glossary_matches.length, 0);
});

test("uses longest glossary match within precedence and keeps placeholder namespaces isolated", () => {
  const glossary: GlossaryEntry[] = [
    { id: "short", source_term: "ACME", target_term: "", case_sensitive: false, mode: "do_not_translate", enabled: true, origin: "user", scope: "persistent_user" },
    { id: "long", source_term: "ACME MS 65 Panel Bond", target_term: "", case_sensitive: false, mode: "do_not_translate", enabled: true, origin: "user", scope: "persistent_user" },
  ];
  const source = "ACME MS 65 Panel Bond at 2.5 MPa";
  const protectedValue = protectForTranslation(source, glossary);
  const glossaryTokens = protectedValue.tokens.filter((token) => token.kind === "glossary");
  const technicalTokens = protectedValue.tokens.filter((token) => token.kind !== "glossary");
  assert.deepEqual(glossaryTokens.map((token) => token.value), ["ACME MS 65 Panel Bond"]);
  assert.match(glossaryTokens[0].placeholder, /^GLS\d+GLS$/);
  assert.ok(technicalTokens.every((token) => /^TK\d+TK$/.test(token.placeholder)));
  assert.ok(!technicalTokens.some((token) => token.placeholder === glossaryTokens[0].placeholder));
  assert.equal(restoreProtectedTokens(protectedValue.protected_text, protectedValue.tokens), "ACME MS 65 Panel Bond at 2,5\u00a0MPa");
});

test("preserves literal placeholder-like text and rejects damaged generated placeholders", () => {
  const glossary: GlossaryEntry[] = [{
    id: "keep", source_term: "ACME MS 65 Panel Bond", target_term: "", case_sensitive: true,
    mode: "do_not_translate", enabled: true, origin: "user", scope: "persistent_user",
  }];
  const literal = "TK000TK GLS000GLS GPH000HPG PHX000XHP";
  const literalProtected = protectForTranslation(literal, []);
  assert.equal(restoreProtectedTokens(literalProtected.protected_text, literalProtected.tokens), literal);

  const protectedValue = protectForTranslation(`GLS000GLS ACME MS 65 Panel Bond at 2.5 MPa`, glossary);
  const glossaryToken = protectedValue.tokens.find((token) => token.kind === "glossary")!;
  const technicalToken = protectedValue.tokens.find((token) => token.kind !== "glossary")!;
  assert.notEqual(glossaryToken.placeholder, "GLS000GLS");
  assert.throws(() => restoreProtectedTokens(protectedValue.protected_text.replace(glossaryToken.placeholder, ""), protectedValue.tokens), /slovarski placeholder/);
  assert.throws(() => restoreProtectedTokens(`${protectedValue.protected_text} ${glossaryToken.placeholder}`, protectedValue.tokens), /slovarski placeholder/);
  assert.throws(() => restoreProtectedTokens(protectedValue.protected_text.replace(technicalToken.placeholder, ""), protectedValue.tokens), /tehnični placeholder/);
  assert.throws(() => restoreProtectedTokens(`${protectedValue.protected_text} ${technicalToken.placeholder}`, protectedValue.tokens), /tehnični placeholder/);
});

test("exact glossary results cannot remove or change technical values", () => {
  const source = "Specification 65, 2.5 MPa, 20 L, RAL 9001";
  const analysis = fixture();
  analysis.document!.pages[0].regions[1].blocks = [block(1, "paragraph", source)];
  const entry = (target_term: string): GlossaryEntry => ({
    id: "technical-exact", source_term: source, target_term, case_sensitive: true,
    mode: "exact_segment", enabled: true, origin: "user", scope: "persistent_user", revision: 3,
  });

  const valid = createTranslationJob(analysis, [], {
    source_language: "en", target_language: "sl", glossary: [entry("Specifikacija 65, 2.5 MPa, 20 L, RAL 9001")],
  }).segments.find((segment) => segment.source_text === source)!;
  assert.equal(valid.status, "translated");
  assert.equal(valid.translated_text, "Specifikacija 65, 2,5\u00a0MPa, 20\u00a0L, RAL 9001");
  assert.equal(valid.needs_review, true);

  const invalid = createTranslationJob(analysis, [], {
    source_language: "en", target_language: "sl", glossary: [entry("Specifikacija 2.5 MPa, 10 L, RAL 9001")],
  }).segments.find((segment) => segment.source_text === source)!;
  assert.equal(invalid.status, "failed");
  assert.equal(invalid.translated_text, "");
  assert.equal(invalid.edited_translation, "");
  assert.equal(invalid.needs_review, true);
  assert.ok(invalid.warnings.includes("glossary_technical_integrity"));
  assert.match(invalid.error ?? "", /tehničnih vrednosti/);

  const locked = protectForTranslation("Use strength 2.5 MPa.", [{
    id: "invalid-inline", source_term: "strength 2.5 MPa", target_term: "trdnost 1 MPa",
    case_sensitive: true, mode: "locked_inline", enabled: true, origin: "user", scope: "document",
  }]);
  assert.throws(() => restoreProtectedTokens(locked.protected_text, locked.tokens), /tehničnih vrednosti/);
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
  assert.throws(() => importTranslationJob('{"schema_version":7}'), /ni podprta/);
});

test("schema four import stays compatible without claiming the new profile", () => {
  const current = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const legacy = JSON.parse(exportTranslationJob(current)) as Record<string, unknown>;
  legacy.schema_version = 4;
  delete legacy.translation_profile;
  delete legacy.quality_checklist;
  const migrated = importTranslationJob(JSON.stringify(legacy));
  assert.equal(migrated.schema_version, 6);
  assert.equal(migrated.translation_profile, null);
  assert.deepEqual(migrated.quality_checklist, []);
  assert.equal(migrated.segments[0].source_text, current.segments[0].source_text);
});

test("runtime rule applicability is separate from static execution mode", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const evaluations = job.translation_profile!.rule_evaluations;
  assert.equal(evaluations.length, 121);
  assert.ok(evaluations.filter((item) => item.rule_id.startsWith("SL-SSKJ-09-")).every((item) => item.applicability === "not_applicable" && item.status === "not_applicable"));
  assert.ok(evaluations.filter((item) => item.rule_id.startsWith("SL-SSKJ-11-")).every((item) => item.applicability === "not_applicable" && item.status === "not_applicable"));
  assert.equal(evaluations.find((item) => item.rule_id === "SL-SSKJ-13-004")?.status, "not_applicable");
  const suspicious = evaluations.find((item) => item.rule_id === "SL-SSKJ-04-001")!;
  assert.equal(suspicious.applicability, "applicable");
  assert.equal(suspicious.status, "needs_review");
  assert.equal(suspicious.segment_ids.length, 1);
  assert.ok(evaluations.some((item) => item.status === "not_checkable"));
  assert.ok(evaluations.some((item) => item.status === "not_applicable"));
  assert.ok(job.translation_profile!.manual_review_rule_ids.length < 121);
  assert.equal(job.translation_profile!.diagnostics.filter((item) => item.message === "Terminološki vir ni preverjen").length, 1);
});

test("ambiguous range warning is attached to its concrete segment", async () => {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{ kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1, blocks: [block(1, "paragraph", "Coverage is 10 to 20 fortnights.")] }];
  const translated = await translateJobBatch(
    createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" }),
    new DeterministicProvider(() => "Pokritost je 10 do 20 štirinajstdnevnih obdobij."),
  );
  const warning = translated.translation_profile!.diagnostics.find((item) => item.rule_id === "SL-SSKJ-05-005")!;
  assert.equal(warning.segment_id, translated.segments[0].segment_id);
  assert.equal(warning.source_text, "Coverage is 10 to 20 fortnights.");
});

test("unknown and changed profiles preserve work while invalidating only stale evaluation", () => {
  const original = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const edited = { ...original, segments: original.segments.map((segment, index) => index === 0 ? editTranslation(segment, "Ročno urejen prevod") : segment) };

  const future = JSON.parse(exportTranslationJob(edited));
  future.translation_profile.profile_id = "sl-future-v9";
  future.translation_profile.profile_version = 9;
  const importedFuture = importTranslationJob(JSON.stringify(future));
  assert.equal(importedFuture.translation_profile?.evaluation_status, "unknown_profile");
  assert.equal(importedFuture.segments[0].source_text, edited.segments[0].source_text);
  assert.equal(importedFuture.segments[0].edited_translation, "Ročno urejen prevod");
  assert.equal(importedFuture.translation_profile?.profile_id, "sl-future-v9");

  const changed = JSON.parse(exportTranslationJob(edited));
  changed.translation_profile.canonical_sha256 = "b".repeat(64);
  const importedChanged = importTranslationJob(JSON.stringify(changed));
  assert.equal(importedChanged.translation_profile?.evaluation_status, "stale");
  assert.deepEqual(importedChanged.translation_profile?.rule_evaluations, []);
  assert.deepEqual(importedChanged.translation_profile?.applied_rule_ids, []);
  assert.equal(importedChanged.segments[0].source_text, edited.segments[0].source_text);
  assert.equal(importedChanged.segments[0].edited_translation, "Ročno urejen prevod");
});

test("user glossary conflict follows section 15 and remains unreviewed", () => {
  const glossary: GlossaryEntry[] = [
    ...createDefaultGlossary(),
    { id: "user-advantages", source_term: "ADVANTAGES", target_term: "KORISTI", case_sensitive: false, mode: "exact_segment", enabled: true, origin: "user" },
  ];
  const analysis = fixture();
  const heading = analysis.document!.pages[0].regions.find((region) => region.kind === "header")!.blocks[0];
  heading.original_text = "ADVANTAGES";
  heading.normalized_text = "ADVANTAGES";
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl", glossary });
  const segment = job.segments[0];
  assert.equal(segment.edited_translation, "KORISTI");
  assert.equal(segment.status, "translated");
  assert.notEqual(segment.status, "reviewed");
  assert.equal(job.translation_profile?.conflicts[0]?.winner, "user_glossary");
});

test("inconsistent terminology produces a warning and profile data survives JSON", () => {
  const glossary: GlossaryEntry[] = [
    { id: "one", source_term: "coating", target_term: "premaz", case_sensitive: false, mode: "preferred", enabled: true, origin: "user" },
    { id: "two", source_term: "Coating", target_term: "obloga", case_sensitive: false, mode: "preferred", enabled: true, origin: "user" },
  ];
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl", glossary });
  assert.ok(job.translation_profile?.diagnostics.some((diagnostic) => diagnostic.rule_id === "SL-SSKJ-14-001"));
  const imported = importTranslationJob(exportTranslationJob(job));
  assert.deepEqual(imported.translation_profile, job.translation_profile);
  assert.deepEqual(imported.quality_checklist, job.quality_checklist);
});

test("a user-confirmed terminology citation is retained without an external lookup", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const updated = updateTerminologySources(job, [{
    selected_term: "premaz",
    domain: "gradbeništvo",
    source_or_citation: "Interni potrjeni glosar, izdaja 2026-09",
    confidence: "high",
    user_confirmed: true,
  }]);
  assert.equal(updated.translation_profile?.terminology_sources[0].selected_term, "premaz");
  assert.equal(updated.translation_profile?.diagnostics.some((item) => item.message === "Terminološki vir ni preverjen"), false);
  const imported = importTranslationJob(exportTranslationJob(updated));
  assert.deepEqual(imported.translation_profile?.terminology_sources, updated.translation_profile?.terminology_sources);
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

test("document glossary outranks approved persistent and built-in entries with auditable usage", () => {
  const glossary: GlossaryEntry[] = [
    ...createDefaultGlossary(),
    { id: "persistent-applications", source_term: "APPLICATIONS", target_term: "TRAJNA UPORABA", case_sensitive: true, mode: "exact_segment", enabled: true, origin: "user", scope: "persistent_user", status: "approved", revision: 4 },
    { id: "document-applications", source_term: "APPLICATIONS", target_term: "DOKUMENTNA UPORABA", case_sensitive: true, mode: "exact_segment", enabled: true, origin: "user", scope: "document", status: "approved", revision: 2 },
  ];
  const job = createTranslationJob(fixture(), [acceptedOcr()], {
    source_language: "en", target_language: "sl", glossary, glossary_set_hash: "a".repeat(64),
  });
  const applications = job.segments.find((segment) => segment.source_text === "APPLICATIONS")!;
  assert.equal(applications.edited_translation, "DOKUMENTNA UPORABA");
  assert.equal(applications.glossary_entry_id, "document-applications");
  assert.equal(applications.status, "translated");
  assert.equal(applications.needs_review, true);
  assert.equal(job.glossary_set_hash, "a".repeat(64));
  assert.equal(job.glossary_usage.find((usage) => usage.glossary_id === "document-applications")?.outcome, "applied");
  assert.equal(job.glossary_usage.find((usage) => usage.glossary_id === "persistent-applications")?.outcome, "warned");
  assert.equal(job.glossary_usage.find((usage) => usage.glossary_id === "persistent-applications")?.conflict, true);
});

test("only approved persistent entries affect a newly prepared job", () => {
  const statuses = ["proposed", "rejected", "archived"] as const;
  for (const status of statuses) {
    const glossary: GlossaryEntry[] = [
      ...createDefaultGlossary(),
      { id: `inactive-${status}`, source_term: "APPLICATIONS", target_term: "NE SME VELJATI", case_sensitive: true, mode: "exact_segment", enabled: true, origin: "user", scope: "persistent_user", status, revision: 1 },
    ];
    const job = createTranslationJob(fixture(), [acceptedOcr()], { source_language: "en", target_language: "sl", glossary });
    const applications = job.segments.find((segment) => segment.source_text === "APPLICATIONS")!;
    assert.equal(applications.edited_translation, "PODROČJA UPORABE");
    assert.equal(applications.glossary_entry_id, "builtin-applications");
  }
});

test("preferred glossary is advisory while do-not-translate and locked-inline are explicit", async () => {
  const glossary: GlossaryEntry[] = [
    { id: "preferred", source_term: "Panel", target_term: "plošča", case_sensitive: true, mode: "preferred", enabled: true, origin: "user" },
    { id: "keep", source_term: "ACME", target_term: "", case_sensitive: true, mode: "do_not_translate", enabled: true, origin: "user" },
    { id: "locked", source_term: "Fast curing", target_term: "hitro utrjevanje", case_sensitive: true, mode: "locked_inline", enabled: true, origin: "user" },
  ];
  const advisory = protectForTranslation("Panel", glossary);
  assert.equal(advisory.protected_text, "Panel");
  assert.equal(advisory.tokens.length, 0);
  const protectedValue = protectForTranslation("ACME Fast curing", glossary);
  assert.doesNotMatch(protectedValue.protected_text, /ACME|Fast curing/);
  assert.equal(protectedValue.tokens.filter((token) => token.kind === "glossary").length, 2);
  assert.equal(restoreProtectedTokens(protectedValue.protected_text, protectedValue.tokens), "ACME hitro utrjevanje");

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
  assert.equal(migrated.schema_version, 6);
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
  assert.equal(migrated.schema_version, 6);
  assert.equal(migrated.segments[0].source_text, current.segments[0].source_text);
  assert.deepEqual(migrated.segments[0].chunks, []);
});

test("schema three job migrates without inventing identity and can be safely rebound", () => {
  const current = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const legacy = JSON.parse(exportTranslationJob(current)) as Record<string, unknown>;
  legacy.schema_version = 3;
  delete legacy.document_identity;
  legacy.segments = (legacy.segments as Array<Record<string, unknown>>).map((segment) => {
    const copy = { ...segment };
    for (const key of ["source_bbox", "source_page_type", "source_rotation", "source_style"]) delete copy[key];
    return copy;
  });
  const migrated = importTranslationJob(JSON.stringify(legacy));
  assert.equal(migrated.schema_version, 6);
  assert.equal(migrated.document_identity, null);
  assert.equal(migrated.segments[0].source_text, current.segments[0].source_text);
  const rebound = bindTranslationJobToAnalysis(migrated, current);
  assert.deepEqual(rebound.document_identity, current.document_identity);
  assert.deepEqual(rebound.segments[0].source_bbox, current.segments[0].source_bbox);
});

test("cancellation during chunk translation returns the unchanged job", async () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const controller = new AbortController();
  const provider: TranslationProvider = { id: "cancel-midway", async translateBatch(requests) { controller.abort(); return requests.map((request) => ({ segment_id: request.segment_id, translated_text: request.text, confidence: null, error: null })); } };
  assert.equal(await translateJobBatch(job, provider, controller.signal), job);
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
