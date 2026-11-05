import rules from "./si-measurement-rules.json";
import { formatSiMeasurement, formatSlovenianTemperatureRange } from "./si-measurements";
export const TECHNICAL_RANGE_RULES_VERSION = "sl-technical-ranges-v5";

export type TechnicalRangeKind = "temperature" | "measurement" | "percentage" | "time";
export type TechnicalRangeConnector = "to" | "from_to" | "between_and" | "hyphen";
export type TechnicalUnitKind = "immutable_symbol" | "translatable_word";

export type TechnicalRange = {
  start_value: string;
  end_value: string;
  unit: string;
  unit_kind: TechnicalUnitKind;
  unit_placement: "end_only" | "both";
  kind: TechnicalRangeKind;
  connector: TechnicalRangeConnector;
  source_text: string;
  target_text: string;
};

export type TechnicalRangeAnalysis = {
  ranges: Array<TechnicalRange & { start: number; end: number }>;
  warnings: string[];
};

const NUMBER_SOURCE = String.raw`[+\-−]?\d+(?:[.,]\d+)?`;
const IMMUTABLE_UNIT_SOURCE = rules.unit_pattern;
const WORD_UNIT_SOURCE = String.raw`(?:minutes?|hours?|days?|months?|years?)`;
const UNIT_SOURCE = String.raw`(?:${IMMUTABLE_UNIT_SOURCE}|${WORD_UNIT_SOURCE})`;
const RANGE_PATTERN = new RegExp(
  String.raw`(?<![\p{L}\p{N}])(?:(from|between)\s+)?(${NUMBER_SOURCE})\s*(${UNIT_SOURCE})?\s*(to|and|[-–—])\s*(${NUMBER_SOURCE})\s*(${UNIT_SOURCE})(?![\p{L}\p{N}])`,
  "gu",
);
const RANGE_CANDIDATE_PATTERN = new RegExp(
  String.raw`(?<![\p{L}\p{N}])(?:(?:from|between)\s+)?${NUMBER_SOURCE}\s*(?:${UNIT_SOURCE})?\s*(?:to|and|[-–—])\s*(?:${NUMBER_SOURCE}|\S+)\s*(?:${UNIT_SOURCE})?(?![\p{L}\p{N}])`,
  "giu",
);

export function analyzeTechnicalRanges(text: string): TechnicalRangeAnalysis {
  const ranges: TechnicalRangeAnalysis["ranges"] = [];
  const acceptedSpans: Array<{ start: number; end: number }> = [];
  const contacts = [...text.matchAll(new RegExp(rules.phone_pattern, "gu"))]
    .map(match => ({ start: match.index, end: match.index + match[0].length }));
  const overlapsContact = (start: number, end: number) => contacts.some(span => start < span.end && end > span.start);
  for (const match of text.matchAll(RANGE_PATTERN)) {
    const start = match.index;
    const end = start + match[0].length;
    if (overlapsContact(start, end)) continue;
    const parsed = parseMatch(match);
    if (!parsed) continue;
    ranges.push({ ...parsed, start, end });
    acceptedSpans.push({ start, end });
  }

  const ambiguous = [...text.matchAll(RANGE_CANDIDATE_PATTERN)].some((match) => {
    const start = match.index;
    const end = start + match[0].length;
    return !overlapsContact(start, end) && !acceptedSpans.some((span) => span.start <= start && span.end >= end);
  });
  return { ranges, warnings: ambiguous ? ["technical_range_ambiguous"] : [] };
}

function parseMatch(match: RegExpMatchArray): TechnicalRange | null {
  if ([match[2], match[5]].some(value => new RegExp(rules.ambiguous_number_pattern).test(value))) return null;
  const prefix = match[1]?.toLowerCase() ?? null;
  const connectorText = match[4].toLowerCase();
  if ((prefix === "from" && connectorText !== "to") || (prefix === "between" && connectorText !== "and")) return null;
  if (!prefix && connectorText === "and") return null;

  const startUnit = normalizeUnit(match[3] ?? "");
  const endUnit = normalizeUnit(match[6]);
  if (!endUnit || (startUnit && unitFamily(startUnit) !== unitFamily(endUnit))) return null;
  const unitKind = isWordUnit(endUnit) ? "translatable_word" : "immutable_symbol";
  const connector: TechnicalRangeConnector = prefix === "from"
    ? "from_to"
    : prefix === "between"
      ? "between_and"
      : connectorText === "to"
        ? "to"
        : "hyphen";
  const startValue = normalizeNumber(match[2]);
  const endValue = normalizeNumber(match[5]);
  const start = unitKind === "translatable_word" ? startValue : formatValue(startValue, startUnit);
  const end = unitKind === "translatable_word" ? endValue : formatValue(endValue, endUnit);
  const temperatureTo = ["°C", "°F", "K"].includes(endUnit) && ["to", "from_to"].includes(connector);
  const targetText = temperatureTo
    ? formatSlovenianTemperatureRange(startValue, endValue, endUnit)!
    : connector === "from_to"
    ? `od ${start} do ${end}`
    : connector === "between_and"
      ? `med ${start} in ${end}`
      : `${start} do ${end}`;
  return {
    start_value: startValue,
    end_value: endValue,
    unit: endUnit,
    unit_kind: unitKind,
    unit_placement: startUnit ? "both" : "end_only",
    kind: rangeKind(endUnit),
    connector,
    source_text: match[0],
    target_text: targetText,
  };
}

function normalizeNumber(value: string): string {
  return value.startsWith("-") ? `−${value.slice(1)}` : value;
}

function normalizeUnit(value: string): string {
  return value.replace(/\s+/g, "").replace(/^°c$/i, "°C");
}

function isWordUnit(unit: string): boolean {
  return new RegExp(`^(?:${WORD_UNIT_SOURCE})$`, "iu").test(unit);
}

function unitFamily(unit: string): string {
  const normalized = unit.toLowerCase();
  const wordFamilies: Record<string, string> = {
    minute: "minute", minutes: "minute",
    hour: "hour", hours: "hour",
    day: "day", days: "day",
    month: "month", months: "month",
    year: "year", years: "year",
  };
  return wordFamilies[normalized] ?? unit;
}

function formatValue(value: string, unit: string): string {
  return unit ? formatSiMeasurement(`${value} ${unit}`) ?? `${value}\u00a0${unit}` : value;
}

function rangeKind(unit: string): TechnicalRangeKind {
  if (["°C", "°F", "K"].includes(unit)) return "temperature";
  if (unit === "%") return "percentage";
  if (/^(?:minutes?|hours?|days?|months?|years?|min|h)$/i.test(unit)) return "time";
  return "measurement";
}
