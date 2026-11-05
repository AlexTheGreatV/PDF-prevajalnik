import rules from "./si-measurement-rules.json";

export const SI_RULES_VERSION = rules.version;
export const INTERNATIONAL_PHONE_PATTERN = rules.phone_pattern;
export type ReviewedUnitCorrection = {
  version: 1; profile: string; document_sha256: string; segment_id: string; source_text: string;
  page_number: number; source_bbox: { x0: number; y0: number; x1: number; y1: number }; source_kind: string;
  target_unit: string; approval_note: string; recorded_at: string; evidence_sha256: string;
};
export type ReviewedUnitContext = { document_sha256: string; segment_id: string; page_number?: number; source_bbox?: { x0: number; y0: number; x1: number; y1: number } | null; source_kind?: string; record?: ReviewedUnitCorrection | null };

export function reviewedUnitTarget(source: string, context?: ReviewedUnitContext): string | null {
  const row = context?.record;
  if (!row || row.version !== 1 || row.document_sha256 !== context?.document_sha256
    || row.page_number !== context.page_number || row.source_kind !== context.source_kind
    || !context.source_bbox || (["x0", "y0", "x1", "y1"] as const).some(k => row.source_bbox[k] !== context.source_bbox![k])
    || row.segment_id !== context.segment_id || row.source_text !== source.trim()
    || typeof row.evidence_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(row.evidence_sha256)) return null;
  const match = source.trim().match(new RegExp(`^${SI_MEASUREMENT_PATTERN}$`, "u"));
  if (!match || !new RegExp(`^${rules.review_unit_pattern}$`).test(match[2])) return null;
  const target = `${canonicalSiNumber(match[1])}\u00a0${row.target_unit}`;
  const parsed = target.match(new RegExp(`^${SI_MEASUREMENT_PATTERN}$`, "u"));
  return parsed && !new RegExp(`^${rules.review_unit_pattern}$`).test(parsed[2]) ? target : null;
}
export function isMachineValue(text: string, offset: number): boolean {
  return new RegExp(rules.machine_label_pattern, "i").test(text.slice(Math.max(0, offset - 24), offset))
    || (text[offset - 1] === "/" && /[A-Za-z0-9_]/.test(text[offset - 2] ?? ""));
}
export const SI_MEASUREMENT_PATTERN = `(?<![\\p{L}\\p{N}_.,])(${rules.number_pattern})[ \\u00a0]*((?:${rules.unit_pattern}|${rules.review_unit_pattern}))(?![\\p{L}\\p{N}_/^⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹·])`;
export const OPAQUE_MEASUREMENT_PATTERN = `(?<![\\p{L}\\p{N}_.,])${rules.number_pattern}[ \\u00a0]*(?:${rules.suspicious_unit_pattern}|${rules.opaque_unit_pattern})(?![\\p{L}\\p{N}_/^⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹·])`;
const protectedText = /https?:\/\/[^\s]+|www\.[^\s]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/gu;

export function canonicalSiNumber(value: string): string {
  const signed = value.replace(/−/g, "-");
  return new RegExp(rules.ambiguous_number_pattern).test(signed) ? signed : signed.replace(".", ",");
}

export function formatSiMeasurement(value: string, signature = false): string | null {
  const match = value.match(new RegExp(`^${SI_MEASUREMENT_PATTERN}$`, "u"));
  if (!match) return null;
  const canonicalNumber = canonicalSiNumber(match[1]);
  const number = !signature && canonicalNumber.startsWith("-") ? `−${canonicalNumber.slice(1)}` : canonicalNumber;
  if (new RegExp(`^${rules.review_unit_pattern}$`).test(match[2])) return signature ? `${number} ${match[2]}` : value;
  if (new RegExp(`^${rules.humidity_unit_pattern}$`, "u").test(match[2])) {
    return `${number}${signature ? " " : rules.value_unit_separator}${signature ? "% r. v." : rules.humidity_unit_output}`;
  }
  const rawUnit = (rules.accepted_unit_aliases as Record<string, string>)[match[2]] ?? match[2].replace(/[ \u00a0]/g, "");
  const superscript: Record<string, string> = rules.superscript;
  const raised = Object.fromEntries(Object.entries(superscript).map(([key, value]) => [value, key]));
  const unit = signature ? rawUnit.replace(/[⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+/g, value => `^${[...value].map(c => superscript[c]).join("")}`)
    : rawUnit.replace(/\^([+\-]?[0-9]+)/g, (_, value: string) => [...value].map(c => raised[c]).join(""));
  return `${number}${["°", "′", "″"].includes(unit) ? "" : signature ? " " : rules.value_unit_separator}${unit}`;
}

export function formatSlovenianTemperatureRange(start: string, end: string, unit: string): string | null {
  if (!["°C", "°F", "K"].includes(unit) || [start,end].some(value =>
    !new RegExp(`^${rules.number_pattern}$`, "u").test(value) || new RegExp(rules.ambiguous_number_pattern).test(value))) return null;
  const bound = canonicalSiNumber(start.replace(/^\+/, "")).replace(/^-/, "−");
  const final = formatSiMeasurement(`${end.replace(/^\+/, "")} ${unit}`);
  return final ? `od ${bound} do ${final}` : null;
}

export function proposeSiMeasurements(text: string) {
  const protectedSpans = [...text.matchAll(protectedText)].map(m => [m.index, m.index + m[0].length]);
  const changes: { rule_id: string; before: string; after: string; start: number; end: number }[] = [];
  const warnings: string[] = [];
  const number = rules.number_pattern;
  const unit = "(?:°[ \\u00a0]?[CF]|(?<=[ \\u00a0])K)";
  const temperatureRanges = new RegExp(`(?<![\\p{L}\\p{N}_.,])(?:(?:od|from)[ \\u00a0]+)?(${number})[ \\u00a0]*(${unit})?[ \\u00a0]*(?:do|to)[ \\u00a0]*(${number})[ \\u00a0]*(${unit})(?![\\p{L}\\p{N}_/^⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹·])`, "gu");
  for (const m of text.matchAll(temperatureRanges)) {
    if (protectedSpans.some(([start,end]) => start <= m.index && m.index < end)) continue;
    if (isMachineValue(text,m.index)) { protectedSpans.push([m.index,m.index+m[0].length]); continue; }
    const first = m[2]?.replace(/[ \u00a0]/g, "");
    const last = m[4].replace(/[ \u00a0]/g, "");
    if (first && first !== last) continue;
    const after = formatSlovenianTemperatureRange(m[1],m[3],last);
    if (after === null) continue;
    protectedSpans.push([m.index,m.index+m[0].length]);
    if (after !== m[0]) changes.push({rule_id:SI_RULES_VERSION,before:m[0],after,start:m.index,end:m.index+m[0].length});
  }
  for (const m of text.matchAll(new RegExp(SI_MEASUREMENT_PATTERN, "gu"))) {
    if (isMachineValue(text, m.index)) continue;
    if (protectedSpans.some(([start, end]) => start <= m.index && m.index < end)) continue;
    if (new RegExp(rules.ambiguous_number_pattern).test(m[1])) {
      warnings.push(`ambiguous_measurement_number:${m[0]}`); continue;
    }
    const after = formatSiMeasurement(m[0])!;
    if (after !== m[0]) changes.push({ rule_id: SI_RULES_VERSION, before: m[0], after, start: m.index, end: m.index + m[0].length });
  }
  for (const m of text.matchAll(new RegExp(`(?<![A-Za-z0-9_.,])${rules.number_pattern}[ \\u00a0]*${rules.review_unit_pattern}(?![A-Za-z0-9_/])`, "gu"))) {
    if (isMachineValue(text, m.index) || protectedSpans.some(([start, end]) => start <= m.index && m.index < end)) continue;
    warnings.push(`source_unit_review_required:${m[0]}`);
  }
  for (const m of text.matchAll(new RegExp(OPAQUE_MEASUREMENT_PATTERN, "gu"))) {
    if (formatSiMeasurement(m[0]) === null && !isMachineValue(text, m.index)
        && !protectedSpans.some(([start, end]) => start <= m.index && m.index < end)) {
      warnings.push(`uninterpreted_compound_unit:${m[0]}`);
    }
  }
  return { changes, warnings };
}
