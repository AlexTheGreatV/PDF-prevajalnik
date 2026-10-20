import assert from "node:assert/strict";

import {
  createExclusiveLatestAction,
  determineGuidedStep,
  hasRetainedNativeTextWarnings,
  readWorkspaceMode,
  readWorkspaceModeFromWindow,
  retainedNativeTextWarning,
  writeWorkspaceMode,
  type WorkspaceMode,
} from "../lib/guided-workflow";
import type { OcrReviewItem } from "../lib/ocr-review";
import type { PdfExportPlan } from "../lib/pdf-export-client";
import type { TranslationJob, TranslationSegment } from "../lib/translation-types";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

function segment(status: TranslationSegment["status"], id = "segment-1"): TranslationSegment {
  return {
    segment_id: id, page_number: 1, block_id: id, block_type: "paragraph",
    source_language: "en", target_language: "sl", source_text: "Source", source_text_origin: "native",
    source_bbox: null, source_page_type: "digital", source_rotation: 0, source_style: null,
    accepted_ocr_source: null, translated_text: status === "pending" ? "" : "Prevod",
    edited_translation: status === "pending" ? "" : "Prevod", status, quality: null, error: null,
    skip_reason: null, protected_tokens: [], glossary_matches: [], translation_origin: status === "pending" ? null : "provider",
    glossary_entry_id: null, model_id: status === "pending" ? null : "hplt", provider_id: status === "pending" ? null : "local",
    placeholder_attempts: [], needs_review: status !== "reviewed", warnings: [], chunks: [], forced_chunking: false,
    review_state: status === "reviewed" ? { content_revision: "fixture", origin: "legacy_unknown" } : null,
    current_qc: null,
    created_at: "2026-09-25T00:00:00Z",
  };
}

function job(segments: TranslationSegment[]): TranslationJob {
  return {
    schema_version: 10, source_filename: "sample.pdf", source_language: "en", target_language: "sl", domain: "",
    created_at: "2026-09-25T00:00:00Z", document_identity: null, glossary: [], glossary_snapshot: [],
    glossary_set_hash: null, glossary_usage: [], translation_memory_snapshot: [], translation_memory_set_hash: null,
    translation_memory_usage: [], segments, translation_profile: null, quality_checklist: [],
  };
}

function input(overrides: Record<string, unknown> = {}) {
  return {
    hasFile: true, analysisReady: true, missingTextPages: 0, ocrItems: [],
    glossaryStatus: "ready" as const, translationMemoryStatus: "ready" as const,
    translatorAvailable: true, translationRunning: false, job: null, exportPlan: null, exportDownloaded: false,
    ...overrides,
  };
}

test("guides file selection and analysis as separate first actions", () => {
  assert.equal(determineGuidedStep(input({ hasFile: false, analysisReady: false })).step, "select_pdf");
  assert.equal(determineGuidedStep(input({ analysisReady: false })).step, "analyze_document");
});

test("a scanned page without acquired text requires OCR review", () => {
  assert.equal(determineGuidedStep(input({ missingTextPages: 1 })).step, "review_text_issues");
});

test("an unresolved OCR candidate blocks a success path", () => {
  const item = { status: "suggested", native_text: "xEADING", server_revision_id: null } as OcrReviewItem;
  assert.equal(determineGuidedStep(input({ ocrItems: [item] })).step, "review_text_issues");
});

test("text acquisition outranks resource, translation, review, and export state", () => {
  const item = { status: "suggested", native_text: "xEADING", server_revision_id: null } as OcrReviewItem;
  const plan = { summary: { eligible: 1, skipped: 0 }, ocr_summary: { eligible_regions: 0 } } as unknown as PdfExportPlan;
  const result = determineGuidedStep(input({
    ocrItems: [item],
    glossaryStatus: "error",
    job: job([segment("reviewed")]),
    exportPlan: plan,
    exportDownloaded: true,
  }));
  assert.equal(result.step, "review_text_issues");
});

test("a glossary or translation-memory error blocks silent preparation", () => {
  assert.equal(determineGuidedStep(input({ glossaryStatus: "error" })).blocked, true);
  assert.equal(determineGuidedStep(input({ translationMemoryStatus: "error" })).blocked, true);
});

test("resource readiness outranks translation even when a stale job is present", () => {
  const result = determineGuidedStep(input({ glossaryStatus: "loading", job: job([segment("pending")]) }));
  assert.equal(result.step, "prepare_translation");
  assert.equal(result.blocked, true);
});

test("ready local resources lead to deterministic preparation", () => {
  assert.equal(determineGuidedStep(input()).step, "prepare_translation");
});

test("a prepared job uses the ready local HPLT action", () => {
  const result = determineGuidedStep(input({ job: job([segment("pending")]) }));
  assert.equal(result.step, "translate");
  assert.equal(result.blocked, false);
});

test("a translated but unreviewed segment enters focused review", () => {
  assert.equal(determineGuidedStep(input({ job: job([segment("translated")]) })).step, "review_translations");
});

test("a partially reviewed document remains in review", () => {
  assert.equal(determineGuidedStep(input({ job: job([segment("reviewed"), segment("translated", "segment-2")]) })).step, "review_translations");
});

test("all reviewed translations proceed to PDF preparation", () => {
  assert.equal(determineGuidedStep(input({ job: job([segment("reviewed")]) })).step, "prepare_pdf");
});

test("a partial export plan still offers a clearly partial download", () => {
  const plan = { summary: { eligible: 1, skipped: 1 }, ocr_summary: { eligible_regions: 0 } } as unknown as PdfExportPlan;
  assert.equal(determineGuidedStep(input({ job: job([segment("reviewed")]), exportPlan: plan })).step, "download_pdf");
});

test("only a completed download reaches the final guided state", () => {
  const plan = { summary: { eligible: 1 }, ocr_summary: { eligible_regions: 0 } } as unknown as PdfExportPlan;
  assert.equal(determineGuidedStep(input({ job: job([segment("reviewed")]), exportPlan: plan, exportDownloaded: true })).step, "complete");
});

test("workspace mode is presentation state and is absent from TranslationJob", () => {
  const mode: WorkspaceMode = "advanced";
  const current = job([segment("translated")]);
  assert.equal(mode, "advanced");
  assert.equal("workspace_mode" in current, false);
  assert.equal(current.segments[0].status, "translated");
});

test("invalid or inaccessible local storage safely keeps simple mode", () => {
  assert.equal(readWorkspaceMode({ getItem: () => "damaged", setItem() {} }), "simple");
  assert.equal(readWorkspaceMode({ getItem: () => { throw new DOMException("blocked", "SecurityError"); }, setItem() {} }), "simple");
  assert.equal(writeWorkspaceMode({ getItem: () => null, setItem: () => { throw new DOMException("blocked", "SecurityError"); } }, "advanced"), false);
  const blockedWindow = Object.create(null, {
    localStorage: { get: () => { throw new DOMException("blocked", "SecurityError"); } },
  }) as { readonly localStorage: Storage };
  assert.equal(readWorkspaceModeFromWindow(blockedWindow), "simple");
});

test("workspace storage contains only the selected presentation mode", () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  assert.equal(writeWorkspaceMode(storage, "advanced"), true);
  assert.deepEqual([...values.values()], ["advanced"]);
  assert.equal(readWorkspaceMode(storage), "advanced");
});

test("exclusive latest actions reject double starts and stale completion", () => {
  const action = createExclusiveLatestAction();
  const first = action.begin();
  assert.equal(typeof first, "number");
  assert.equal(action.begin(), null);
  action.invalidate();
  assert.equal(action.isCurrent(first!), false);
  const second = action.begin();
  assert.equal(action.isCurrent(second!), true);
  assert.equal(action.finish(first!), false);
  assert.equal(action.finish(second!), true);
});

test("retaining suspicious native OCR text shows a review warning without changing it", () => {
  const item = { status: "rejected", reason: "suspicious_glyph", native_text: "xEADING" } as const;
  assert.match(retainedNativeTextWarning(item) ?? "", /sumljiv izvorni zapis/i);
  assert.equal(item.native_text, "xEADING");
  assert.equal(retainedNativeTextWarning({ ...item, status: "accepted" }), null);
  assert.equal(hasRetainedNativeTextWarnings([item as OcrReviewItem]), true);
  assert.equal(hasRetainedNativeTextWarnings([{ ...item, status: "accepted" } as OcrReviewItem]), false);
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
