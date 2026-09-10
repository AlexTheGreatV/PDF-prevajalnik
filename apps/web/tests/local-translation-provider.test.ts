import assert from "node:assert/strict";
import {
  LocalTranslationProvider,
  getLocalTranslationStatus,
  localTranslationUrl,
} from "../lib/local-translation-provider";
import type { TranslationRequest } from "../lib/translation-types";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

function request(index: number): TranslationRequest {
  return {
    segment_id: `segment-${index}`,
    text: `Text ${index} with TK000TK`,
    context: { segment_id: `segment-${index}`, page_number: 1, block_type: "paragraph", glossary_matches: [] },
  };
}

test("rejects external translation API origins", () => {
  assert.throws(() => localTranslationUrl("/status", "https://example.com"), /samo z lokalnim/);
  assert.equal(
    localTranslationUrl("/status", "http://127.0.0.1:8000"),
    "http://127.0.0.1:8000/status",
  );
});

test("reports the active HPLT profile when the local model is unavailable", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input) => {
    assert.match(String(input), /^http:\/\/(?:127\.0\.0\.1|localhost):\d+\/api\/translation\/providers\/local\/status$/);
    return new Response(JSON.stringify({
      available: false,
      loaded: false,
      provider_id: "local-hplt-ctranslate2",
      model_id: "HPLT/translate-en-sl-v2.0-hplt",
      profile_id: "hplt-en-sl-v2",
      device: "cpu",
      compute_type: "int8",
      message: "Lokalni prevajalnik ni nameščen.",
      load_time_ms: null,
    }), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    const status = await getLocalTranslationStatus();
    assert.equal(status.available, false);
    assert.equal(status.provider_id, "local-hplt-ctranslate2");
    assert.equal(status.profile_id, "hplt-en-sl-v2");
    assert.doesNotMatch(status.provider_id, /test/i);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("batches locally, preserves order and reports progress", async () => {
  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  const batchSizes: number[] = [];
  globalThis.fetch = (async (input, init) => {
    urls.push(String(input));
    const payload = JSON.parse(String(init?.body)) as { source_language: string; target_language: string; segments: Array<{ segment_id: string; text: string }> };
    batchSizes.push(payload.segments.length);
    assert.equal(payload.source_language, "en");
    assert.equal(payload.target_language, "sl");
    return new Response(JSON.stringify({
      provider_id: "local-hplt-ctranslate2",
      model_id: "HPLT/translate-en-sl-v2.0-hplt",
      processing_time_ms: 1,
      results: payload.segments.map((segment) => ({
        segment_id: segment.segment_id,
        translated_text: `SL:${segment.text}`,
        confidence: null,
        error: null,
      })),
    }), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    const progress: Array<[number, number]> = [];
    const requests = Array.from({ length: 33 }, (_, index) => request(index));
    const results = await new LocalTranslationProvider().translateBatch(requests, {
      source_language: "auto",
      target_language: "sl",
      onProgress: (completed, total) => progress.push([completed, total]),
    });
    assert.deepEqual(batchSizes, [32, 1]);
    assert.deepEqual(progress, [[32, 33], [33, 33]]);
    assert.deepEqual(results.map((result) => result.segment_id), requests.map((item) => item.segment_id));
    assert.ok(results.every((result) => result.translated_text?.includes("TK000TK")));
    assert.ok(urls.every((url) => /^http:\/\/(?:127\.0\.0\.1|localhost):\d+\//.test(url)));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("cancels before issuing a translation request", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => { calls += 1; throw new Error("unexpected fetch"); }) as typeof fetch;
  const controller = new AbortController();
  controller.abort();
  try {
    await assert.rejects(
      new LocalTranslationProvider().translateBatch([request(1)], {
        source_language: "en",
        target_language: "sl",
        signal: controller.signal,
      }),
      /preklican/,
    );
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
