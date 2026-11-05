import { protectTechnicalTokens, restoreTechnicalTokens } from "./technical-tokens";
import type { ProtectedToken, TranslationSegment } from "./translation-types";
import { canonicalSiNumber, formatSiMeasurement, reviewedUnitTarget, SI_RULES_VERSION, type ReviewedUnitContext } from "./si-measurements";

export const PROTECTED_DATA_RULES_VERSION = `protected-data-v2:${SI_RULES_VERSION}`;

export function createTranslationContentRevision(segment: Pick<TranslationSegment, "status" | "source_text" | "edited_translation" | "translated_text" | "source_text_origin" | "accepted_ocr_source">): string {
  return createTranslationContentRevisionFromValues(
    segment.source_text,
    ["pending", "failed", "translating"].includes(segment.status) && !segment.edited_translation
      ? "" : segment.edited_translation || segment.translated_text,
    segment.source_text_origin === "accepted_ocr" ? segment.accepted_ocr_source?.revision_id ?? null : null,
  );
}

export function createTranslationContentRevisionFromValues(sourceText: string, targetText: string, ocrRevision: string | null): string {
  return JSON.stringify({ source_text: sourceText, target_text: targetText, ocr_revision: ocrRevision });
}

export function validateTranslationTechnicalIntegrity(source: string, target: string, context?: ReviewedUnitContext): { valid: boolean; normalizedTarget: string; expected: string[]; detected: string[] } {
  const normalizedSource = source.replace(/−/g, "-");
  const normalizedInput = target.replace(/−/g, "-");
  const validPercentageSpacing = !hasInvalidPercentageSpacing(normalizedSource) && !hasInvalidPercentageSpacing(normalizedInput);
  const targetProtection = protectTechnicalTokens(normalizedInput);
  const sourceValues = integritySignatures(normalizedSource);
  const targetValues = integritySignatures(normalizedInput);
  const approvedTarget = reviewedUnitTarget(source, context);
  const spellingApproved = approvedTarget !== null && formatSiMeasurement(target.trim(), true) === formatSiMeasurement(approvedTarget, true);
  const valid = spellingApproved || validPercentageSpacing
    && !sourceValues.some((value) => value.startsWith("inconsistent-temperature-units:"))
    && !targetValues.some((value) => value.startsWith("inconsistent-temperature-units:"))
    && sourceValues.length === targetValues.length
    && sourceValues.every((value, index) => value === targetValues[index]);
  return {
    valid,
    expected: sourceValues,
    detected: targetValues,
    normalizedTarget: valid ? restoreTechnicalTokens(targetProtection.protected_text, targetProtection.tokens) : "",
  };
}

// Compare ordered temperature bounds, not the typography of a repeated/shared unit.
// Only explicit range syntax qualifies; isolated quantities retain strict token checks.
function integritySignatures(text: string): string[] {
  const number = "[+-]?\\d+(?:[.,]\\d+)?";
  const unit = "(?:°[ \\u00a0]?C|°[ \\u00a0]?F|K)";
  const space = "[ \\u00a0]*";
  const ranges = new RegExp(`(?<![\\p{L}\\p{N}.,+\\-])(?:from |od )?(${number})${space}(${unit})?${space}(?:to|do)${space}(${number})${space}(${unit})(?![\\p{L}\\p{N}])`, "gu");
  const values: string[] = [];
  let cursor = 0;
  for (const match of text.matchAll(ranges)) {
    values.push(...technicalSignatures(protectTechnicalTokens(text.slice(cursor, match.index)).tokens));
    const firstUnit = match[2]?.replace(/[ \u00a0]/g, "");
    const lastUnit = match[4].replace(/[ \u00a0]/g, "");
    if (firstUnit && firstUnit !== lastUnit) {
      values.push(`inconsistent-temperature-units:${match[0]}`);
    } else {
      const bound = (value: string) => normalizeTechnicalSignature(value).replace(/^\+/, "");
      values.push(`temperature-range:${bound(match[1])}:${bound(match[3])}:${lastUnit}`);
    }
    cursor = match.index + match[0].length;
  }
  values.push(...technicalSignatures(protectTechnicalTokens(text.slice(cursor)).tokens));
  return values;
}

function hasInvalidPercentageSpacing(value: string): boolean {
  return /[+-]?\d+(?:[.,]\d+)?(?:[ \u00a0]{2,}|[\t\r\n]+)%/u.test(value);
}

function technicalSignatures(tokens: ProtectedToken[]): string[] {
  return tokens.flatMap((token) => {
    if (token.kind === "technical_unit_boundary") return [];
    if (token.kind !== "technical_range" || !token.technical_range) {
      if (["product_code", "url", "email", "source_placeholder"].includes(token.kind)) return [token.value];
      const si = token.kind === "measurement" ? formatSiMeasurement(token.value, true) : null;
      return [si ?? normalizeTechnicalSignature(token.replacement_value ?? token.value)];
    }
    const range = token.technical_range;
    if (range.unit_kind === "translatable_word") {
      return [range.start_value, range.end_value].map(normalizeTechnicalSignature);
    }
    const start = range.unit_placement === "both" ? `${range.start_value} ${range.unit}` : range.start_value;
    return [start, `${range.end_value} ${range.unit}`].map(normalizeTechnicalSignature);
  });
}

function normalizeTechnicalSignature(value: string): string {
  const normalized = value.replace(/[+\-−]?\d+(?:[.,]\d+)?/g, canonicalSiNumber);
  const percentage = normalized.match(/^([+-]?\d+(?:,\d+)?)(?: |\u00a0)?%$/u);
  if (percentage) return `${percentage[1]}%`;
  return normalized.replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
}
