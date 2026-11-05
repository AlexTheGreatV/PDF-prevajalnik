import assert from "node:assert/strict";
import { createPdfExportSettings, restorePdfExportSettings } from "../lib/pdf-export-settings";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { nativeSourceReviewWarning, sourceAwareQcLabel } from "../lib/ocr-review";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfAnalysis, StructuredBlock, StructuredRegion } from "../lib/pdf-types";
import { createDefaultGlossary } from "../lib/default-glossary";
import { findGlossaryMatches } from "../lib/glossary";
import { proposeSiMeasurements, SI_RULES_VERSION } from "../lib/si-measurements";
import { protectTechnicalTokens, restoreTechnicalTokens } from "../lib/technical-tokens";
import { TECHNICAL_RANGE_RULES_VERSION } from "../lib/technical-ranges";
import {
  bindTranslationJobToAnalysis,
  recoverTranslationJobForAnalysis,
  restoreVerifiedOcrItems,
  hasUnresolvedOcrSources,
  createTranslationJob,
  refreshTranslationMemoryContext,
  previewTranslationMemoryRefresh,
  applyTranslationMemoryRefresh,
  previewMeasurementNormalization,
  applyMeasurementNormalization,
  createTranslationChunks,
  editTranslation,
  exportTranslationJob,
  getSkipReason,
  importTranslationJob,
  invalidateStaleOcrApprovals,
  mergeCurrentTranslationResult,
  protectForTranslation,
  resetTranslation,
  restoreProtectedTokens,
  retryFailedTranslation,
  reviewTranslation,
  splitOversizedChunk,
  translateJobBatch,
  updateTranslationJobSegment,
  validateTranslationTechnicalIntegrity,
  validateTranslationJobCompatibility,
} from "../lib/translation-job";
import type { GlossaryEntry, TranslationProvider, TranslationProviderResult, TranslationRequest } from "../lib/translation-types";
import { updateTerminologySources } from "../lib/translation-profile-evaluation";
import { refreshTranslationProfileMetadata } from "../lib/translation-profile-evaluation";
import type { TranslationMemoryEntry } from "../lib/translation-memory";

const tests: Promise<void>[] = [];
test("a durable verified alias imports over identical accepted OCR without transferring unverified reviews", () => {
  const analysis = fixture();
  const bbox = analysis.document!.pages[0].regions.flatMap(region => region.blocks).find(block => block.reading_order === 1)!.bbox;
  const old = { ...acceptedOcr(), pdf_bbox: bbox, server_revision_id: "a".repeat(32) };
  const current = { ...old, server_revision_id: "b".repeat(32) };
  let imported = createTranslationJob(analysis, [old], { source_language: "en", target_language: "sl" });
  const id = imported.segments.find(s => s.source_text_origin === "accepted_ocr")!.segment_id;
  // Synthetic review setup, not a real user's approval.
  imported = updateTranslationJobSegment(imported, id, s => reviewTranslation(editTranslation(s, "UPORABA")));
  const expected = createTranslationJob(analysis, [current], { source_language: "en", target_language: "sl" });
  const items = restoreVerifiedOcrItems(imported, expected, [current], new Set([old.server_revision_id]));
  const restored = recoverTranslationJobForAnalysis(imported, createTranslationJob(analysis, items,
    { source_language: "en", target_language: "sl" }), items);
  assert.deepEqual(restored.segments.find(s => s.segment_id === id)!.review_state,
    imported.segments.find(s => s.segment_id === id)!.review_state);
  assert.equal(restored.segments.find(s => s.segment_id === id)!.status, "reviewed");
  const unverifiedItems = restoreVerifiedOcrItems(imported, expected, [current], new Set());
  const unresolved = recoverTranslationJobForAnalysis(imported, expected, unverifiedItems).segments.find(s => s.segment_id === id)!;
  assert.equal(unresolved.review_state, null); assert.equal(unresolved.needs_review, true);
  assert.equal(unresolved.accepted_ocr_source?.revision_id, null);
  const rejectedSame = restoreVerifiedOcrItems(imported, imported, [old], new Set());
  assert.equal(rejectedSame[0].status, "pending");
  assert.equal(rejectedSame[0].ocr_text, old.ocr_text);
  const rejectedJob = recoverTranslationJobForAnalysis(imported, createTranslationJob(analysis, rejectedSame,
    { source_language: "en", target_language: "sl" }), rejectedSame);
  assert.equal(rejectedJob.segments.find(s => s.segment_id === id)!.review_state, null);
  const changed = { ...current, ocr_text: "DIFFERENT", effective_text: "DIFFERENT" };
  assert.throws(() => recoverTranslationJobForAnalysis(imported, createTranslationJob(analysis, [changed],
    { source_language: "en", target_language: "sl" }), [changed]), /nezdružljive/);
});
test("SI suggestions are explicit, traceable and invalidate only edited confirmations", () => {
  let job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const id = job.segments.find(s => s.source_text === "Fast curing")!.segment_id;
  job = updateTranslationJobSegment(job, id, s => reviewTranslation(editTranslation({ ...s,
    source_text: "Pressure 2.50 MPa at 45°." }, "Tlak 2.50MPa pri 45 °.")));
  const original = JSON.stringify(job);
  const preview = previewMeasurementNormalization(job);
  assert.equal(JSON.stringify(job), original);
  assert.equal(preview.changes.length, 1);
  assert.equal(preview.changes[0].after, "Tlak 2,50\u00a0MPa pri 45°." );
  assert.equal(preview.changes[0].reviewed, true);
  const applied = applyMeasurementNormalization(job, preview);
  const changed = applied.segments.find(s => s.segment_id === id)!;
  assert.equal(changed.source_text, "Pressure 2.50 MPa at 45°.");
  assert.equal(changed.status, "translated"); assert.equal(changed.needs_review, true);
  assert.equal(changed.review_state, null); assert.equal(changed.current_qc?.status, "passed");
  assert.ok(changed.translation_rule_adjustments?.some(row => row.source_text === "2.50MPa" && row.replacement_text === "2,50\u00a0MPa"));
  assert.deepEqual(previewMeasurementNormalization(applied).changes, []);
  assert.throws(() => applyMeasurementNormalization(applied, preview), /zastarel/);
  assert.deepEqual(importTranslationJob(exportTranslationJob(applied)).segments, applied.segments);
});
test("SI proposals leave ambiguous decimals, codes, URLs and source-unit repairs for review", async () => {
  const { proposeSiMeasurements } = await import("../lib/si-measurements");
  const source = "1,000 g; 1.59 gr/ml; 2 Mpa; code 2.5mm; MS65; ISO 1.5; https://example.test/1.59g";
  const result = proposeSiMeasurements(source);
  assert.deepEqual(result.changes, []);
  assert.ok(result.warnings.some(w => w.startsWith("ambiguous_measurement_number:")));
  assert.ok(result.warnings.some(w => w.includes("gr/ml")));
  assert.ok(result.warnings.some(w => w.includes("Mpa")));
});
test("one reviewed quantity spelling is scoped and never approves new digits or imported claims", () => {
  let job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const id = job.segments.find(s => s.source_text === "Fast curing")!.segment_id;
  job = updateTranslationJobSegment(job, id, s => editTranslation({ ...s, source_text: "1.59 gr/ml" }, "1,59 gr/ml"));
  // Synthetic server-verified record; not a review of any real document.
  const record = { version: 1 as const, profile: "unit-test", document_sha256: job.document_identity!.sha256,
    segment_id: id, page_number: job.segments.find(s => s.segment_id === id)!.page_number, source_bbox: job.segments.find(s => s.segment_id === id)!.source_bbox!, source_kind: "native", source_text: "1.59 gr/ml", target_unit: "g/ml", approval_note: "Synthetic source spelling review",
    recorded_at: "2026-11-03T00:00:00+00:00", evidence_sha256: "a".repeat(64) };
  job = { ...job, reviewed_unit_correction: record };
  const preview = previewMeasurementNormalization(job);
  assert.equal(preview.changes.length, 1);
  assert.equal(preview.changes[0].after, "1,59\u00a0g/ml");
  const applied = applyMeasurementNormalization(job, preview);
  const segment = applied.segments.find(s => s.segment_id === id)!;
  assert.equal(segment.status, "translated"); assert.equal(segment.review_state, null);
  assert.equal(segment.current_qc?.status, "warnings");
  assert.equal(segment.current_qc?.findings.some(f => f.severity === "blocking"), false);
  assert.equal(segment.translation_rule_adjustments?.at(-1)?.evidence_sha256, record.evidence_sha256);
  const context = { document_sha256: record.document_sha256, segment_id: id, page_number: record.page_number, source_bbox: record.source_bbox, source_kind: record.source_kind, record };
  assert.equal(validateTranslationTechnicalIntegrity("1.59 gr/ml", "1,59 g/ml", context).valid, true);
  for (const text of ["1,60 g/ml", "1,590 g/ml", "-1,59 g/ml", "1,59 kg/ml", "1,59 g/L", "1,59 g/ml and 20 L"]) {
    assert.equal(validateTranslationTechnicalIntegrity("1.59 gr/ml", text, context).valid, false, text);
  }
  assert.equal(validateTranslationTechnicalIntegrity("1.59 gr/ml", "1,59 g/ml", { ...context, segment_id: "other" }).valid, false);
  assert.equal(validateTranslationTechnicalIntegrity("1.59 gr/ml", "1,59 g/ml", { ...context, document_sha256: "b".repeat(64) }).valid, false);
  const imported = importTranslationJob(exportTranslationJob(applied));
  assert.equal(imported.reviewed_unit_correction, undefined);
  assert.equal(imported.segments.find(s => s.segment_id === id)!.current_qc?.status, "blocked");
  assert.equal(refreshTranslationProfileMetadata({ ...imported, reviewed_unit_correction: record }).segments.find(s => s.segment_id === id)!.current_qc?.findings.some(f => f.severity === "blocking"), false);
});
test("TM preview is non-mutating and applying it preserves JSON review, OCR and export settings", () => {
  const analysis = fixture();
  const table = analysis.document!.pages[0].regions[1].blocks.find(block => block.kind === "table")!;
  table.table_provenance = { table_id: "refresh-test-table", bbox: table.bbox, row_count: 2, column_count: 2,
    geometry_evidence: "vector_grid", cells: [], warnings: [] };
  let job = createTranslationJob(analysis, [acceptedOcr()], { source_language: "auto", target_language: "sl" });
  job = { ...job, export_settings: createPdfExportSettings(analysis, "document-role-typography-pilot-v1",
    { page_number: 1, table_id: "refresh-test-table" }) };
  const reviewedId = job.segments.find(s => s.source_text === "Package")!.segment_id;
  job = updateTranslationJobSegment(job, reviewedId, s => reviewTranslation(editTranslation(s, "Ročni paket")));
  const options = { source_language: "en", domain: "technical_datasheets",
    entries: [tmEntry("Fast curing", "Hitro utrjevanje")], active_set_hash: "preview-test" };
  const original = JSON.stringify(job);
  const preview = previewTranslationMemoryRefresh(job, options);
  assert.equal(JSON.stringify(job), original);
  assert.equal(preview.changes.length, 1);
  assert.equal(preview.changes[0].source, "Fast curing");
  assert.equal(preview.changes[0].after, "Hitro utrjevanje");
  const refreshed = applyTranslationMemoryRefresh(job, options, preview).job;
  const restored = importTranslationJob(exportTranslationJob(refreshed));
  assert.deepEqual(restored.segments, refreshed.segments);
  assert.deepEqual(restored.export_settings, job.export_settings);
  assert.deepEqual(restored.segments.map(s => s.accepted_ocr_source), job.segments.map(s => s.accepted_ocr_source));
  assert.deepEqual(restored.segments.find(s => s.segment_id === reviewedId)!.review_state,
    job.segments.find(s => s.segment_id === reviewedId)!.review_state);
  const repeat = previewTranslationMemoryRefresh(restored, options);
  assert.deepEqual(repeat.changes, []);
  assert.deepEqual(applyTranslationMemoryRefresh(restored, options, repeat).job, refreshed);
});
test("late TM preview cannot overwrite changed work, domain, TM entries or active set", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "auto", target_language: "sl" });
  const options = { source_language: "en", domain: "technical_datasheets",
    entries: [tmEntry("Fast curing", "Hitro utrjevanje")], active_set_hash: "first" };
  const preview = previewTranslationMemoryRefresh(job, options);
  const changed = updateTranslationJobSegment(job, job.segments[0].segment_id, s => editTranslation(s, "Novo ročno besedilo"));
  assert.throws(() => applyTranslationMemoryRefresh(changed, options, preview), /zastarel/);
  for (const replacement of [{ ...options, domain: "other" }, { ...options, source_language: "de" },
    { ...options, active_set_hash: "second" }, { ...options, entries: [tmEntry("Fast curing", "Drug predlog")] }]) {
    assert.throws(() => applyTranslationMemoryRefresh(job, replacement, preview), /zastarel/);
  }
  assert.equal(changed.segments[0].edited_translation, "Novo ročno besedilo");
});
test("explicit TM context refresh preserves reviewed and manual work and source identity", () => {
  let job = createTranslationJob(fixture(), [], { source_language: "auto", target_language: "sl" });
  const provider = job.segments.find(s => s.source_text === "Fast curing")!;
  const reviewed = job.segments.find(s => s.source_text === "qPPLICATIONS")!;
  const manual = job.segments.find(s => s.source_text === "Package")!;
  job = updateTranslationJobSegment(job, provider.segment_id, s => ({ ...s, status: "translated",
    translated_text: "Star predlog", edited_translation: "Star predlog", translation_origin: "provider",
    placeholder_attempts: [{ strategy: "primary_tk", provider_text: "Star predlog", valid: true, error: null }] }));
  // Explicit synthetic fixture review, not a real document approval.
  job = updateTranslationJobSegment(job, reviewed.segment_id, s => reviewTranslation(editTranslation(s, "Pregledani naslov")));
  job = updateTranslationJobSegment(job, manual.segment_id, s => editTranslation(s, "Ročno urejeni paket"));
  const before = JSON.stringify(job);
  const entries = [tmEntry("Fast curing", "Hitro utrjevanje", { domain: "technical_datasheets" }),
    tmEntry("qPPLICATIONS", "Drugi naslov", { id: "reviewed-match", domain: "technical_datasheets" }),
    tmEntry("Package", "Drug paket", { id: "manual-match", domain: "technical_datasheets" })];
  const result = refreshTranslationMemoryContext(job, { source_language: "en", domain: "technical_datasheets", entries, active_set_hash: "new" });
  assert.equal(JSON.stringify(job), before);
  assert.equal(result.job.created_at, job.created_at);
  assert.deepEqual(result.job.document_identity, job.document_identity);
  assert.deepEqual(result.changed_segment_ids, [provider.segment_id]);
  const updated = result.job.segments.find(s => s.segment_id === provider.segment_id)!;
  assert.equal(updated.edited_translation, "Hitro utrjevanje");
  assert.equal(updated.translation_origin, "translation_memory");
  assert.equal(updated.needs_review, true);
  assert.equal(updated.review_state, null);
  assert.equal(updated.current_qc?.status, "passed");
  assert.deepEqual(updated.placeholder_attempts, job.segments.find(s => s.segment_id === provider.segment_id)!.placeholder_attempts);
  const afterReviewed = result.job.segments.find(s => s.segment_id === reviewed.segment_id)!;
  assert.equal(afterReviewed.edited_translation, "Pregledani naslov");
  assert.equal(afterReviewed.status, "reviewed");
  assert.deepEqual(afterReviewed.review_state, job.segments.find(s => s.segment_id === reviewed.segment_id)!.review_state);
  assert.equal(result.job.segments.find(s => s.segment_id === manual.segment_id)!.edited_translation, "Ročno urejeni paket");
  for (const [index, segment] of job.segments.entries()) {
    const after = result.job.segments[index];
    assert.equal(after.segment_id, segment.segment_id);
    assert.equal(after.source_text, segment.source_text);
    assert.deepEqual(after.source_bbox, segment.source_bbox);
    assert.deepEqual(after.accepted_ocr_source, segment.accepted_ocr_source);
  }
  assert.equal(importTranslationJob(exportTranslationJob(result.job)).domain, "technical_datasheets");
  assert.equal(editTranslation(afterReviewed, "Spremenjeni naslov").review_state, null);
  assert.equal(editTranslation(afterReviewed, "Spremenjeni naslov").status, "translated");
});
test("TM context refresh leaves missing, conflicting, unapproved and unsafe matches untouched", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "auto", target_language: "sl" });
  const source = job.segments.find(s => s.source_text === "Fast curing")!;
  for (const entries of [[], [tmEntry(source.source_text, "Predlog", { status: "proposed" })],
    [tmEntry(source.source_text, "Prvi"), tmEntry(source.source_text, "Drugi", { id: "conflict" })]]) {
    const result = refreshTranslationMemoryContext(job, { source_language: "en", domain: "technical_datasheets", entries, active_set_hash: "test" });
    assert.deepEqual(result.changed_segment_ids, []);
    assert.equal(result.job.segments.find(s => s.segment_id === source.segment_id)!.edited_translation, source.edited_translation);
  }
  const technical = job.segments.find(s => s.source_text.includes("CO₂"))!;
  const result = refreshTranslationMemoryContext(job, { source_language: "en", domain: "technical_datasheets",
    entries: [tmEntry(technical.source_text, "Manjkajoče zaščitene vrednosti")], active_set_hash: "unsafe" });
  assert.deepEqual(result.changed_segment_ids, []);
  assert.equal(result.job.segments.find(s => s.segment_id === technical.segment_id)!.edited_translation, technical.edited_translation);
});
test("context-only changes never claim unmatched provider translations were refreshed", () => {
  let job = createTranslationJob(fixture(), [], { source_language: "auto", target_language: "sl" });
  const source = job.segments.find(s => s.source_text === "Fast curing")!;
  job = updateTranslationJobSegment(job, source.segment_id, s => ({ ...s, status: "translated",
    translated_text: "Isti ponudnikov predlog", edited_translation: "Isti ponudnikov predlog", translation_origin: "provider" }));
  const result = refreshTranslationMemoryContext(job, { source_language: "en", domain: "technical_datasheets", entries: [], active_set_hash: "empty" });
  assert.deepEqual(result.changed_segment_ids, []);
  const after = result.job.segments.find(s => s.segment_id === source.segment_id)!;
  assert.equal(after.edited_translation, source.edited_translation || "Isti ponudnikov predlog");
  assert.equal(after.translation_origin, "provider");
  assert.equal(result.job.source_language, "en");
  assert.equal(result.job.domain, "technical_datasheets");
});
test("TM refresh respects explicit glossary translations and unlabelled manual edits", () => {
  let job = createTranslationJob(fixture(), [], { source_language: "auto", target_language: "sl" });
  const source = job.segments.find(s => s.source_text === "Fast curing")!;
  const entries = [tmEntry(source.source_text, "TM predlog")];
  for (const segment of [{ ...source, translation_origin: "glossary" as const, translated_text: "Glosar", edited_translation: "Glosar" },
    { ...source, translation_origin: "provider" as const, translated_text: "Model", edited_translation: "Ročna sprememba" }]) {
    job = { ...job, segments: job.segments.map(s => s.segment_id === source.segment_id ? segment : s) };
    const result = refreshTranslationMemoryContext(job, { source_language: "en", domain: "technical_datasheets", entries, active_set_hash: "current" });
    assert.equal(result.job.segments.find(s => s.segment_id === source.segment_id)!.edited_translation, segment.edited_translation);
    assert.deepEqual(result.changed_segment_ids, []);
  }
});
test("repeated explicit TM refresh is deterministic and never creates a review", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "auto", target_language: "sl" });
  const options = { source_language: "en", domain: "technical_datasheets", entries: [tmEntry("Fast curing", "Hitro utrjevanje")], active_set_hash: "same" };
  const first = refreshTranslationMemoryContext(job, options);
  const second = refreshTranslationMemoryContext(first.job, options);
  assert.deepEqual(second.changed_segment_ids, []);
  assert.deepEqual(second.job, first.job);
  assert.equal(second.job.segments.some(s => s.status === "reviewed"), false);
});
test("context refresh changes export revision without changing confirmed content revision", async () => {
  const { createCoverageRevisionKey } = await import("../lib/document-coverage");
  const analysis = fixture();
  let job = createTranslationJob(analysis, [], { source_language: "auto", target_language: "sl" });
  const source = job.segments.find(s => s.source_text === "Fast curing")!;
  job = updateTranslationJobSegment(job, source.segment_id, s => reviewTranslation(editTranslation(s, "Potrjen prevod")));
  const after = refreshTranslationMemoryContext(job, { source_language: "en", domain: "technical_datasheets", entries: [], active_set_hash: "next" }).job;
  assert.notEqual(createCoverageRevisionKey({ analysis, ocrItems: [], job, mode: "replacement_preview" }),
    createCoverageRevisionKey({ analysis, ocrItems: [], job: after, mode: "replacement_preview" }));
  assert.deepEqual(after.segments.find(s => s.segment_id === source.segment_id)!.review_state,
    job.segments.find(s => s.segment_id === source.segment_id)!.review_state);
});
test("JSON preserves explicit typography and table choice without persisting an authorization token or changing reviews", () => {
  const analysis = fixture();
  const table = analysis.document!.pages[0].regions[1].blocks.find(block => block.kind === "table")!;
  table.table_provenance = {table_id: "synthetic-table", bbox: table.bbox, row_count: 2, column_count: 2,
    geometry_evidence: "vector_grid", cells: [], warnings: []};
  let job = createTranslationJob(analysis, [], {source_language: "en", target_language: "sl"});
  // Synthetic fixture approval only; never an approval of a real user's text.
  const reviewedId = job.segments.find(segment => segment.source_text === "Fast curing")!.segment_id;
  job = updateTranslationJobSegment(job, reviewedId, segment => reviewTranslation(editTranslation(segment, "Hitro utrjevanje")));
  const before = JSON.stringify(job.segments);
  const settings = createPdfExportSettings(analysis, "document-role-typography-pilot-v1", {page_number: 1, table_id: "synthetic-table"});
  const restored = importTranslationJob(exportTranslationJob({...job, export_settings: settings}));
  assert.deepEqual(restored.export_settings, settings);
  assert.equal(JSON.stringify(restored.segments), before);
  assert.equal(restored.segments.find(segment => segment.segment_id === reviewedId)!.status, "reviewed");
  assert.equal(restorePdfExportSettings(analysis, restored.export_settings).selection?.table_id, "synthetic-table");
  assert.equal(restorePdfExportSettings(analysis, restored.export_settings).error, null);
  assert.equal(JSON.stringify(settings).includes('"token"'), false);
  assert.equal(restorePdfExportSettings(analysis, undefined).selection, null);
});
test("restored reconstruction fails closed for another PDF, model, page, table or geometry while preserving translations", () => {
  const analysis = fixture();
  const table = analysis.document!.pages[0].regions[1].blocks.find(block => block.kind === "table")!;
  table.table_provenance = {table_id: "synthetic-table", bbox: table.bbox, row_count: 2, column_count: 2,
    geometry_evidence: "vector_grid", cells: [], warnings: []};
  const settings = createPdfExportSettings(analysis, "original", {page_number: 1, table_id: "synthetic-table"});
  for (const changed of [{...analysis, sha256: "different"}, {...analysis, document_model_version: 999}]) {
    assert.ok(restorePdfExportSettings(changed, settings).error);
    assert.equal(restorePdfExportSettings(changed, settings).selection, null);
  }
  for (const selection of [{...settings.reconstruction!, page_number: 2}, {...settings.reconstruction!, table_id: "other"},
    {...settings.reconstruction!, geometry_binding: "forged"}]) {
    assert.ok(restorePdfExportSettings(analysis, {...settings, reconstruction: selection}).error);
  }
  const changed = structuredClone(analysis);
  changed.document!.pages[0].regions[1].blocks.find(block => block.kind === "table")!.bbox.x1 += 1;
  assert.ok(restorePdfExportSettings(changed, settings).error);
  assert.ok(restorePdfExportSettings(analysis, {...settings, version: 99}).error);
});
test("closed vector row units have stable separate sources without shifting neighboring IDs", () => {
  const analysis=fixture();
  analysis.document_model_version=4;
  const source=analysis.document!.pages[0].regions[1].blocks[0];
  source.original_text="Black\nchocolate brown\nWhite";
  source.normalized_text="Black chocolate brown White";
  source.independent_units=["Black","chocolate brown","White"].map((text,index)=>({
    unit_id:`line-${index}`,text,bbox:{x0:2,y0:20+index*20,x1:70,y1:30+index*20},
    baseline_y:28+index*20,row_bbox:{x0:0,y0:15+index*20,x1:100,y1:35+index*20},
    source_span_ids:[`span-${index}`],evidence:"closed_vector_row" as const,
  }));
  const options={source_language:"en",target_language:"sl"};
  const job=createTranslationJob(analysis,[],options);
  const children=job.segments.filter(s=>s.block_id==="p1-r2-b2");
  assert.deepEqual(children.map(s=>s.source_text),["Black","chocolate brown","White"]);
  assert.deepEqual(children.map(s=>s.segment_id),["p1-r2-b2-line-0","p1-r2-b2-line-1","p1-r2-b2-line-2"]);
  assert.ok(children.every(s=>s.status!=="reviewed" && !s.review_state));
  assert.equal(job.segments.find(s=>s.source_text==="Fast curing")!.segment_id,"p1-r2-b4");
  assert.equal(source.original_text,"Black\nchocolate brown\nWhite");
  assert.equal(exportTranslationJob(importTranslationJob(exportTranslationJob(job))),exportTranslationJob(job));
  const legacy=structuredClone(job);
  legacy.document_identity!.document_model_version=3;
  const before=JSON.stringify(legacy);
  assert.doesNotThrow(()=>importTranslationJob(exportTranslationJob(legacy)));
  assert.throws(()=>validateTranslationJobCompatibility(legacy,job),/PDF ponovno analizirajte/);
  assert.equal(JSON.stringify(legacy),before);
});
test("frontend QC matches shared server protected-data contract cases", () => {
  const cases: Array<{id:string;source:string;target:string;valid:boolean}> = JSON.parse(readFileSync(resolve(process.cwd(), "../api/tests/fixtures/protected-data-cases.json"), "utf8"));
  for (const row of cases) assert.equal(validateTranslationTechnicalIntegrity(row.source,row.target).valid,row.valid,row.id);
});
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

function tmEntry(source_text: string, target_text: string, changes: Partial<TranslationMemoryEntry> = {}): TranslationMemoryEntry {
  return {
    id: "tm-1", source_text, target_text, normalized_source: source_text.normalize("NFC").trim(),
    normalized_source_sha256: "b".repeat(64), source_language: "en", target_language: "sl",
    domain: "", status: "approved", origin: "manual_review", source_document_sha256: null,
    source_segment_id: null, source_kind: "native", note: "", revision: 2,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    approved_at: "2026-01-01T00:00:00Z", usage_count: 0, last_used_at: null, ...changes,
  };
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

test("schema ten JSON round trip preserves schema nine table cell provenance", () => {
  const analysis = fixture();
  analysis.document_model_version = 2;
  const table = analysis.document!.pages[0].regions[1].blocks.find((item) => item.kind === "table")!;
  table.table_provenance = {
    table_id: "p1-t1",
    bbox: table.bbox,
    row_count: 2,
    column_count: 2,
    geometry_evidence: "vector_grid",
    warnings: [],
    cells: [{
      cell_id: "p1-t1-r1-c1",
      row_index: 0,
      column_index: 0,
      row_span: 1,
      column_span: 1,
      cell_bbox: { x0: 0, y0: 40, x1: 50, y1: 50 },
      source_bbox: { x0: 2, y0: 42, x1: 30, y1: 48 },
      content_bbox: { x0: 0.5, y0: 40.5, x1: 49.5, y1: 49.5 },
      source_span_ids: ["p1-s1"],
      geometry_evidence: "vector_grid",
      proven_edges: ["top", "right", "bottom", "left"],
      ownership: "owned",
      block_reason: null,
      has_graphic_overlap: false,
      rotated_text: false,
      background_fill: "#ffffff",
    }],
  };

  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  const before = job.segments.find((segment) => segment.segment_id === "p1-r2-b5-c1-1")!.table_cell_provenance;
  const imported = importTranslationJob(exportTranslationJob(job));
  const after = imported.segments.find((segment) => segment.segment_id === "p1-r2-b5-c1-1")!.table_cell_provenance;

  assert.deepEqual(after, before);
});

test("keeps structural table geometry but omits cells without visible source text", () => {
  const analysis = fixture();
  const table = analysis.document!.pages[0].regions[1].blocks.find((item) => item.kind === "table")!;
  table.table_cells = [
    ["Visible", ""],
    [" \t\n", "\u00a0"],
    ["\u200b", "—"],
    ["%", "+"],
  ];
  const originalGrid = structuredClone(table.table_cells);

  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  const cells = job.segments.filter((segment) => segment.block_type === "table_cell");

  assert.deepEqual(cells.map((segment) => segment.source_text), ["Visible", "—", "%", "+"]);
  assert.deepEqual(cells.map((segment) => segment.segment_id), [
    "p1-r2-b5-c1-1", "p1-r2-b5-c3-2", "p1-r2-b5-c4-1", "p1-r2-b5-c4-2",
  ]);
  assert.deepEqual(table.table_cells, originalGrid);
});

test("accepted OCR turns an explicitly linked empty table cell into a translation unit", () => {
  const analysis = fixture();
  const table = analysis.document!.pages[0].regions[1].blocks.find((item) => item.kind === "table")!;
  table.table_cells = [["Label", ""]];
  const item: OcrReviewItem = {
    ...acceptedOcr(),
    id: "table-cell-ocr",
    linked_block_reading_order: table.reading_order,
    linked_table_cell: { row_index: 0, column_index: 1 },
    native_text: "",
    ocr_text: "Recovered value",
    effective_text: "Recovered value",
    pdf_bbox: { x0: 50, y0: 40, x1: 95, y1: 50 },
  };

  const segment = createTranslationJob(analysis, [item], { source_language: "en", target_language: "sl" })
    .segments.find((candidate) => candidate.segment_id === "p1-r2-b5-c1-2");

  assert.equal(segment?.source_text, "Recovered value");
  assert.equal(segment?.source_text_origin, "accepted_ocr");
  assert.deepEqual(segment?.source_bbox, item.pdf_bbox);
  assert.equal(segment?.accepted_ocr_source?.review_id, item.id);
  assert.equal(table.table_cells[0][1], "");
});

test("imports legacy jobs with empty table cells without exposing them as translation units", () => {
  const expected = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const legacy = JSON.parse(exportTranslationJob(expected));
  const template = expected.segments.find((segment) => segment.block_type === "table_cell")!;
  legacy.segments.push({
    ...template,
    segment_id: "p1-r2-b5-c9-9",
    source_text: "\u200b",
    translated_text: "",
    edited_translation: "",
    status: "skipped",
    skip_reason: "Prazno besedilo.",
    protected_tokens: [],
    glossary_matches: [],
    chunks: [],
  });

  const imported = importTranslationJob(JSON.stringify(legacy));
  assert.equal(imported.segments.some((segment) => segment.segment_id === "p1-r2-b5-c9-9"), false);
  assert.doesNotThrow(() => validateTranslationJobCompatibility(imported, expected));
  assert.equal(JSON.parse(exportTranslationJob(imported)).segments.length, expected.segments.length);
});

test("never sends a legacy empty table cell to glossary memory or provider processing", async () => {
  const base = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const template = base.segments.find((segment) => segment.block_type === "table_cell")!;
  const provider = new DeterministicProvider();
  const translated = await translateJobBatch({
    ...base,
    segments: [{
      ...template,
      segment_id: "legacy-empty-cell",
      source_text: "\u00a0\u200b",
      status: "pending",
      skip_reason: null,
      glossary_matches: [],
      protected_tokens: [],
    }],
  }, provider);

  assert.equal(provider.calls, 0);
  assert.deepEqual(translated.segments, []);
  assert.deepEqual(translated.glossary_usage, []);
  assert.deepEqual(translated.translation_memory_usage, []);
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

test("uses normalized native text for translation without changing the document original", () => {
  const analysis = fixture();
  const sourceBlock = analysis.document!.pages[0].regions[1].blocks[0];
  sourceBlock.original_text = "moisture-\ncuring polymer";
  sourceBlock.normalized_text = "moisture-curing polymer";
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  assert.equal(job.segments.find((segment) => segment.block_id === "p1-r2-b2")?.source_text, "moisture-curing polymer");
  assert.equal(sourceBlock.original_text, "moisture-\ncuring polymer");
});

test("uses an approved exact translation memory snapshot without auto review", () => {
  const source = "Fast curing";
  const entry = tmEntry(source, "Hitro strjevanje");
  const job = createTranslationJob(fixture(), [], {
    source_language: "en", target_language: "sl", translation_memory: [entry], translation_memory_set_hash: "tm-hash",
  });
  const segment = job.segments.find((item) => item.source_text === source)!;
  assert.equal(segment.edited_translation, "Hitro strjevanje");
  assert.equal(segment.translation_origin, "translation_memory");
  assert.equal(segment.translation_memory_entry_id, entry.id);
  assert.equal(segment.translation_memory_revision, 2);
  assert.equal(segment.status, "translated");
  assert.equal(segment.needs_review, true);
  assert.notEqual(segment.status, "reviewed");
  assert.equal(job.translation_memory_usage[0].entry_id, entry.id);
});

test("document exact glossary has priority over translation memory", () => {
  const source = "Fast curing";
  const glossary: GlossaryEntry[] = [{
    id: "document", source_term: source, target_term: "Dokumentni prevod", case_sensitive: true,
    mode: "exact_segment", enabled: true, origin: "user", scope: "document", status: "approved",
  }];
  const job = createTranslationJob(fixture(), [], {
    source_language: "en", target_language: "sl", glossary,
    translation_memory: [tmEntry(source, "Prevod spomina")],
  });
  const segment = job.segments.find((item) => item.source_text === source)!;
  assert.equal(segment.translation_origin, "glossary");
  assert.equal(segment.edited_translation, "Dokumentni prevod");
  assert.equal(segment.translation_memory_entry_id, null);
});

test("approved manual persistent glossary outranks an approved external candidate", () => {
  const source = "Open time";
  const analysis = fixture();
  analysis.document!.pages[0].regions[1].blocks = [block(1, "heading", source)];
  const shared = { source_term: source, case_sensitive: true, mode: "exact_segment" as const, enabled: true, origin: "user" as const, scope: "persistent_user" as const, status: "approved" as const };
  const glossary: GlossaryEntry[] = [
    { ...shared, id: "external", target_term: "Zunanji kandidat", revision: 1, source: "import", external_source: "Synthetic TBX" },
    { ...shared, id: "company", target_term: "Odobren podjetniški termin", revision: 4, source: "manual" },
  ];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl", glossary });
  const segment = job.segments.find((item) => item.source_text === source)!;
  assert.equal(segment.edited_translation, "Odobren podjetniški termin");
  assert.equal(segment.glossary_entry_id, "company");
  assert.equal(segment.status, "translated");
  assert.equal(segment.needs_review, true);
});

test("approved external candidate remains unreviewed and keeps source provenance", () => {
  const source = "Open time";
  const analysis = fixture();
  analysis.document!.pages[0].regions[1].blocks = [block(1, "heading", source)];
  const glossary: GlossaryEntry[] = [{
    id: "external", source_term: source, target_term: "Odprti čas", case_sensitive: true,
    mode: "exact_segment", enabled: true, origin: "user", scope: "persistent_user",
    status: "approved", revision: 2, source: "import", domain: "adhesives",
    external_source: "Synthetic TBX", external_record_id: "term-1",
  }];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl", glossary });
  const segment = job.segments.find((item) => item.source_text === source)!;
  const usage = job.glossary_usage.find((item) => item.glossary_id === "external");
  assert.equal(segment.edited_translation, "Odprti čas");
  assert.equal(segment.status, "translated");
  assert.equal(segment.needs_review, true);
  assert.notEqual(segment.status, "reviewed");
  assert.equal(usage?.revision, 2);
  assert.equal(usage?.external_source, "Synthetic TBX");
  assert.equal(usage?.external_record_id, "term-1");
  assert.equal(usage?.domain, "adhesives");
});

test("external candidate affects only jobs created after explicit approval", () => {
  const source = "Open time";
  const analysis = fixture();
  analysis.document!.pages[0].regions[1].blocks = [block(1, "heading", source)];
  const candidate: GlossaryEntry = {
    id: "external-lifecycle", source_term: source, target_term: "Odprti čas", case_sensitive: true,
    mode: "exact_segment", enabled: true, origin: "user", scope: "persistent_user",
    revision: 1, source: "import", status: "proposed", domain: "adhesives",
    external_source: "Synthetic TBX", external_record_id: "term-lifecycle",
  };
  const prepare = (status: "proposed" | "approved" | "rejected" | "archived", revision: number) =>
    createTranslationJob(analysis, [], {
      source_language: "en", target_language: "sl", glossary: [{ ...candidate, status, revision }],
    });

  assert.equal(prepare("proposed", 1).segments.find((item) => item.source_text === source)!.glossary_entry_id, null);
  const approvedJob = prepare("approved", 2);
  const approved = approvedJob.segments.find((item) => item.source_text === source)!;
  const usage = approvedJob.glossary_usage.find((item) => item.glossary_id === "external-lifecycle");
  assert.equal(approved.edited_translation, "Odprti čas");
  assert.equal(approved.glossary_entry_id, "external-lifecycle");
  assert.equal(usage?.revision, 2);
  assert.equal(usage?.domain, "adhesives");
  assert.equal(usage?.external_source, "Synthetic TBX");
  assert.equal(approved.status, "translated");
  assert.equal(approved.needs_review, true);
  assert.equal(prepare("rejected", 3).segments.find((item) => item.source_text === source)!.glossary_entry_id, null);
  assert.equal(prepare("archived", 4).segments.find((item) => item.source_text === source)!.glossary_entry_id, null);
});

test("rejects a technically invalid memory result and falls back to the provider with provenance", async () => {
  const source = "Use CO₂ at +25 °C and 2.5 MPa with 50 % coverage.";
  const entry = tmEntry(source, "Uporabite pri +20 °C in 3 MPa.");
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl", translation_memory: [entry] });
  const prepared = job.segments.find((item) => item.source_text === source)!;
  assert.equal(prepared.status, "pending");
  assert.equal(prepared.translation_origin, null);
  assert.match(prepared.warnings.join("\n"), new RegExp(`translation_memory_rejected:${entry.id}:rev:${entry.revision}:technical_integrity`));
  assert.equal(job.translation_memory_usage.length, 0);
  const translated = await translateJobBatch(job, new DeterministicProvider((text) => text));
  const segment = translated.segments.find((item) => item.source_text === source)!;
  assert.equal(segment.status, "translated");
  assert.equal(segment.translation_origin, "provider");
  assert.equal(segment.translation_memory_entry_id, null);
  assert.match(segment.warnings.join("\n"), new RegExp(`translation_memory_rejected:${entry.id}:rev:${entry.revision}:technical_integrity`));
  assert.equal(translated.translation_memory_usage.length, 0);
});

test("accepts only safe typographic percentage spacing in technical integrity checks", () => {
  const accepted: Array<[string, string]> = [
    ["72%", "72\u00a0%"],
    ["100%", "100\u00a0%"],
    ["76%", "76\u00a0%"],
    ["72 %", "72%"],
    ["72% 100% 76%", "72\u00a0% 100 % 76%"],
    ["72% and 72%", "72\u00a0% in 72 %"],
    ["72.5%", "72,5\u00a0%"],
  ];
  for (const [source, target] of accepted) {
    const result = validateTranslationTechnicalIntegrity(source, target);
    assert.equal(result.valid, true, `${source} -> ${target}`);
  }
  assert.equal(
    validateTranslationTechnicalIntegrity("72% 100% 76%", "72\u00a0% 100\u00a0% 76\u00a0%").normalizedTarget,
    "72\u00a0% 100\u00a0% 76\u00a0%",
  );
});

test("rejects percentage changes and damage to other protected technical tokens", () => {
  const rejected: Array<[string, string]> = [
    ["72%", "73\u00a0%"],
    ["-72%", "72\u00a0%"],
    ["72%", "72"],
    ["72%", "72 percent"],
    ["72%", "72\u00a0°C"],
    ["72%", "7 2%"],
    ["72%", "72  %"],
    ["72%", "72\t%"],
    ["72.5%", "725\u00a0%"],
    ["72% 100%", "72\u00a0%"],
    ["72%", "72\u00a0% 72\u00a0%"],
    ["72% and 72%", "72\u00a0%"],
    ["72% 100%", "100\u00a0% 72\u00a0%"],
    ["CO₂ 72%", "CO2 72\u00a0%"],
    ["2.5 MPa 72%", "2,6\u00a0MPa 72\u00a0%"],
    ["290 ml 20 L 72%", "291\u00a0ml 20\u00a0L 72\u00a0%"],
    ["RAL 9001 72%", "RAL 9002 72\u00a0%"],
    ["+25 °C 72%", "+26\u00a0°C 72\u00a0%"],
    ["{{coverage}} 72%", "{{amount}} 72\u00a0%"],
    ["https://example.test/72 72%", "https://example.test/73 72\u00a0%"],
    ["quality72@example.test 72%", "quality73@example.test 72\u00a0%"],
    ["batch-72 72%", "batch-73 72\u00a0%"],
    ["ACME MS 72 Panel Bond 72%", "ACME MS 73 Panel Bond 72\u00a0%"],
  ];
  for (const [source, target] of rejected) {
    assert.equal(validateTranslationTechnicalIntegrity(source, target).valid, false, `${source} -> ${target}`);
  }
});

test("preserves mixed TM plus provider provenance for three deterministic sentence chunks", async () => {
  const analysis = fixture();
  const source = "Apply the product. Keep the surface clean. Allow it to dry.";
  analysis.document!.pages[0].regions = [{
    kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1,
    blocks: [block(1, "paragraph", source)],
  }];
  const provider = new DeterministicProvider((_text, index) => index === 0 ? "Površina naj ostane čista." : "Pustite, da se posuši.");
  const job = createTranslationJob(analysis, [], {
    source_language: "en", target_language: "sl",
    translation_memory: [tmEntry("Apply the product.", "Nanesite izdelek.")],
  });
  const translated = await translateJobBatch(job, provider);
  assert.equal(provider.requests.flat().length, 2);
  const segment = translated.segments[0];
  assert.equal(segment.edited_translation, "Nanesite izdelek. Površina naj ostane čista. Pustite, da se posuši.");
  assert.equal(segment.status, "translated");
  assert.equal(segment.translation_origin, "mixed");
  assert.equal(segment.needs_review, true);
  assert.deepEqual(segment.chunks.map((chunk) => chunk.translation_origin), ["translation_memory", "provider", "provider"]);
  assert.equal(segment.chunks[0].translation_memory_entry_id, "tm-1");
  assert.equal(segment.chunks[0].translation_memory_revision, 2);
  assert.equal(segment.chunks[0].translation_memory_match, "exact");
  assert.equal(segment.chunks[0].translation_memory_domain, "");
  assert.ok(segment.chunks.slice(1).every((chunk) => !chunk.translation_memory_entry_id));
  assert.equal(translated.translation_memory_usage[0].entry_id, "tm-1");
  assert.equal(translated.translation_memory_usage.length, 1);
  const roundTrip = importTranslationJob(exportTranslationJob(translated));
  assert.equal(roundTrip.segments[0].translation_origin, "mixed");
  assert.deepEqual(roundTrip.segments[0].chunks.map((chunk) => chunk.translation_origin), ["translation_memory", "provider", "provider"]);
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
  const changed = { ...item, effective_text: "Changed OCR", ocr_text: "Changed OCR" };
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
  assert.equal(restoreTechnicalTokens(protectedValue.protected_text, protectedValue.tokens), "CO₂, od 5 do 25\u00a0°C, 2,5\u00a0MPa, 50\u00a0%, RAL 9001, 290\u00a0ml, 600\u00a0ml, 20\u00a0L and 200\u00a0L.");
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

  for (const source of ["version 1.2.3", "code AB-3.14", "identifier v1.2", "en.9.9", "IP 192.168.1.59", "ISO 1.59", "DIN 3.14", "serial 1.59", "https://example.test/en.9.9"]) {
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

test("protects composite technical labels and rejects damaged structure", () => {
  const protectedValue = protectTechnicalTokens("Classification + / T 1 remains valid.");
  assert.ok(protectedValue.tokens.some((token) => token.value === "+ / T 1"));
  assert.equal(restoreTechnicalTokens(protectedValue.protected_text, protectedValue.tokens), "Classification + / T 1 remains valid.");
  assert.throws(() => restoreTechnicalTokens(protectedValue.protected_text.replace("TK000TK", "+ / T1"), protectedValue.tokens), /placeholder/);
  assert.equal(validateTranslationTechnicalIntegrity("Classification + / T 1", "Razvrstitev + / T1").valid, false);
});

test("splits repeated structural markers and pipe fields without losing order", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const template = job.segments.find((segment) => segment.block_type === "paragraph")!;
  const bullets = createTranslationChunks({ ...template, source_text: "Ð First item Ð Second item Ð Third item" });
  assert.deepEqual(bullets.map((chunk) => chunk.source_text), ["Ð First item", "Ð Second item", "Ð Third item"]);
  assert.equal(bullets.map((chunk) => chunk.source_text + chunk.separator_after).join(""), "Ð First item Ð Second item Ð Third item");

  const fields = createTranslationChunks({ ...template, source_text: "Address 14 | +49 8684 908-0 | info@example.test" });
  assert.deepEqual(fields.map((chunk) => chunk.source_text), ["Address 14", "+49 8684 908-0", "info@example.test"]);
  assert.equal(fields.map((chunk) => chunk.source_text + chunk.separator_after).join(""), "Address 14 | +49 8684 908-0 | info@example.test");
});

test("records distinct glossary occurrences by offsets and deduplicates only identical events", () => {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{
    kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1,
    blocks: [block(1, "paragraph", "Use sealant, then inspect the sealant.")],
  }];
  const glossary: GlossaryEntry[] = [{
    id: "sealant", source_term: "sealant", target_term: "tesnilna masa", case_sensitive: false,
    mode: "preferred", enabled: true, origin: "user", scope: "persistent_user", status: "approved", revision: 2,
  }];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl", glossary });
  const matches = job.segments[0].glossary_matches;
  assert.deepEqual(matches.map((match) => [match.start_offset, match.end_offset]), [[4, 11], [30, 37]]);
  assert.deepEqual(job.glossary_usage.map((usage) => [usage.start_offset, usage.end_offset]), [[4, 11], [30, 37]]);
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
    ["-40 °C to +90 °C", "od −40 do 90\u00a0°C", "temperature"],
    ["from -40 °C to +90 °C", "od −40 do 90\u00a0°C", "temperature"],
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
  assert.equal(segment.translated_text, "Temperaturna odpornost od −40 do 90\u00a0°C");
  assert.equal(segment.status, "translated");
  assert.equal(segment.needs_review, true);
  assert.equal(segment.translation_rules_version, TECHNICAL_RANGE_RULES_VERSION);
  assert.deepEqual(segment.translation_rule_adjustments, [{
    rule_id: "technical_range",
    rule_version: TECHNICAL_RANGE_RULES_VERSION,
    kind: "temperature",
    source_text: "-40 °C to +90 °C",
    replacement_text: "od −40 do 90\u00a0°C",
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
    domain: "construction_adhesives",
    external_source: "Synthetic user TBX",
    external_record_id: "term-65",
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
  const usage = translated.glossary_usage.find((item) => item.glossary_id === "product-name");
  assert.equal(usage?.revision, 2);
  assert.equal(usage?.domain, "construction_adhesives");
  assert.equal(usage?.external_source, "Synthetic user TBX");
  assert.equal(usage?.external_record_id, "term-65");
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
  const job = updateTranslationJobSegment(base, base.segments[0].segment_id, (segment) => reviewTranslation(editTranslation(segment, "UPORABA")));
  assert.deepEqual(importTranslationJob(exportTranslationJob(job)), job);
  assert.throws(() => importTranslationJob("not-json"), /veljaven JSON/);
  assert.throws(() => importTranslationJob('{"schema_version":11}'), /ni podprta/);
});

test("schema four import stays compatible without claiming the new profile", () => {
  const current = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const legacy = JSON.parse(exportTranslationJob(current)) as Record<string, unknown>;
  legacy.schema_version = 4;
  delete legacy.translation_profile;
  delete legacy.quality_checklist;
  const migrated = importTranslationJob(JSON.stringify(legacy));
  assert.equal(migrated.schema_version, 10);
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

test("import compatibility explains that a stale document model requires reanalysis", () => {
  const expected = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const imported = importTranslationJob(exportTranslationJob(expected));
  const stale = {
    ...imported,
    document_identity: imported.document_identity
      ? { ...imported.document_identity, document_model_version: imported.document_identity.document_model_version - 1 }
      : null,
  };

  assert.throws(
    () => validateTranslationJobCompatibility(stale, expected),
    /PDF ponovno analizirajte; uvožena datoteka opravila ostane nespremenjena/,
  );
  assert.equal(stale.segments[0].source_text, imported.segments[0].source_text);
  assert.equal(stale.segments[0].edited_translation, imported.segments[0].edited_translation);
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
  assert.equal(migrated.schema_version, 10);
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
  assert.equal(migrated.schema_version, 10);
  assert.equal(migrated.segments[0].source_text, current.segments[0].source_text);
  assert.deepEqual(migrated.segments[0].chunks, []);
});

test("schema seven mixed chunks migrate without inventing TM provenance", async () => {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{
    kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1,
    blocks: [block(1, "paragraph", "Apply the product. Keep the surface clean.")],
  }];
  const translated = await translateJobBatch(createTranslationJob(analysis, [], {
    source_language: "en", target_language: "sl",
    translation_memory: [tmEntry("Apply the product.", "Nanesite izdelek.")],
  }), new DeterministicProvider(() => "Površina naj ostane čista."));
  const legacy = JSON.parse(exportTranslationJob(translated)) as Record<string, unknown>;
  legacy.schema_version = 7;
  legacy.segments = (legacy.segments as Array<Record<string, unknown>>).map((segment) => ({
    ...segment,
    translation_origin: segment.translation_origin === "mixed" ? "provider" : segment.translation_origin,
    chunks: (segment.chunks as Array<Record<string, unknown>>).map((chunk) => {
      const copy = { ...chunk };
      delete copy.translation_origin;
      delete copy.translation_memory_match;
      return copy;
    }),
  }));
  const migrated = importTranslationJob(JSON.stringify(legacy));
  assert.equal(migrated.schema_version, 10);
  assert.equal(migrated.segments[0].translation_origin, "mixed");
  assert.deepEqual(migrated.segments[0].chunks.map((chunk) => chunk.translation_origin), ["translation_memory", "provider"]);
});

test("unknown future schema and provenance are rejected rather than silently discarded", () => {
  const current = JSON.parse(exportTranslationJob(createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" }))) as Record<string, unknown>;
  current.schema_version = 99;
  assert.throws(() => importTranslationJob(JSON.stringify(current)), /Različica/);
  const malformed = JSON.parse(exportTranslationJob(createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" }))) as Record<string, unknown>;
  (malformed.segments as Array<Record<string, unknown>>)[0].translation_origin = "future_origin";
  assert.throws(() => importTranslationJob(JSON.stringify(malformed)), /neveljaven prevajalski segment/);
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
  assert.equal(migrated.schema_version, 10);
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

test("manual correction keeps provider history but recomputes current placeholder QC", async () => {
  const source = "Support the bonded materials for at least 24 hours after application.";
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{
    kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1,
    blocks: [block(1, "paragraph", source)],
  }];
  const initial = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  const failed = await translateJobBatch(initial, new DeterministicProvider(() => "Podprite materiale vsaj 24 ur po nanosu."));
  assert.equal(failed.segments[0].status, "failed");
  assert.ok(failed.segments[0].warnings.includes("placeholder_integrity"));

  const corrected = updateTranslationJobSegment(failed, failed.segments[0].segment_id, (segment) =>
    editTranslation(segment, "Podprite lepljene materiale vsaj 24 ur po nanosu."),
  );
  assert.ok(corrected.segments[0].warnings.includes("placeholder_integrity"));
  assert.equal(corrected.segments[0].placeholder_attempts.length, failed.segments[0].placeholder_attempts.length);
  assert.equal(corrected.segments[0].current_qc?.status, "passed");
  assert.equal(corrected.segments[0].status, "translated");
  assert.equal(corrected.segments[0].needs_review, true);
});

test("current QC blocks a manual edit that changes a protected value or unit", () => {
  const source = "Use 2.5 MPa for 24 hours.";
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{
    kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1,
    blocks: [block(1, "paragraph", source)],
  }];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  const missingUnit = updateTranslationJobSegment(job, job.segments[0].segment_id, (segment) =>
    editTranslation(segment, "Uporabite 2,5 za 24 ur."),
  );
  assert.equal(missingUnit.segments[0].current_qc?.status, "blocked");
  assert.ok(missingUnit.segments[0].current_qc?.findings.some((finding) => finding.code === "technical_integrity"));

  const changedValue = updateTranslationJobSegment(job, job.segments[0].segment_id, (segment) =>
    editTranslation(segment, "Uporabite 3 MPa za 24 ur."),
  );
  assert.equal(changedValue.segments[0].current_qc?.status, "blocked");
});

test("preferred terminology warning follows the current edited translation", () => {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{
    kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1,
    blocks: [block(1, "paragraph", "Fast curing")],
  }];
  const glossary: GlossaryEntry[] = [{
    id: "preferred-fast-curing", source_term: "Fast curing", target_term: "hitro utrjevanje",
    case_sensitive: false, mode: "preferred", enabled: true, origin: "user", scope: "document", status: "approved", revision: 1,
  }];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl", glossary });
  const missing = updateTranslationJobSegment(job, job.segments[0].segment_id, (segment) => editTranslation(segment, "Hitro sušenje"));
  assert.ok(missing.segments[0].current_qc?.findings.some((finding) => finding.code === "preferred_term_missing"));
  const present = updateTranslationJobSegment(missing, missing.segments[0].segment_id, (segment) => editTranslation(segment, "hitro utrjevanje"));
  assert.ok(!present.segments[0].current_qc?.findings.some((finding) => finding.code === "preferred_term_missing"));
  const removed = updateTranslationJobSegment(present, present.segments[0].segment_id, (segment) => editTranslation(segment, "Hitro sušenje"));
  assert.ok(removed.segments[0].current_qc?.findings.some((finding) => finding.code === "preferred_term_missing"));
});

test("review is revision-bound while repeated QC keeps an unchanged review", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const id = job.segments[0].segment_id;
  const reviewed = updateTranslationJobSegment(job, id, (segment) => reviewTranslation(editTranslation(segment, "UPORABA")));
  assert.equal(reviewed.segments[0].status, "reviewed");
  assert.equal(reviewed.segments[0].review_state?.origin, "local_user_action");
  const rechecked = updateTranslationJobSegment(reviewed, id, (segment) => ({ ...segment }));
  assert.equal(rechecked.segments[0].status, "reviewed");
  assert.deepEqual(rechecked.segments[0].review_state, reviewed.segments[0].review_state);
  const edited = updateTranslationJobSegment(rechecked, id, (segment) => editTranslation(segment, "DRUGA UPORABA"));
  assert.equal(edited.segments[0].status, "translated");
  assert.equal(edited.segments[0].review_state, null);
});

test("schema nine review migrates without inventing reviewer identity", () => {
  const current = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const reviewed = updateTranslationJobSegment(current, current.segments[0].segment_id, (segment) => reviewTranslation(editTranslation(segment, "UPORABA")));
  const legacy = JSON.parse(exportTranslationJob(reviewed)) as Record<string, unknown>;
  legacy.schema_version = 9;
  legacy.segments = (legacy.segments as Array<Record<string, unknown>>).map((segment) => {
    const copy = { ...segment };
    delete copy.review_state;
    delete copy.current_qc;
    return copy;
  });
  const imported = importTranslationJob(JSON.stringify(legacy));
  assert.equal(imported.schema_version, 10);
  assert.equal(imported.segments[0].status, "reviewed");
  assert.equal(imported.segments[0].review_state?.origin, "legacy_unknown");
});

test("current QC rejects extra missing and altered protected data while allowing existing typography rules", () => {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{
    kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1,
    blocks: [block(1, "paragraph", "Use CO₂ at -40 °C and 2.5 MPa for 24 hours with 72% coverage. RAL 9001 {{code}}")],
  }];
  const job = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  const correct = "Uporabite CO₂ pri −40\u00a0°C in 2,5\u00a0MPa za 24 ur z 72\u00a0% pokritostjo. RAL 9001 {{code}}";
  const checked = (text: string) => updateTranslationJobSegment(job, job.segments[0].segment_id, (segment) => editTranslation(segment, text)).segments[0];
  assert.ok(checked(correct).current_qc?.findings.every((finding) => finding.code === "technical_range_ambiguous"));
  for (const broken of [
    correct.replace("24", ""), correct.replace("MPa", "kPa"), correct.replace("CO₂", "CO2"),
    correct.replace("RAL 9001", "RAL 9002"), correct.replace("{{code}}", ""), `${correct} 17`,
  ]) assert.equal(checked(broken).current_qc?.status, "blocked", broken);
});

test("editing a reviewed translation to empty leaves QC unchecked and never reuses the old target", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const id = job.segments[0].segment_id;
  const reviewed = updateTranslationJobSegment(job, id, (segment) => reviewTranslation(editTranslation({ ...segment, translated_text: "UPORABA" }, "UPORABA")));
  const emptied = updateTranslationJobSegment(reviewed, id, (segment) => editTranslation(segment, ""));
  assert.equal(emptied.segments[0].status, "pending");
  assert.equal(emptied.segments[0].current_qc?.status, "not_checked");
  assert.equal(emptied.segments[0].review_state, null);
});

test("QC configuration changes are recorded without changing the reviewed wording", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const id = job.segments[0].segment_id;
  const reviewed = updateTranslationJobSegment(job, id, (segment) => reviewTranslation(editTranslation(segment, "UPORABA")));
  const next = refreshTranslationProfileMetadata({ ...reviewed, glossary: [{
    id: "current-term", source_term: reviewed.segments[0].source_text, target_term: "Priporočeni izraz",
    case_sensitive: true, mode: "preferred", enabled: true, origin: "user", scope: "document", status: "approved",
  }] });
  assert.notEqual(next.segments[0].current_qc?.rules_revision, reviewed.segments[0].current_qc?.rules_revision);
  assert.equal(next.segments[0].status, "reviewed");
  assert.equal(next.segments[0].edited_translation, "UPORABA");
  assert.ok(next.segments[0].current_qc?.findings.some((finding) => finding.code === "preferred_term_missing"));
});

test("import recomputes a stale QC result and invalidates a review bound to different wording", () => {
  const job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const id = job.segments[0].segment_id;
  const reviewed = updateTranslationJobSegment(job, id, (segment) => reviewTranslation(editTranslation(segment, "UPORABA")));
  const stale = JSON.parse(exportTranslationJob(reviewed));
  stale.segments[0].edited_translation = "DRUGA UPORABA";
  const imported = importTranslationJob(JSON.stringify(stale));
  assert.equal(imported.segments[0].status, "translated");
  assert.equal(imported.segments[0].review_state, null);
  assert.notEqual(imported.segments[0].current_qc?.content_revision, reviewed.segments[0].current_qc?.content_revision);
});

test("a delayed provider result or failure cannot overwrite a newer manual edit and review", async () => {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{
    kind: "body", bbox: { x0: 0, y0: 0, x1: 100, y1: 50 }, reading_order: 1,
    blocks: [block(1, "paragraph", "Keep the surface clean."), block(2, "paragraph", "Use a clean cloth.")],
  }];
  const requested = createTranslationJob(analysis, [], { source_language: "en", target_language: "sl" });
  let release!: () => void;
  const delayed = new Promise<void>((resolve) => { release = resolve; });
  const provider: TranslationProvider = { id: "delayed-test", async translateBatch(requests) {
    await delayed;
    return requests.map((request) => ({ segment_id: request.segment_id, translated_text: "Odgovor ponudnika.", confidence: null, error: null }));
  } };
  const pending = translateJobBatch(requested, provider);
  const running = { ...requested, segments: requested.segments.map((segment) => ({ ...segment, status: "translating" as const })) };
  const edited = updateTranslationJobSegment(running, running.segments[0].segment_id, (segment) => reviewTranslation(editTranslation(segment, "Površina naj bo čista.")));
  release();
  const result = await pending;
  const merged = mergeCurrentTranslationResult(edited, requested, result);
  assert.equal(merged.segments[0].edited_translation, "Površina naj bo čista.");
  assert.equal(merged.segments[0].status, "reviewed");
  assert.deepEqual(merged.segments[0].review_state, edited.segments[0].review_state);
  assert.equal(merged.segments[1].edited_translation, "Odgovor ponudnika.");
  const failed = mergeCurrentTranslationResult(edited, requested, requested);
  assert.equal(failed.segments[0].edited_translation, "Površina naj bo čista.");
  assert.equal(failed.segments[0].status, "reviewed");
  const otherDocument = { ...edited, document_identity: { ...edited.document_identity!, sha256: "f".repeat(64) } };
  assert.equal(mergeCurrentTranslationResult(otherDocument, requested, result), otherDocument);
});

test("temperature range integrity compares ordered signed bounds and shared units only", () => {
  const cases = [
    ["-40 °C to +90 °C", "od −40 do +90\u00a0°C"],
    ["+5 °C to 35 °C", "od 5 do 35 °C"],
    ["-12.5 °C to +86 °C", "od −12,5 °C do 86 °C"],
    ["Resistance -40 °C to +90 °C, RAL 9001", "Odpornost od -40 do 90 °C, RAL 9001"],
  ];
  for (const [source, target] of cases) {
    assert.equal(validateTranslationTechnicalIntegrity(source, target).valid, true, target);
    assert.equal(validateTranslationTechnicalIntegrity(target, source).valid, true, source);
  }
  const source = "-40 °C to +90 °C";
  for (const target of [
    "od 40 do 90 °C", "od −41 do 90 °C", "od −40 do 91 °C", "od −40 do −90 °C",
    "od 90 do −40 °C", "od −40 do 90 °F", "od −40 do 90 K", "od −40 do 90",
    "od −40 K do 90 °C", "od −40 do 90 °C, 90 °C", "od −4 0 do 90 °C",
    "od −40 do 9,0 °C", "od −40 do 90 °C, RAL 9001", "od −40 °C in 90 °C",
  ]) assert.equal(validateTranslationTechnicalIntegrity(source, target).valid, false, target);
  assert.equal(validateTranslationTechnicalIntegrity("+5 °C", "5 °C").valid, false, "isolated quantity stays strict");
});

test("digit-leading designations retain letters and cannot be replaced by an explanation or another code", () => {
  const source = "Use 2K paint, 3D parts and 20 L.";
  assert.ok(protectTechnicalTokens(source).tokens.some(token => token.value === "2K" && token.kind === "product_code"));
  assert.equal(validateTranslationTechnicalIntegrity(source, "Uporabite dvokomponentno barvo (2K), dele 3D in 20 L.").valid, true);
  for (const target of ["Uporabite barvo 2X, dele 3D in 20 L.", "Uporabite barvo 2, dele 3D in 20 L.", "Uporabite dvokomponentno barvo, dele 3D in 20 L."]) {
    assert.equal(validateTranslationTechnicalIntegrity(source, target).valid, false, target);
  }
});

test("new temperature QC refresh preserves valid content review and reports exact genuine differences", () => {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{kind: "body", bbox: {x0: 0,y0: 0,x1: 100,y1: 50}, reading_order: 1,
    blocks: [block(1, "paragraph", "-40 °C to +90 °C")] }];
  const job = createTranslationJob(analysis, [], {source_language: "en",target_language: "sl"});
  const reviewed = updateTranslationJobSegment(job, job.segments[0].segment_id, s => reviewTranslation(editTranslation(s, "od −40 do 90 °C")));
  const current = refreshTranslationProfileMetadata(reviewed);
  assert.equal(current.segments[0].status, "reviewed");
  assert.deepEqual(current.segments[0].review_state, reviewed.segments[0].review_state);
  assert.equal(current.segments[0].current_qc?.status, "passed");
  assert.ok(current.segments[0].current_qc?.rules_revision.includes("current-segment-qc-v2"));
  const wrong = updateTranslationJobSegment(current, current.segments[0].segment_id, s => editTranslation(s, "od 40 do 90 °C"));
  assert.equal(wrong.segments[0].status, "translated");
  assert.equal(wrong.segments[0].current_qc?.status, "blocked");
  assert.match(wrong.segments[0].current_qc!.findings[0].message, /Pričakovano:.*-40.*Zaznano:.*40/);
});

test("preferred term QC uses whole words and never guesses Slovenian inflection", () => {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{kind: "body", bbox: {x0: 0,y0: 0,x1: 100,y1: 50}, reading_order: 1,
    blocks: [block(1, "paragraph", "Use sealant with fibre cement panels.")] }];
  const glossary: GlossaryEntry[] = [
    {id:"term-a",source_term:"sealant",target_term:"tesnilna masa",case_sensitive:false,mode:"preferred",enabled:true,origin:"user"},
    {id:"term-b",source_term:"fibre cement panels",target_term:"vlaknocementne plošče",case_sensitive:false,mode:"preferred",enabled:true,origin:"user"},
  ];
  const job = createTranslationJob(analysis, [], {source_language:"en",target_language:"sl",glossary});
  const warnings = (target: string) => updateTranslationJobSegment(job,job.segments[0].segment_id,s=>editTranslation(s,target)).segments[0].current_qc!.findings.filter(f=>f.code==="preferred_term_missing");
  assert.equal(warnings("Tesnilna masa, vlaknocementne plošče.").length,0);
  assert.equal(warnings("Tesnilna masaaa, vlaknocementne plošče.").length,1);
  assert.equal(warnings("Tesnilne mase, vlaknocementne plošče.").length,1,"no unsupported automatic inflection");
  assert.equal(warnings("Tesnilna masa, vlakna cementne plošče.").length,1,"real terminology error remains visible");
});

test("OCR recovery retains all translations but invalidates only the unproven source review", () => {
  const analysis = fixture();
  const box = analysis.document!.pages[0].regions.find(r=>r.kind==="header")!.blocks[0].bbox;
  const accepted = {...acceptedOcr(),pdf_bbox:box};
  const candidate = {...accepted,status:"pending" as const,selected_text_source:"native" as const,effective_text:accepted.native_text,ocr_text:null,server_revision_id:null};
  const imported = createTranslationJob(analysis,[accepted],{source_language:"en",target_language:"sl"});
  // Synthetic fixture reviews are test setup, not human approval of real documents.
  const reviewed = importTranslationJob(exportTranslationJob({...imported,segments:imported.segments.map(s=>s.skip_reason?s:reviewTranslation(editTranslation(s,s.source_text)))}));
  const expected = createTranslationJob(analysis,[candidate],{source_language:"en",target_language:"sl"});
  const original = JSON.stringify(reviewed);
  const recovered = recoverTranslationJobForAnalysis(reviewed,expected,[candidate]);
  const ocr = recovered.segments.find(s=>s.source_text_origin==="accepted_ocr")!;
  assert.equal(ocr.edited_translation,"APPLICATIONS");
  assert.equal(ocr.source_text,"APPLICATIONS");
  assert.equal(ocr.status,"translated");
  assert.equal(ocr.review_state,null);
  assert.equal(ocr.accepted_ocr_source?.revision_id,null);
  assert.equal(ocr.accepted_ocr_source?.review_id,candidate.id);
  assert.ok(ocr.warnings.includes("ocr_recovery_required"));
  assert.equal(JSON.stringify(reviewed),original);
  for(const s of recovered.segments.filter(s=>s.segment_id!==ocr.segment_id)) {
    assert.deepEqual(s,reviewed.segments.find(old=>old.segment_id===s.segment_id));
  }
  assert.deepEqual(importTranslationJob(exportTranslationJob(recovered)).segments,recovered.segments);
  const stillPending = invalidateStaleOcrApprovals(recovered,[candidate]);
  assert.equal(stillPending.segments[0].accepted_ocr_source?.review_id,candidate.id);
  const relinked = invalidateStaleOcrApprovals(stillPending,[{...accepted,server_revision_id:"new-explicit-acceptance"}]);
  assert.equal(relinked.segments[0].accepted_ocr_source?.revision_id,"new-explicit-acceptance");
  assert.equal(relinked.segments[0].status,"translated");
  assert.equal(relinked.segments[0].review_state,null);
  const changedText = invalidateStaleOcrApprovals(recovered,[{...accepted,effective_text:"OTHER HEADING",server_revision_id:"new-revision"}]);
  assert.equal(changedText.segments[0].source_text,"OTHER HEADING");
  assert.equal(changedText.segments[0].edited_translation,"APPLICATIONS");
  assert.equal(changedText.segments[0].review_state,null);
});

test("OCR recovery rejects another PDF or unproven block rectangle and never adopts a moved acceptance", () => {
  const analysis=fixture();
  const box=analysis.document!.pages[0].regions.find(r=>r.kind==="header")!.blocks[0].bbox;
  const accepted={...acceptedOcr(),pdf_bbox:box};
  const candidate={...accepted,status:"pending" as const,selected_text_source:"native" as const,effective_text:accepted.native_text,server_revision_id:null};
  const imported=createTranslationJob(analysis,[accepted],{source_language:"en",target_language:"sl"});
  const expected=createTranslationJob(analysis,[candidate],{source_language:"en",target_language:"sl"});
  const other={...expected,document_identity:{...expected.document_identity!,sha256:"b".repeat(64)}};
  assert.throws(()=>recoverTranslationJobForAnalysis(imported,other,[candidate]));
  for(const changed of [
    {...candidate,pdf_bbox:{...box,x0:box.x0+1}}, {...candidate,page_number:2},
    {...candidate,linked_block_reading_order:2}, {...candidate,native_text:"Different native block"},
    {...candidate,source:"manual" as const}, {...candidate,id:"different-review"},
  ]) assert.throws(()=>recoverTranslationJobForAnalysis(imported,expected,[changed]));
  const recovered=recoverTranslationJobForAnalysis(imported,expected,[candidate]);
  const moved=invalidateStaleOcrApprovals(recovered,[{...accepted,pdf_bbox:{...box,x0:box.x0+1},server_revision_id:"moved"}]);
  assert.equal(moved.segments[0].accepted_ocr_source?.revision_id,null);
  assert.deepEqual(moved.segments[0].source_bbox,box);
});

test("server-verified unchanged OCR survives fresh analysis and JSON round trips without a new review", () => {
  const analysis=fixture();
  const box=analysis.document!.pages[0].regions.find(r=>r.kind==="header")!.blocks[0].bbox;
  const accepted={...acceptedOcr(),pdf_bbox:box};
  const candidate={...accepted,status:"pending" as const,selected_text_source:"native" as const,effective_text:accepted.native_text,ocr_text:null,server_revision_id:null};
  const original=createTranslationJob(analysis,[accepted],{source_language:"en",target_language:"sl"});
  // Synthetic user actions, not approval of real document translations.
  const approved={...original,segments:original.segments.map(s=>s.source_text_origin==="accepted_ocr"?reviewTranslation(editTranslation(s,"UPORABA")):s)};
  const imported=importTranslationJob(exportTranslationJob(approved));
  const expected=createTranslationJob(analysis,[candidate],{source_language:"en",target_language:"sl"});
  const restored=restoreVerifiedOcrItems(imported,expected,[candidate],new Set([accepted.server_revision_id!]));
  const rebound=recoverTranslationJobForAnalysis(imported,createTranslationJob(analysis,restored,{source_language:"en",target_language:"sl"}),restored);
  const before=imported.segments.find(s=>s.source_text_origin==="accepted_ocr")!;
  const after=rebound.segments.find(s=>s.segment_id===before.segment_id)!;
  assert.equal(after.status,"reviewed");
  assert.deepEqual(after.review_state,before.review_state);
  assert.deepEqual(after.accepted_ocr_source,before.accepted_ocr_source);
  assert.equal(hasUnresolvedOcrSources(rebound),false);
  const historicalWarning={...rebound,segments:rebound.segments.map(s=>({...s,warnings:[...s.warnings,"ocr_recovery_required"]}))};
  assert.equal(hasUnresolvedOcrSources(historicalWarning),false);
  assert.deepEqual(historicalWarning.segments.find(s=>s.segment_id===after.segment_id)!.review_state,before.review_state);
  assert.deepEqual(importTranslationJob(exportTranslationJob(rebound)).segments,rebound.segments);
  assert.deepEqual(invalidateStaleOcrApprovals(rebound,restored).segments,rebound.segments);
  const notReviewed=updateTranslationJobSegment(imported,before.segment_id,s=>editTranslation(s,"DRUGA UPORABA"));
  const stillNotReviewed=recoverTranslationJobForAnalysis(notReviewed,createTranslationJob(analysis,restored,{source_language:"en",target_language:"sl"}),restored);
  assert.equal(stillNotReviewed.segments.find(s=>s.segment_id===before.segment_id)!.status,"translated");
  const edited=updateTranslationJobSegment(rebound,after.segment_id,s=>editTranslation(s,"DRUGA UPORABA"));
  assert.equal(edited.segments.find(s=>s.segment_id===after.segment_id)!.review_state,null);
  const unknown=restoreVerifiedOcrItems(imported,expected,[candidate],new Set());
  const rejected=recoverTranslationJobForAnalysis(imported,expected,unknown);
  assert.equal(hasUnresolvedOcrSources(rejected),true);
  assert.equal(rejected.segments.find(s=>s.segment_id===after.segment_id)!.status,"translated");
  assert.throws(()=>restoreVerifiedOcrItems(imported,{...expected,document_identity:{...expected.document_identity!,sha256:"b".repeat(64)}},[candidate],new Set([accepted.server_revision_id!])));
  assert.throws(()=>restoreVerifiedOcrItems(imported,expected,[{...candidate,pdf_bbox:{...box,x0:box.x0+1}}],new Set([accepted.server_revision_id!])));
});


// Synthetic proposal automation; no real user review is created here.
function terminologyFixture() {
  const analysis = fixture();
  analysis.document!.pages[0].regions = [{kind:"body",bbox:{x0:0,y0:0,x1:100,y1:50},reading_order:1,
    blocks:[block(1,"paragraph","Durable coating.")]}];
  const glossary: GlossaryEntry[] = [{id:"coating",source_term:"coating",target_term:"premaz",case_sensitive:false,
    mode:"preferred",enabled:true,origin:"user"}];
  return createTranslationJob(analysis,[],{source_language:"en",target_language:"sl",domain:"technical_datasheets",glossary});
}
test("full-segment terminology retry selects verified candidate and keeps proposal history", async () => {
  const base=terminologyFixture();
  const provider=new DeterministicProvider((_text,_index,_call,request)=>request.context.terminology_retry?"Trajen premaz.":"Trajna obloga.");
  const result=await translateJobBatch(base,provider); const segment=result.segments[0];
  assert.equal(segment.edited_translation,"Trajen premaz."); assert.equal(segment.status,"translated");
  assert.equal(segment.needs_review,true); assert.equal(segment.review_state,null);
  assert.equal(provider.requests[1][0].context.domain,"technical_datasheets");
  assert.equal(provider.requests[1][0].text,"Durable coating.");
  assert.equal(segment.terminology_attempts?.[0].before,"Trajna obloga.");
  assert.ok(segment.terminology_attempts?.[0].before_chunks?.length);
  const imported=importTranslationJob(exportTranslationJob(result));
  assert.equal(imported.domain,"technical_datasheets");
  assert.deepEqual(imported.segments[0].terminology_attempts,segment.terminology_attempts);
  await translateJobBatch(result,provider); assert.equal(provider.calls,2);
});
test("terminology retry rejects invented protected data and retains the previous draft", async () => {
  const provider=new DeterministicProvider((_text,_i,_call,r)=>r.context.terminology_retry?"Premaz 99 kg.":"Trajna obloga.");
  const result=await translateJobBatch(terminologyFixture(),provider);
  assert.equal(result.segments[0].edited_translation,"Trajna obloga.");
  assert.equal(result.segments[0].terminology_attempts?.[0].selected,null);
});
test("terminology retry cancellation never installs a late result", async () => {
  const base=terminologyFixture(); const controller=new AbortController();
  const provider=new DeterministicProvider((_text,_i,_call,r)=>{
    if(r.context.terminology_retry) controller.abort(); return r.context.terminology_retry?"Trajen premaz.":"Trajna obloga.";
  });
  assert.equal(await translateJobBatch(base,provider,controller.signal),base);
});
test("exact approved memory bypasses terminology rewrite and remains unreviewed", async () => {
  const base=terminologyFixture(); base.translation_memory_snapshot=[tmEntry("Durable coating.","Trajna obloga.",{domain:"technical_datasheets"})];
  const provider=new DeterministicProvider(); const result=await translateJobBatch(base,provider);
  assert.equal(result.segments[0].translation_origin,"translation_memory");
  assert.equal(result.segments[0].edited_translation,"Trajna obloga.");
  assert.equal(result.segments[0].status,"translated"); assert.equal(provider.calls,0);
});

test("fresh exact glossary proposals receive SI formatting without altering existing reviews", async () => {
  const base=terminologyFixture();
  base.segments[0].source_text="Coating 2.50 MPa";
  base.glossary=[{id:"exact-measurement",source_term:"Coating 2.50 MPa",target_term:"Premaz 2.50MPa",case_sensitive:true,
    mode:"exact_segment",enabled:true,origin:"user"}];
  const provider=new DeterministicProvider();const result=await translateJobBatch(base,provider);
  assert.equal(result.segments[0].edited_translation,"Premaz 2,50\u00a0MPa");
  assert.equal(result.segments[0].translation_origin,"glossary"); assert.equal(result.segments[0].status,"translated");
  assert.equal(result.segments[0].needs_review,true); assert.equal(provider.calls,0);
  const manual=updateTranslationJobSegment(result,result.segments[0].segment_id,s=>reviewTranslation(editTranslation(s,"Premaz 2.50 MPa")));
  const next=await translateJobBatch(manual,provider);
  assert.equal(next.segments[0].edited_translation,"Premaz 2.50 MPa");
  assert.deepEqual(next.segments[0].review_state,manual.segments[0].review_state);
});

test("whole-segment terminology retry cannot overwrite an approved memory chunk", async () => {
  const base=terminologyFixture();base.segments[0].source_text="Coating protects. Durable coating.";
  base.translation_memory_snapshot=[tmEntry("Coating protects.","Obloga varuje.",{domain:"technical_datasheets"})];
  const provider=new DeterministicProvider((_text,_i,_call,r)=>r.context.terminology_retry?"Premaz varuje. Trajen premaz.":"Trajna obloga.");
  const result=await translateJobBatch(base,provider);const segment=result.segments[0];
  assert.equal(segment.translation_origin,"mixed");
  assert.equal(segment.edited_translation,"Obloga varuje. Trajna obloga.");
  assert.equal(segment.terminology_attempts?.[0].reason,"review_required_for_memory_alignment");
  assert.equal(segment.terminology_attempts?.[0].selected,null);
  assert.ok(segment.chunks.some(c=>c.translation_memory_entry_id==="tm-1"));
});

test("a suspicious native source is never displayed as ordinary successful QC", () => {
  const analysis=fixture();const item=acceptedOcr();const job=createTranslationJob(analysis,[],{source_language:"en",target_language:"sl"});
  const native=job.segments.find(s=>s.source_text==="qPPLICATIONS")!;
  const matching={...item,pdf_bbox:native.source_bbox!,status:"pending" as const,native_text:native.source_text};
  const warning=nativeSourceReviewWarning(native,[matching]);assert.ok(warning);
  const passed={...native,current_qc:{content_revision:"test",rules_revision:"test",status:"passed" as const,findings:[]}};
  assert.match(sourceAwareQcLabel(passed,warning),/^Pregled izvora potreben/);
  assert.match(sourceAwareQcLabel({...passed,current_qc:{...passed.current_qc,status:"blocked"}},warning),/Blokada/);
  assert.equal(sourceAwareQcLabel(passed,null),"Zaščiteni podatki preverjeni");
  assert.equal(nativeSourceReviewWarning({...native,source_text_origin:"accepted_ocr"},[matching]),null);
});

test("Slovenian temperature proposals use od-do, a shared unit and no redundant plus", () => {
  for (const [source,target] of [
    ["-12.50 °C to +37.0 °C","od −12,50 do 37,0\u00a0°C"],
    ["+6 °C to 48 °C","od 6 do 48\u00a0°C"],
    ["from -8 °F to +33 °F","od −8 do 33\u00a0°F"],
    ["280 K to 310 K","od 280 do 310\u00a0K"],
  ]) {
    const protectedValue=protectTechnicalTokens(source);
    assert.equal(restoreTechnicalTokens(protectedValue.protected_text,protectedValue.tokens),target);
    assert.equal(validateTranslationTechnicalIntegrity(source,target).valid,true);
  }
  for (const target of ["od 12,50 do 37,0 °C","od −12,51 do 37,0 °C","od −12,50 do 37,0 K","od 37,0 do −12,50 °C"])
    assert.equal(validateTranslationTechnicalIntegrity("-12.50 °C to +37.0 °C",target).valid,false);
  const codes=protectTechnicalTokens("2K and 4K paints; RAL 9001; 45°.");
  assert.equal(restoreTechnicalTokens(codes.protected_text,codes.tokens),"2K and 4K paints; RAL 9001; 45°.");
});

test("temperature style changes in an existing reviewed job require explicit preview and new review", () => {
  let job=terminologyFixture();job=updateTranslationJobSegment(job,job.segments[0].segment_id,s=>reviewTranslation(editTranslation({...s,source_text:"-12.50 °C to +37.0 °C"},"−12,50 °C do +37,0 °C")));
  const original=JSON.stringify(job);const preview=previewMeasurementNormalization(job);
  assert.equal(JSON.stringify(job),original);assert.equal(preview.changes.length,1);
  assert.equal(preview.changes[0].after,"od −12,50 do 37,0\u00a0°C");
  const applied=applyMeasurementNormalization(job,preview);
  assert.equal(applied.segments[0].status,"translated");assert.equal(applied.segments[0].review_state,null);
  assert.equal(applied.segments[0].source_text,"-12.50 °C to +37.0 °C");
  assert.deepEqual(proposeSiMeasurements("https://example.test/5°Cto10°C; ID 5 °C to 10 °C; RAL 9001; 2K").changes,[]);
  assert.ok(!proposeSiMeasurements("5 °C/s to 10 °C/s").changes.some(c=>c.after.startsWith("od ")));
});
void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });


test("relative humidity preview is explicit, idempotent and invalidates only changed reviews", () => {
  let job = createTranslationJob(fixture(), [], { source_language: "en", target_language: "sl" });
  const id = job.segments.find(s => s.source_text === "Fast curing")!.segment_id;
  job = updateTranslationJobSegment(job, id, s => reviewTranslation(editTranslation({ ...s,
    source_text: "10 min (20 °C /50 % Rh.)" }, "10 min (20 °C / 50 % r.v.)")));
  const before = JSON.stringify(job);
  const preview = previewMeasurementNormalization(job);
  assert.equal(JSON.stringify(job), before);
  const proposed = preview.changes.find(c => c.segment_id === id)!;
  assert.equal(proposed.after, "10\u00a0min (20\u00a0°C / 50\u00a0%\u00a0r. v.)");
  assert.equal(proposed.reviewed, true);
  const applied = applyMeasurementNormalization(job, preview);
  const target = applied.segments.find(s => s.segment_id === id)!;
  assert.equal(target.source_text, "10 min (20 °C /50 % Rh.)");
  assert.equal(target.status, "translated"); assert.equal(target.review_state, null);
  assert.equal(target.needs_review, true);
  assert.equal(target.current_qc?.findings.some(f => f.severity === "blocking"), false);
  assert.equal(previewMeasurementNormalization(applied).changes.some(c => c.segment_id === id), false);
  assert.ok(target.translation_rule_adjustments?.some(r => r.rule_version === SI_RULES_VERSION));
  const untouched = proposeSiMeasurements("https://example.test/50%Rh. ID 50%Rh.");
  assert.deepEqual(untouched.changes, []);
});
