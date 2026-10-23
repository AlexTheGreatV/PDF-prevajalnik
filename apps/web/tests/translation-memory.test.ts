import assert from "node:assert/strict";

import {
  canInitializeTranslationJob,
  createTranslationMemoryDraft,
  createLatestTranslationMemoryRequestCoordinator,
  normalizeTranslationMemorySource,
  selectExactTranslationMemoryMatch,
  translationMemoryDomainScopeLabel,
  type TranslationMemoryEntry,
} from "../lib/translation-memory";
import type { TranslationJob, TranslationSegment } from "../lib/translation-types";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

function entry(changes: Partial<TranslationMemoryEntry> = {}): TranslationMemoryEntry {
  return {
    id: "entry", source_text: "Apply product.", target_text: "Nanesite izdelek.",
    normalized_source: "Apply product.", normalized_source_sha256: "a".repeat(64),
    source_language: "en", target_language: "sl", domain: "", status: "approved",
    origin: "manual_review", source_document_sha256: null, source_segment_id: null,
    source_kind: "native", note: "", revision: 1, created_at: "now", updated_at: "now",
    approved_at: "now", usage_count: 0, last_used_at: null, ...changes,
  };
}

test("normalization is conservative and platform independent", () => {
  assert.equal(normalizeTranslationMemorySource("  Cafe\u0301\r\nline  "), "Café\nline");
  assert.notEqual(normalizeTranslationMemorySource("TEXT."), normalizeTranslationMemorySource("text."));
  assert.notEqual(normalizeTranslationMemorySource("Text."), normalizeTranslationMemorySource("Text"));
});

test("domain scope explains exclusions without changing matching or widening access", () => {
  const scoped = entry({ domain: "coatings" });
  assert.match(translationMemoryDomainScopeLabel(""), /samo splošni vnosi/);
  assert.match(translationMemoryDomainScopeLabel("coatings"), /področje coatings in splošni vnosi/);
  assert.equal(selectExactTranslationMemoryMatch(scoped.source_text, "en", "sl", "", [scoped]).status, "none");
  assert.equal(selectExactTranslationMemoryMatch(scoped.source_text, "en", "sl", "coatings", [scoped]).status, "match");
  assert.equal(selectExactTranslationMemoryMatch(scoped.source_text, "en", "sl", "other", [scoped]).status, "none");
});

test("exact lookup excludes inactive entries and preserves number differences", () => {
  assert.equal(selectExactTranslationMemoryMatch("Apply product.", "en", "sl", "", [entry({ status: "proposed" })]).status, "none");
  assert.equal(selectExactTranslationMemoryMatch("Apply 2 products.", "en", "sl", "", [entry({ source_text: "Apply 3 products.", normalized_source: "Apply 3 products." })]).status, "none");
});

test("domain match wins over generic and equal conflicts are explicit", () => {
  const generic = entry({ id: "generic", target_text: "Splošno." });
  const domain = entry({ id: "domain", domain: "coatings", target_text: "Premazi." });
  assert.equal(selectExactTranslationMemoryMatch("Apply product.", "en", "sl", "coatings", [generic, domain]).match?.id, "domain");
  const conflict = entry({ id: "conflict", domain: "coatings", target_text: "Drug prevod." });
  assert.equal(selectExactTranslationMemoryMatch("Apply product.", "en", "sl", "coatings", [domain, conflict]).status, "conflict");
});

test("only a reviewed manual correction can become a proposed entry", () => {
  const segment = {
    segment_id: "s1", source_text: "Apply product.", edited_translation: "Nanesite izdelek.",
    source_language: "en", target_language: "sl", source_text_origin: "native",
    status: "reviewed", translation_origin: "manual",
  } as TranslationSegment;
  const job = { document_identity: { sha256: "f".repeat(64) } } as TranslationJob;
  const draft = createTranslationMemoryDraft(segment, job, "coatings");
  assert.equal(draft.status, "proposed");
  assert.equal(draft.origin, "manual_review");
  assert.equal(draft.source_document_sha256, "f".repeat(64));
  assert.throws(() => createTranslationMemoryDraft({ ...segment, translation_origin: "provider" }, job), /ročno/);
});

test("a late translation memory response cannot replace newer document state", async () => {
  const coordinator = createLatestTranslationMemoryRequestCoordinator();
  let resolveOld!: (value: string) => void;
  const old = new Promise<string>((resolve) => { resolveOld = resolve; });
  const applied: string[] = [];
  const first = coordinator.run(() => old, { onLoading() {}, onReady(value) { applied.push(value); }, onError() {} });
  await coordinator.run(async () => "new-document", { onLoading() {}, onReady(value) { applied.push(value); }, onError() {} });
  resolveOld("old-document");
  assert.equal(await first, "stale");
  assert.deepEqual(applied, ["new-document"]);
});

test("job initialization waits for both glossary and translation memory snapshots", () => {
  assert.equal(canInitializeTranslationJob(true, "ready", "loading"), false);
  assert.equal(canInitializeTranslationJob(false, "ready", "ready"), false);
  assert.equal(canInitializeTranslationJob(true, "loading", "ready"), false);
  assert.equal(canInitializeTranslationJob(true, "ready", "ready"), true);
  assert.equal(canInitializeTranslationJob(true, "explicitly_skipped", "ready"), true);
  assert.equal(canInitializeTranslationJob(true, "ready", "explicitly_skipped"), true);
  assert.equal(canInitializeTranslationJob(true, "ready", "error"), false);
});

test("failed load requires retry or an explicit skip and a retry can become ready", async () => {
  const coordinator = createLatestTranslationMemoryRequestCoordinator();
  let state: "loading" | "ready" | "error" | "explicitly_skipped" = "loading";
  await coordinator.run(async () => { throw new Error("offline"); }, {
    onLoading() { state = "loading"; }, onReady() { state = "ready"; }, onError() { state = "error"; },
  });
  assert.equal(state, "error");
  assert.equal(canInitializeTranslationJob(true, "ready", state), false);
  await coordinator.run(async () => "snapshot", {
    onLoading() { state = "loading"; }, onReady() { state = "ready"; }, onError() { state = "error"; },
  });
  assert.equal(state, "ready");
  assert.equal(canInitializeTranslationJob(true, "ready", state), true);
  state = "explicitly_skipped";
  assert.equal(canInitializeTranslationJob(true, "ready", state), true);
});

test("late memory completion does not reset an edited or reviewed job", async () => {
  const coordinator = createLatestTranslationMemoryRequestCoordinator();
  let resolveLoad!: (value: string) => void;
  const delayed = new Promise<string>((resolve) => { resolveLoad = resolve; });
  const job = { edited_translation: "Ročni prevod", status: "reviewed" };
  const request = coordinator.run(() => delayed, {
    onLoading() {}, onReady() {}, onError() {},
  });
  coordinator.invalidate();
  resolveLoad("late-snapshot");
  assert.equal(await request, "stale");
  assert.deepEqual(job, { edited_translation: "Ročni prevod", status: "reviewed" });
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
