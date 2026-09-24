import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  confirmTerminologyImport,
  previewTerminologyImport,
  readCsvHeaders,
  TERMINOLOGY_PREVIEW_BUTTON_LABEL,
  terminologyPreviewValidation,
  type TerminologyImportPreview,
} from "../lib/terminology-import";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

const preview: TerminologyImportPreview = {
  preview_id: "preview-1", import_batch_id: "batch-1", expires_at: "2026-09-21T12:00:00Z",
  format: "csv", file_sha256: "a".repeat(64), candidates: [], valid_count: 1, new_count: 1,
  duplicate_count: 0, conflict_count: 0, invalid_count: 0, missing_translation_count: 0,
  missing_language_count: 0, issues: [], domains: ["lepila"], ignored_fields: ["unused"],
  external_source: "local test", source_version: null, license_info: null,
};

test("renders a named preview button and explains why it is disabled", () => {
  const component = readFileSync(resolve(process.cwd(), "components/terminology-import-panel.tsx"), "utf8");
  const button = component.split(/\r?\n/).find((line) => line.includes("terminology-preview-button")) ?? "";
  assert.match(button, /disabled=\{previewDisabled\}/);
  assert.match(button, /aria-describedby="terminology-preview-help"/);
  assert.doesNotMatch(button, /aria-label=/);
  assert.equal(TERMINOLOGY_PREVIEW_BUTTON_LABEL, "Pripravi predogled");
  assert.match(component, /Vnesite dejanski vir podatkov zaradi sledljivosti\./);
  assert.match(component, /Zunanji vir <RequiredMark \/>/);
});

test("enables preview only after required file metadata and CSV mapping are ready", () => {
  const incomplete = terminologyPreviewValidation({
    file_selected: true, format: "csv", source_language: "en", target_language: "sl",
    external_source: "", csv_mapping: { source_term: "source", target_term: "" },
  });
  assert.deepEqual(incomplete, [
    "Vnesite dejanski zunanji vir zaradi sledljivosti.",
    "Preslikajte stolpca za izvorni in ciljni termin.",
  ]);
  assert.deepEqual(terminologyPreviewValidation({
    file_selected: true, format: "csv", source_language: "en", target_language: "sl",
    external_source: "Lokalni test 8E1", csv_mapping: { source_term: "source", target_term: "target" },
  }), []);
  assert.deepEqual(terminologyPreviewValidation({
    file_selected: true, format: "tbx", source_language: "en", target_language: "sl",
    external_source: "Lokalni test 8E1", csv_mapping: { source_term: "", target_term: "" },
  }), []);
});

test("reads quoted CSV headers locally for explicit mapping", async () => {
  const file = new File(['"source, term",target,domain\nvalue,prevod,lepila'], "terms.csv", { type: "text/csv" });
  assert.deepEqual(await readCsvHeaders(file), ["source, term", "target", "domain"]);
});

test("sends terminology preview only to the configured local API", async () => {
  const originalFetch = globalThis.fetch;
  let requestUrl = "";
  let requestBody: BodyInit | null | undefined;
  let requestCount = 0;
  globalThis.fetch = (async (input, init) => {
    requestCount += 1;
    requestUrl = String(input); requestBody = init?.body;
    return Response.json(preview);
  }) as typeof fetch;
  try {
    const result = await previewTerminologyImport(
      new File(["source,target\ncuring,strjevanje"], "terms.csv", { type: "text/csv" }),
      "csv",
      {
        source_language: "en", target_language: "sl", default_domain: "lepila",
        default_match_mode: "preferred_term", external_source: "local test",
        csv_mapping: { source_term: "source", target_term: "target" },
      },
    );
    const url = new URL(requestUrl);
    assert.equal(url.origin, "http://127.0.0.1:8000");
    assert.equal(url.pathname, "/api/glossary/imports/terminology/preview");
    assert.equal(requestCount, 1);
    assert.notEqual(url.pathname, "/api/glossary/imports/terminology/confirm");
    assert.ok(requestBody instanceof FormData);
    assert.equal(result.file_sha256, "a".repeat(64));
  } finally { globalThis.fetch = originalFetch; }
});

test("confirms only selected server-preview candidate identifiers", async () => {
  const originalFetch = globalThis.fetch;
  let body = "";
  globalThis.fetch = (async (_input, init) => {
    body = String(init?.body);
    return Response.json({ import_batch_id: "batch-1", created: 1, skipped_duplicates: 0, skipped_conflicts: 0, skipped_unselected: 2 });
  }) as typeof fetch;
  try {
    const result = await confirmTerminologyImport(preview, ["candidate-1"]);
    assert.deepEqual(JSON.parse(body), {
      preview_id: "preview-1", file_sha256: "a".repeat(64), candidate_ids: ["candidate-1"],
    });
    assert.equal(result.created, 1);
  } finally { globalThis.fetch = originalFetch; }
});

test("shows the server explanation when a preview expired", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => Response.json(
    { detail: "Predogled uvoza ne obstaja ali je potekel. Datoteko ponovno preglejte." },
    { status: 409 },
  )) as typeof fetch;
  try {
    await assert.rejects(
      confirmTerminologyImport(preview, ["candidate-1"]),
      /ne obstaja ali je potekel/,
    );
  } finally { globalThis.fetch = originalFetch; }
});

test("rejects an oversized file before a network request", async () => {
  const originalFetch = globalThis.fetch;
  let called = false;
  globalThis.fetch = (async () => { called = true; return Response.json({}); }) as typeof fetch;
  try {
    const bytes = new Uint8Array(5 * 1024 * 1024 + 1);
    await assert.rejects(
      previewTerminologyImport(new File([bytes], "large.csv"), "csv", {
        source_language: "en", target_language: "sl", default_domain: "",
        default_match_mode: "preferred_term", external_source: "test",
        csv_mapping: { source_term: "source", target_term: "target" },
      }),
      /5 MB/,
    );
    assert.equal(called, false);
  } finally { globalThis.fetch = originalFetch; }
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
