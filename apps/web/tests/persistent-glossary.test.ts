import assert from "node:assert/strict";
import {
  createLatestGlossaryRequestCoordinator,
  createReviewedCorrectionDraft,
  isGlossarySnapshotStale,
  persistentToRuntimeEntry,
  previewGlossaryCsv,
  recordGlossaryUsage,
  resolveGlossarySnapshot,
  type PersistentGlossaryEntry,
} from "../lib/persistent-glossary";
import type { TranslationJob, TranslationSegment } from "../lib/translation-types";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
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

test("publishes a delayed glossary snapshot exactly once after it becomes ready", async () => {
  const delayed = deferred<{ entries: PersistentGlossaryEntry[]; hash: string }>();
  const coordinator = createLatestGlossaryRequestCoordinator();
  let loading = 0;
  let prepared = 0;
  let snapshotMode = "";
  const running = coordinator.run(() => delayed.promise, {
    onLoading: () => { loading += 1; },
    onReady: (value) => { snapshotMode = value.entries[0].match_mode; prepared += 1; },
    onError: () => assert.fail("load should succeed"),
  });
  assert.equal(loading, 1);
  assert.equal(prepared, 0);
  delayed.resolve({ entries: [{ ...entry, match_mode: "do_not_translate" }], hash: "snapshot-1" });
  assert.equal(await running, "applied");
  assert.equal(prepared, 1);
  assert.equal(snapshotMode, "do_not_translate");
});

test("only resolves a deterministic translation snapshot after loading or explicit skip", () => {
  const documentEntry = persistentToRuntimeEntry({ ...entry, id: "document-entry" });
  documentEntry.scope = "document";
  const persistentEntry = persistentToRuntimeEntry({ ...entry, id: "persistent-entry", match_mode: "do_not_translate" });
  const builtInEntry = { ...documentEntry, id: "built-in", scope: "built_in" as const, origin: "built_in" as const };
  assert.equal(resolveGlossarySnapshot([documentEntry], { status: "loading" }, [builtInEntry]), null);
  assert.equal(resolveGlossarySnapshot([documentEntry], { status: "error", message: "offline" }, [builtInEntry]), null);
  assert.deepEqual(
    resolveGlossarySnapshot([documentEntry], { status: "ready", entries: [persistentEntry], active_set_hash: "hash" }, [builtInEntry])?.map((item) => item.id),
    ["document-entry", "persistent-entry", "built-in"],
  );
  assert.deepEqual(
    resolveGlossarySnapshot([documentEntry], { status: "explicitly_skipped" }, [builtInEntry])?.map((item) => item.id),
    ["document-entry", "built-in"],
  );
});

test("has the same ready snapshot when the glossary resolves before document preparation", async () => {
  const coordinator = createLatestGlossaryRequestCoordinator();
  let snapshotHash = "";
  await coordinator.run(async () => ({ hash: "snapshot-1" }), {
    onLoading: () => undefined,
    onReady: (value) => { snapshotHash = value.hash; },
    onError: () => assert.fail("load should succeed"),
  });
  assert.equal(snapshotHash, "snapshot-1");
});

test("keeps a failed glossary load explicit and allows a successful retry", async () => {
  const coordinator = createLatestGlossaryRequestCoordinator();
  const states: string[] = [];
  await coordinator.run(async () => { throw new Error("API ni dosegljiv"); }, {
    onLoading: () => states.push("loading"),
    onReady: () => states.push("ready"),
    onError: () => states.push("error"),
  });
  await coordinator.run(async () => ({ hash: "retry-hash" }), {
    onLoading: () => states.push("loading"),
    onReady: () => states.push("ready"),
    onError: () => states.push("error"),
  });
  assert.deepEqual(states, ["loading", "error", "loading", "ready"]);
});

test("ignores a late glossary response after the document request is invalidated", async () => {
  const delayed = deferred<{ hash: string }>();
  const coordinator = createLatestGlossaryRequestCoordinator();
  let ready = 0;
  const running = coordinator.run(() => delayed.promise, {
    onLoading: () => undefined,
    onReady: () => { ready += 1; },
    onError: () => assert.fail("stale load should be ignored"),
  });
  coordinator.invalidate();
  delayed.resolve({ hash: "old-document" });
  assert.equal(await running, "stale");
  assert.equal(ready, 0);
});

test("does not reset edited work when an old glossary request finishes late", async () => {
  const delayed = deferred<{ hash: string }>();
  const coordinator = createLatestGlossaryRequestCoordinator();
  const edited = { status: "reviewed", text: "Ročno potrjeno" };
  const running = coordinator.run(() => delayed.promise, {
    onLoading: () => undefined,
    onReady: () => assert.fail("late response must not update existing work"),
    onError: () => assert.fail("late response must not update existing work"),
  });
  coordinator.invalidate();
  delayed.resolve({ hash: "late" });
  await running;
  assert.deepEqual(edited, { status: "reviewed", text: "Ročno potrjeno" });
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
