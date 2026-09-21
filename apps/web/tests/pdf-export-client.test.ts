import assert from "node:assert/strict";

import { calculateDocumentCoverage, createCoverageRevisionKey } from "../lib/document-coverage";
import { downloadPdfExport, preparePdfExport, type PdfExportPlan } from "../lib/pdf-export-client";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfAnalysis, PdfKind } from "../lib/pdf-types";
import type { TranslationJob } from "../lib/translation-types";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

function job(): TranslationJob {
  return {
    schema_version: 8,
    source_filename: "source.pdf",
    source_language: "en",
    target_language: "sl",
    domain: "",
    created_at: "2026-09-11T00:00:00Z",
    document_identity: { sha256: "a".repeat(64), page_count: 1, document_model_version: 1, pages: [{ page_number: 1, width: 100, height: 100, rotation: 0 }] },
    glossary: [],
    glossary_snapshot: [],
    glossary_set_hash: null,
    glossary_usage: [],
    translation_memory_snapshot: [],
    translation_memory_set_hash: null,
    translation_memory_usage: [],
    segments: [],
    translation_profile: null,
    quality_checklist: [],
  };
}

test("prepares a local multipart PDF export plan", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    assert.match(String(input), /^http:\/\/(?:127\.0\.0\.1|localhost):\d+\/api\/pdf\/export-translated\/plan$/);
    assert.equal(init?.method, "POST");
    const form = init?.body as FormData;
    assert.equal(form.get("mode"), "diagnostic");
    assert.equal(JSON.parse(String(form.get("translation_job"))).schema_version, 8);
    return new Response(JSON.stringify({
      filename: "source-sl-preview.pdf",
      mode: "diagnostic",
      document_sha256: "a".repeat(64),
      summary: { total_segments: 0, reviewed: 0, eligible: 0, skipped: 0, overflow: 0, conflicts: 0 },
      table_summary: { detected_tables: 1, reviewed_cells: 2, eligible_cells: 1, exported_cells: 0, technical_cells: 1, overflow_cells: 0, ambiguous_cells: 0, image_only_cells: 0, conflicting_cells: 0, partial_tables: 1 },
      ocr_summary: { reviewed_regions: 1, eligible_regions: 0, exported_regions: 0, unsafe_regions: 1, stale_regions: 0 },
      replacements: [],
      tables: [{ table_id: "p1-t1", page_number: 1, bbox: { x0: 10, y0: 20, x1: 90, y1: 80 }, row_count: 1, column_count: 2, reliable: true, partial_export: true, warnings: [], cells: [] }],
      ocr_replacements: [{ segment_id: "ocr-1", skip_reason: "ocr_background_unsafe" }],
    }), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    const result = await preparePdfExport(new File(["%PDF-"], "source.pdf", { type: "application/pdf" }), job(), "diagnostic");
    assert.equal(result.filename, "source-sl-preview.pdf");
    assert.equal(result.table_summary.detected_tables, 1);
    assert.equal(result.tables[0].partial_export, true);
    assert.equal(result.ocr_summary.unsafe_regions, 1);
    assert.equal(result.ocr_replacements[0].skip_reason, "ocr_background_unsafe");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("downloads a new local PDF with the server filename", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(new Blob(["%PDF-output"], { type: "application/pdf" }), {
    status: 200,
    headers: { "content-disposition": "attachment; filename*=UTF-8''source-sl-preview.pdf" },
  })) as typeof fetch;
  try {
    const result = await downloadPdfExport(new File(["%PDF-"], "source.pdf", { type: "application/pdf" }), job(), "replacement_preview");
    assert.equal(result.filename, "source-sl-preview.pdf");
    assert.equal(await result.blob.text(), "%PDF-output");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("shows a structured Slovenian export error", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: { code: "pdf_hash_mismatch", message: "Prevajalsko opravilo pripada drugemu PDF-ju." } }), { status: 400 })) as typeof fetch;
  try {
    await assert.rejects(() => preparePdfExport(new File(["%PDF-"], "source.pdf", { type: "application/pdf" }), job(), "diagnostic"), /drugemu PDF/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("separates text acquisition translation review and PDF export", () => {
  const currentJob = job();
  currentJob.document_identity = { ...currentJob.document_identity!, page_count: 2, pages: [
    { page_number: 1, width: 100, height: 100, rotation: 0 },
    { page_number: 2, width: 100, height: 100, rotation: 0 },
  ] };
  currentJob.segments = [segment({ status: "reviewed", edited_translation: "Prevod" })];
  const coverage = calculateDocumentCoverage({
    analysis: analysis(["digital", "scanned"]),
    ocrItems: [ocrItem({ page_number: 2, status: "needs_review" })],
    job: currentJob,
    plan: plan([{ segment_id: "p1-r1-b1", page_number: 1, eligible: true, skip_reason: null }]),
    downloadCompleted: true,
  });
  assert.deepEqual(coverage.text, { acquired_pages: 1, total_pages: 2, missing_pages: [2] });
  assert.deepEqual(coverage.translation, { translated: 1, translatable: 1 });
  assert.deepEqual(coverage.review, { reviewed: 1, translated: 1 });
  assert.equal(coverage.pdf_export.exported, 1);
  assert.ok(coverage.states.includes("ocr_review_required"));
  assert.ok(!coverage.states.includes("known_text_export_complete"));
  assert.match(coverage.pages[1].warnings.join(" "), /besedilo ni bilo pridobljeno/i);
});

test("reports a fully scanned document without inventing a completion percentage", () => {
  const coverage = calculateDocumentCoverage({
    analysis: analysis(["scanned"]),
    ocrItems: [ocrItem({ status: "failed" })],
    job: job(),
    plan: null,
    downloadCompleted: false,
  });
  assert.deepEqual(coverage.states, ["text_missing", "ocr_review_required"]);
  assert.equal(coverage.text.acquired_pages, 0);
  assert.equal(coverage.unresolved_ocr_candidates, 1);
});

test("requires current server OCR provenance and reports expired revisions", () => {
  const currentJob = job();
  currentJob.segments = [segment({
    source_text_origin: "accepted_ocr",
    accepted_ocr_source: null,
    status: "translated",
    edited_translation: "Prevod",
  })];
  const coverage = calculateDocumentCoverage({
    analysis: analysis(["scanned"]),
    ocrItems: [ocrItem({ status: "accepted", server_revision_id: null })],
    job: currentJob,
    plan: null,
    downloadCompleted: false,
  });
  assert.equal(coverage.pages[0].accepted_ocr_segments, 0);
  assert.equal(coverage.pages[0].unresolved_ocr_candidates, 1);
  assert.ok(coverage.states.includes("ocr_review_required"));
});

test("claims complete export only when every known page and segment is covered", () => {
  const currentJob = job();
  currentJob.segments = [segment({ status: "reviewed", edited_translation: "Prevod" })];
  const coverage = calculateDocumentCoverage({
    analysis: analysis(["digital"]),
    ocrItems: [],
    job: currentJob,
    plan: plan([{ segment_id: "p1-r1-b1", page_number: 1, eligible: true, skip_reason: null }]),
    downloadCompleted: true,
  });
  assert.deepEqual(coverage.states, ["known_text_export_complete"]);
  assert.equal(coverage.text.acquired_pages, 1);
  assert.equal(coverage.pdf_export.exported, 1);
});

test("reports partial table and overflow export without hiding safe output", () => {
  const currentJob = job();
  currentJob.segments = [
    segment({ segment_id: "cell-1", block_type: "table_cell", status: "reviewed", edited_translation: "Lastnost" }),
    segment({ segment_id: "cell-2", block_type: "table_cell", status: "reviewed", edited_translation: "Predolg prevod" }),
  ];
  const coverage = calculateDocumentCoverage({
    analysis: analysis(["digital"]),
    ocrItems: [],
    job: currentJob,
    plan: plan([
      { segment_id: "cell-1", page_number: 1, eligible: true, skip_reason: null },
      { segment_id: "cell-2", page_number: 1, eligible: false, skip_reason: "cell_overflow" },
    ]),
    downloadCompleted: true,
  });
  assert.equal(coverage.pdf_export.exported, 1);
  assert.ok(coverage.states.includes("partially_exported"));
  assert.deepEqual(coverage.pages[0].skips, [{ reason: "cell_overflow", count: 1 }]);
});

test("reports security skips separately from successful safe replacements", () => {
  const currentJob = job();
  currentJob.segments = [
    segment({ segment_id: "safe", status: "reviewed", edited_translation: "Varen prevod" }),
    segment({ segment_id: "unsafe", status: "reviewed", edited_translation: "Nevaren prevod" }),
  ];
  const coverage = calculateDocumentCoverage({
    analysis: analysis(["digital"]), ocrItems: [], job: currentJob,
    plan: plan([
      { segment_id: "safe", page_number: 1, eligible: true, skip_reason: null },
      { segment_id: "unsafe", page_number: 1, eligible: false, skip_reason: "image_overlap" },
    ]),
    downloadCompleted: false,
  });
  assert.equal(coverage.pdf_export.ready, 1);
  assert.deepEqual(coverage.pages[0].skips, [{ reason: "image_overlap", count: 1 }]);
  assert.ok(!coverage.states.includes("partially_exported"));
  assert.match(coverage.warnings.join(" "), /načrt PDF izvoza je delen/i);
});

test("invalidates export completion when translation review OCR source or document changes", () => {
  const baseAnalysis = analysis(["digital"]);
  const baseJob = job();
  const baseOcr = [ocrItem({ status: "accepted", selected_text_source: "ocr", effective_text: "OCR", server_revision_id: "rev-1" })];
  const key = createCoverageRevisionKey({ analysis: baseAnalysis, ocrItems: baseOcr, job: baseJob, mode: "diagnostic" });

  const editedJob = { ...baseJob, segments: [segment({ edited_translation: "Spremenjen prevod", status: "translated" })] };
  assert.notEqual(createCoverageRevisionKey({ analysis: baseAnalysis, ocrItems: baseOcr, job: editedJob, mode: "diagnostic" }), key);

  const reviewedJob = { ...baseJob, segments: [segment({ edited_translation: "Prevod", status: "reviewed" })] };
  assert.notEqual(createCoverageRevisionKey({ analysis: baseAnalysis, ocrItems: baseOcr, job: reviewedJob, mode: "diagnostic" }), key);

  const changedOcr = [ocrItem({ status: "accepted", selected_text_source: "ocr", effective_text: "Nov OCR", server_revision_id: "rev-2" })];
  assert.notEqual(createCoverageRevisionKey({ analysis: baseAnalysis, ocrItems: changedOcr, job: baseJob, mode: "diagnostic" }), key);

  const changedDocument = { ...baseAnalysis, sha256: "b".repeat(64) };
  assert.notEqual(createCoverageRevisionKey({ analysis: changedDocument, ocrItems: baseOcr, job: baseJob, mode: "diagnostic" }), key);
  assert.notEqual(createCoverageRevisionKey({ analysis: baseAnalysis, ocrItems: baseOcr, job: baseJob, mode: "replacement_preview" }), key);
});

test("does not mark an empty document or an export plan as completed", () => {
  const emptyCoverage = calculateDocumentCoverage({
    analysis: analysis([]), ocrItems: [], job: job(), plan: null, downloadCompleted: false,
  });
  assert.ok(emptyCoverage.states.includes("text_missing"));
  assert.ok(!emptyCoverage.states.includes("known_text_export_complete"));

  const currentJob = job();
  currentJob.segments = [segment({ status: "reviewed", edited_translation: "Prevod" })];
  const plannedCoverage = calculateDocumentCoverage({
    analysis: analysis(["digital"]), ocrItems: [], job: currentJob,
    plan: plan([{ segment_id: "p1-r1-b1", page_number: 1, eligible: true, skip_reason: null }]),
    downloadCompleted: false,
  });
  assert.equal(plannedCoverage.pdf_export.ready, 1);
  assert.equal(plannedCoverage.pdf_export.exported, 0);
  assert.ok(!plannedCoverage.states.includes("known_text_export_complete"));
});

function analysis(kinds: PdfKind[]): PdfAnalysis {
  return {
    filename: "source.pdf", sha256: "a".repeat(64), page_count: kinds.length, document_model_version: 1,
    kind: kinds.length === 1 ? kinds[0] : "mixed", extracted_text_chars: 10,
    pages: kinds.map((kind, index) => ({ page_number: index + 1, kind, extracted_text_chars: kind === "digital" ? 10 : 0, dimensions: { width: 100, height: 100, unit: "pt" }, rotation: 0, text_blocks: [], image_count: kind === "digital" ? 0 : 1 })),
    document: { metadata: {}, pages: kinds.map((kind, index) => ({ page_number: index + 1, width: 100, height: 100, rotation: 0, page_type: kind, regions: [], images: kind === "digital" ? [] : [{ bbox: { x0: 0, y0: 0, x1: 100, y1: 100 }, width: 100, height: 100, xref: 1, needs_ocr_check: true, ocr_text: null, ocr_confidence: null }] })) },
  };
}

function segment(overrides: Partial<TranslationJob["segments"][number]> = {}): TranslationJob["segments"][number] {
  return {
    segment_id: "p1-r1-b1", page_number: 1, block_id: "p1-r1-b1", block_type: "paragraph",
    source_language: "en", target_language: "sl", source_text: "Source", source_text_origin: "native",
    source_bbox: { x0: 1, y0: 1, x1: 20, y1: 10 }, source_page_type: "digital", source_rotation: 0,
    source_style: null, accepted_ocr_source: null, translated_text: "", edited_translation: "", status: "pending",
    quality: null, error: null, skip_reason: null, protected_tokens: [], glossary_matches: [], translation_origin: null,
    glossary_entry_id: null, model_id: null, provider_id: null, placeholder_attempts: [], needs_review: false,
    warnings: [], chunks: [], forced_chunking: false, created_at: "2026-09-11T00:00:00Z", ...overrides,
  };
}

function ocrItem(overrides: Partial<OcrReviewItem> = {}): OcrReviewItem {
  return {
    id: "ocr-1", page_number: 1, reason: "scanned_page", pdf_bbox: { x0: 0, y0: 0, x1: 100, y1: 100 },
    pixel_bbox: null, native_text: "", ocr_text: null, confidence: null, status: "pending", selected_text_source: "native",
    effective_text: "", error: null, source: "page", warnings: [], attempts: [], crop_image_url: null, crop_debug: null,
    server_revision_id: null, ...overrides,
  };
}

function plan(items: Array<{ segment_id: string; page_number: number; eligible: boolean; skip_reason: string | null }>): PdfExportPlan {
  return {
    filename: "source-sl.pdf", mode: "replacement_preview", document_sha256: "a".repeat(64),
    summary: { total_segments: items.length, reviewed: items.length, eligible: items.filter((item) => item.eligible).length, skipped: items.filter((item) => !item.eligible).length, overflow: 0, conflicts: 0, exact: 0, expanded: 0, scaled: 0, background_patches: 0, image_overlap_skipped: 0 },
    replacements: items.map((item) => ({ ...item, source_bbox: null, original_bbox: null, fitted_bbox: null, translated_text: "Prevod", reviewed: true, fit_status: item.eligible ? "fits" : "overflow", warnings: [], requested_font_size: 10, fitted_font_size: item.eligible ? 10 : null, scale: item.eligible ? 1 : null, original_line_count: 1, fitted_line_count: item.eligible ? 1 : null, font_family: "FiraGO", font_weight: "regular", font_style: "normal", color: "#000000", fit_strategy: item.eligible ? "exact" : "overflow", typography_warnings: [], image_overlap_kind: "none", overlapping_image_xrefs: [], overlap_ratio: 0, background_patch_used: false, background_color: null, patch_rects: [], overlap_confidence: 1, background_metrics: null, image_overlap_warnings: [] })),
    table_summary: { detected_tables: 0, reviewed_cells: 0, eligible_cells: 0, exported_cells: 0, technical_cells: 0, overflow_cells: 0, ambiguous_cells: 0, image_only_cells: 0, conflicting_cells: 0, partial_tables: 0 }, tables: [],
    ocr_summary: { reviewed_regions: 0, eligible_regions: 0, exported_regions: 0, unsafe_regions: 0, stale_regions: 0 }, ocr_replacements: [],
  };
}


void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
