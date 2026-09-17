import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  createTranslationProfileSnapshot,
  resolveTranslationRuleConflict,
  TRANSLATION_PROFILE_CANONICAL_SHA256,
  TRANSLATION_PROFILE_RULES,
  TRANSLATION_PROFILE_SOURCE_SHA256,
  TRANSLATION_QUALITY_CHECK_RULE_IDS,
  TRANSLATION_RULE_PRIORITY,
} from "../lib/translation-profile";

const tests: Promise<void>[] = [];
function test(name: string, run: () => void | Promise<void>) {
  tests.push(Promise.resolve().then(run).then(() => console.log(`ok - ${name}`)));
}

test("canonical profile contains all 17 source sections and stable unique rule ids", () => {
  assert.equal(TRANSLATION_PROFILE_RULES.length, 121);
  assert.deepEqual([...new Set(TRANSLATION_PROFILE_RULES.map((rule) => rule.source_section))], Array.from({ length: 17 }, (_, index) => index + 1));
  assert.equal(new Set(TRANSLATION_PROFILE_RULES.map((rule) => rule.rule_id)).size, 121);
  assert.deepEqual(TRANSLATION_PROFILE_RULES.map((rule) => rule.source_order), Array.from({ length: 121 }, (_, index) => index + 1));
  assert.ok(TRANSLATION_PROFILE_RULES.every((rule) => rule.user_message && rule.execution_mode && rule.importance));
  assert.equal(TRANSLATION_PROFILE_RULES.some((rule) => String(rule.execution_mode) === "not_applicable"), false);
  assert.equal(TRANSLATION_QUALITY_CHECK_RULE_IDS.length, 11);
});

test("canonical Markdown hash and source provenance match the versioned profile", () => {
  const canonical = readFileSync(resolve(process.cwd(), "../../docs/translation-profiles/sl-general-sskj-v1.md"));
  const text = normalizeCanonicalText(canonical.toString("utf8"));
  assert.equal(createHash("sha256").update(text, "utf8").digest("hex"), TRANSLATION_PROFILE_CANONICAL_SHA256);
  const windowsCheckout = `\uFEFF${text.replace(/\n/g, "\r\n")}`;
  assert.equal(createHash("sha256").update(normalizeCanonicalText(windowsCheckout), "utf8").digest("hex"), TRANSLATION_PROFILE_CANONICAL_SHA256);
  assert.match(text, new RegExp(`source_sha256: ${TRANSLATION_PROFILE_SOURCE_SHA256}`));
  assert.equal((text.match(/^## \d+\./gm) ?? []).length, 17);
  assert.equal((text.match(/\*\*SL-SSKJ-\d{2}-\d{3}:\*\*/g) ?? []).length, 121);
  for (const rule of TRANSLATION_PROFILE_RULES) {
    assert.ok(text.includes(`**${rule.rule_id}:** ${rule.source_text}`), `Canonical rule mismatch: ${rule.rule_id}`);
  }
  assert.equal(TRANSLATION_PROFILE_SOURCE_SHA256, "f51b8252a623014406244a07b2f263f7c2d16d5117b95903aad8094115659adc");
});

test("section 15 priority is exact and style cannot override technical accuracy", () => {
  assert.deepEqual(TRANSLATION_RULE_PRIORITY.map((item) => item.id), [
    "user_text_instructions",
    "user_glossary",
    "official_terminology",
    "professional_correctness",
    "meaning_and_logic",
    "language_norm",
    "style",
  ]);
  const conflict = resolveTranslationRuleConflict("style", "professional_correctness");
  assert.equal(conflict.winner, "professional_correctness");
  assert.match(conflict.reason, /Slog ne sme preglasiti/);
});

test("profile never invents external terminology verification", () => {
  const snapshot = createTranslationProfileSnapshot();
  assert.equal(snapshot.terminology_sources.length, 0);
  assert.ok(snapshot.diagnostics.some((diagnostic) => diagnostic.kind === "external_source_unchecked" && diagnostic.message === "Terminološki vir ni preverjen"));
  assert.deepEqual(snapshot.rule_evaluations, []);
  assert.deepEqual(snapshot.unverifiable_rule_ids, []);
});

function normalizeCanonicalText(value: string): string {
  return value.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
}

void Promise.all(tests).catch((error) => { console.error(error); process.exitCode = 1; });
