import { protectTechnicalTokens, restoreTechnicalTokens } from "./technical-tokens";
import type { ProtectedToken, TranslationSegment } from "./translation-types";

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

export function validateTranslationTechnicalIntegrity(source: string, target: string): { valid: boolean; normalizedTarget: string } {
  const normalizedSource = source.replace(/−/g, "-");
  const normalizedInput = target.replace(/−/g, "-");
  const validPercentageSpacing = !hasInvalidPercentageSpacing(normalizedSource) && !hasInvalidPercentageSpacing(normalizedInput);
  const sourceProtection = protectTechnicalTokens(normalizedSource);
  const targetProtection = protectTechnicalTokens(normalizedInput);
  const sourceValues = technicalSignatures(sourceProtection.tokens);
  const targetValues = technicalSignatures(targetProtection.tokens);
  const valid = validPercentageSpacing
    && sourceValues.length === targetValues.length
    && sourceValues.every((value, index) => value === targetValues[index]);
  return {
    valid,
    normalizedTarget: valid ? restoreTechnicalTokens(targetProtection.protected_text, targetProtection.tokens) : "",
  };
}

function hasInvalidPercentageSpacing(value: string): boolean {
  return /[+-]?\d+(?:[.,]\d+)?(?:[ \u00a0]{2,}|[\t\r\n]+)%/u.test(value);
}

function technicalSignatures(tokens: ProtectedToken[]): string[] {
  return tokens.flatMap((token) => {
    if (token.kind === "technical_unit_boundary") return [];
    if (token.kind !== "technical_range" || !token.technical_range) {
      return [normalizeTechnicalSignature(token.replacement_value ?? token.value)];
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
  const normalized = value.replace(/−/g, "-").replace(/(?<=\d)\.(?=\d)/g, ",");
  const percentage = normalized.match(/^([+-]?\d+(?:,\d+)?)(?: |\u00a0)?%$/u);
  if (percentage) return `${percentage[1]}%`;
  return normalized.replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
}
