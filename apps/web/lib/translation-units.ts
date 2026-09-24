import type { TranslationSegment } from "./translation-types";

const INVISIBLE_TEXT = /^[\s\u00a0\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufeff\uffa0]*$/u;

export function hasVisibleTranslationText(value: string): boolean {
  return !INVISIBLE_TEXT.test(value);
}

export function isEmptyStructuralTableCell(segment: Pick<TranslationSegment, "block_type" | "source_text">): boolean {
  return segment.block_type === "table_cell" && !hasVisibleTranslationText(segment.source_text);
}

export function isTranslationUnitSegment(segment: Pick<TranslationSegment, "block_type" | "source_text">): boolean {
  return !isEmptyStructuralTableCell(segment);
}
