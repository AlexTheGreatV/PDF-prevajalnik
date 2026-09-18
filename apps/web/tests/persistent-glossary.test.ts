import assert from "node:assert/strict";
import {
  createReviewedCorrectionDraft,
  isGlossarySnapshotStale,
  persistentToRuntimeEntry,
  previewGlossaryCsv,
  recordGlossaryUsage,
  type PersistentGlossaryEntry,
} from "../lib/persistent-glossary";
import type { TranslationJob, TranslationSegment } from "../lib/translation-types";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

const entry: PersistentGlossaryEntry = {
  id: "entry-1", source_term: "curing", target_term: "utrjevanje", source_language: "en", target_language: "sl",
  domain: "gradbeništvo", match_mode: "preferred_term", case_sensitive: false, status: "approved",
  origin: "manual", note: "", source_label: null, document_hash: null, revision: 3,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", usage_count: 0, last_used_at: null,
};

test("maps an approved persistent entry without losing revision or provenance", () => {
  assert.deepEqual(persistentToRuntimeEntry(entry), {
    id: "entry-1", source_term: "curing", target_term: "utrjevanje", case_sensitive: false,
    mode: "preferred", enabled: true, origin: "user", scope: "persistent_user", revision: 3,
    status: "approved", source: "manual",
  });
});

test("creates a proposed entry only from a reviewed manual correction", () => {
  const segment = {
    status: "reviewed", translation_origin: "manual", edited_translation: "Odprti čas",
    source_text: "Open time", source_language: "en", target_language: "sl",
  } as TranslationSegment;
  const draft = createReviewedCorrectionDraft(segment, "lepila");
  assert.equal(draft.source_term, "Open time");
  assert.equal(draft.target_term, "Odprti čas");
  assert.equal(draft.origin, "reviewed_correction");
  assert.equal(draft.status, "proposed");
  assert.throws(() => createReviewedCorrectionDraft({ ...segment, status: "translated" }), /potrjen prevod/);
});

test("detects a changed active glossary snapshot without mutating the job", () => {
  const job = { glossary_set_hash: "old-hash" } as TranslationJob;
  assert.equal(isGlossarySnapshotStale(job, "new-hash"), true);
  assert.equal(isGlossarySnapshotStale(job, "old-hash"), false);
  assert.equal(job.glossary_set_hash, "old-hash");
});

test("records unique usage IDs only through the configured local API", async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = "";
  let requestBody = "";
  let contentType = "";
  globalThis.fetch = (async (input, init) => {
    requestedUrl = String(input);
    requestBody = String(init?.body);
    contentType = new Headers(init?.headers).get("content-type") ?? "";
    return new Response(null, { status: 204 });
  }) as typeof fetch;
  try {
    await recordGlossaryUsage(["entry-1", "entry-1", "entry-2"]);
    assert.equal(new URL(requestedUrl).origin, "http://127.0.0.1:8000");
    assert.equal(new URL(requestedUrl).pathname, "/api/glossary/usage");
    assert.deepEqual(JSON.parse(requestBody), { entry_ids: ["entry-1", "entry-2"] });
    assert.equal(contentType, "application/json");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("previews CSV locally without writing before explicit confirmation", async () => {
  const originalFetch = globalThis.fetch;
  let requestedPath = "";
  let requestBody: BodyInit | null | undefined;
  globalThis.fetch = (async (input, init) => {
    requestedPath = new URL(String(input)).pathname;
    requestBody = init?.body;
    return Response.json({
      valid_rows: [], new_count: 1, duplicate_count: 1, conflict_count: 1,
      invalid_count: 1, override_count: 1, issues: [],
    });
  }) as typeof fetch;
  try {
    const result = await previewGlossaryCsv(new File(["source_term,target_term\ncuring,utrjevanje"], "glossary.csv", { type: "text/csv" }));
    assert.equal(requestedPath, "/api/glossary/imports/csv/preview");
    assert.ok(requestBody instanceof FormData);
    assert.equal(result.conflict_count, 1);
    assert.equal(result.override_count, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("shows a clear local glossary error returned by the API", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => Response.json({ detail: "CSV vsebuje nepričakovana polja: secret." }, { status: 422 })) as typeof fetch;
  try {
    await assert.rejects(
      previewGlossaryCsv(new File(["secret\nvalue"], "bad.csv", { type: "text/csv" })),
      /nepričakovana polja/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
