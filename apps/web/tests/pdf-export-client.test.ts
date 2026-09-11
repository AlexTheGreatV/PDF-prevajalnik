import assert from "node:assert/strict";

import { downloadPdfExport, preparePdfExport } from "../lib/pdf-export-client";
import type { TranslationJob } from "../lib/translation-types";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

function job(): TranslationJob {
  return {
    schema_version: 4,
    source_filename: "source.pdf",
    source_language: "en",
    target_language: "sl",
    created_at: "2026-09-11T00:00:00Z",
    document_identity: { sha256: "a".repeat(64), page_count: 1, document_model_version: 1, pages: [{ page_number: 1, width: 100, height: 100, rotation: 0 }] },
    glossary: [],
    glossary_snapshot: [],
    segments: [],
  };
}

test("prepares a local multipart PDF export plan", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    assert.match(String(input), /^http:\/\/(?:127\.0\.0\.1|localhost):\d+\/api\/pdf\/export-translated\/plan$/);
    assert.equal(init?.method, "POST");
    const form = init?.body as FormData;
    assert.equal(form.get("mode"), "diagnostic");
    assert.equal(JSON.parse(String(form.get("translation_job"))).schema_version, 4);
    return new Response(JSON.stringify({ filename: "source-sl-preview.pdf", mode: "diagnostic", document_sha256: "a".repeat(64), summary: { total_segments: 0, reviewed: 0, eligible: 0, skipped: 0, overflow: 0, conflicts: 0 }, replacements: [] }), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    const result = await preparePdfExport(new File(["%PDF-"], "source.pdf", { type: "application/pdf" }), job(), "diagnostic");
    assert.equal(result.filename, "source-sl-preview.pdf");
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

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
