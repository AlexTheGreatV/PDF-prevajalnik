import { canonicalSiNumber, formatSiMeasurement, isMachineValue, SI_MEASUREMENT_PATTERN, OPAQUE_MEASUREMENT_PATTERN, SI_RULES_VERSION } from "./si-measurements";
import type { PlaceholderStrategy, ProtectedToken } from "./translation-types";
import { analyzeTechnicalRanges, TECHNICAL_RANGE_RULES_VERSION } from "./technical-ranges";

const TOKEN_PATTERN = new RegExp(String.raw`https?://[^\s]+|www\.[^\s]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\{\{[A-Za-z_][\w.-]*\}\}|\$\{[A-Za-z_][\w.-]*\}|\{[A-Za-z_][\w.-]*\}|%(?:s|d)|\[LINK\]|\bRAL\s+\d{3,5}\b|(?<![\p{L}\p{N}])[+-]\s*/\s*[A-Z]\s+\d+(?![\p{L}\p{N}])|(?<![\p{L}\p{N}])\+\d(?:[\d -]{5,}\d)(?![\p{L}\p{N}])|${SI_MEASUREMENT_PATTERN}|${OPAQUE_MEASUREMENT_PATTERN}|(?<![\p{L}\p{N}])CO[₂2](?![\p{L}\p{N}])|(?<!\p{L})(?=[A-Za-z0-9₂₃₄₅₆₇₈₉]*[₂₃₄₅₆₇₈₉])(?:[A-Z][a-z]?[0-9₂₃₄₅₆₇₈₉]*){2,}(?!\p{L})|\b[A-Z]{1,8}[-/]?(?=[A-Z0-9/-]*\d)[A-Z0-9/-]{1,20}\b|(?<![\p{L}\p{N}])\d+[A-Z][A-Z0-9/-]*(?![\p{L}\p{N}])|[+-]?\d+(?:[.,]\d+)?`, "gu");
export function protectTechnicalTokens(text: string, strategy: PlaceholderStrategy = "primary_tk"): { protected_text: string; tokens: ProtectedToken[]; warnings: string[] } {
  const tokens: ProtectedToken[] = [];
  const analysis = analyzeTechnicalRanges(text);
  let cursor = 0;
  let protected_text = "";
  for (const range of analysis.ranges) {
    protected_text += protectValues(text.slice(cursor, range.start), tokens, strategy);
    const technicalRange = {
      start_value: range.start_value,
      end_value: range.end_value,
      unit: range.unit,
      unit_kind: range.unit_kind,
      unit_placement: range.unit_placement,
      kind: range.kind,
      connector: range.connector,
      source_text: range.source_text,
      target_text: range.target_text,
    };
    const index = String(tokens.length).padStart(3, "0");
    const placeholder = strategy === "primary_tk" ? `TK${index}TK` : `PHX${index}XHP`;
    const unitStartPlaceholder = range.unit_kind === "translatable_word"
      ? placeholderFor(tokens.length + 1, strategy)
      : undefined;
    const unitEndPlaceholder = range.unit_kind === "translatable_word"
      ? placeholderFor(tokens.length + 2, strategy)
      : undefined;
    const profileReplacement = normalizeDecimalDots(range.target_text);
    tokens.push({
      placeholder,
      value: range.source_text,
      replacement_value: profileReplacement,
      kind: "technical_range",
      technical_range: technicalRange,
      rule_id: "technical_range",
      rule_version: TECHNICAL_RANGE_RULES_VERSION,
      profile_rule_ids: profileReplacement !== range.target_text ? ["SL-SSKJ-05-001"] : [],
      unit_start_placeholder: unitStartPlaceholder,
      unit_end_placeholder: unitEndPlaceholder,
    });
    if (unitStartPlaceholder && unitEndPlaceholder) {
      tokens.push(
        { placeholder: unitStartPlaceholder, value: "", replacement_value: "", kind: "technical_unit_boundary" },
        { placeholder: unitEndPlaceholder, value: "", replacement_value: "", kind: "technical_unit_boundary" },
      );
      protected_text += `${placeholder} ${unitStartPlaceholder} ${range.unit} ${unitEndPlaceholder}`;
    } else {
      protected_text += placeholder;
    }
    cursor = range.end;
  }
  protected_text += protectValues(text.slice(cursor), tokens, strategy);
  return { protected_text, tokens, warnings: analysis.warnings };
}

export function restoreTechnicalTokens(text: string, tokens: ProtectedToken[]): string {
  for (const token of tokens) {
    const occurrences = text.split(token.placeholder).length - 1;
    if (occurrences === 0) throw new Error("Prevod vsebuje manjkajoč ali poškodovan tehnični placeholder.");
    if (occurrences !== 1) throw new Error("Prevod vsebuje podvojen tehnični placeholder.");
  }
  return tokens.reduce((result, token) => restoreToken(result, token), text);
}

function restoreToken(text: string, token: ProtectedToken): string {
  const replacement = token.replacement_value ?? token.value;
  if (token.kind !== "technical_range" || token.technical_range?.unit_kind !== "translatable_word") {
    return text.replace(token.placeholder, replacement);
  }
  if (!token.unit_start_placeholder || !token.unit_end_placeholder) {
    throw new Error("Prevodljiva besedna enota nima veljavnih mejnih placeholderjev.");
  }
  const pattern = new RegExp(
    `\\s*${escapeRegExp(token.unit_start_placeholder)}\\s+([\\p{L}][\\p{L}\\p{M}-]*(?:\\s+[\\p{L}][\\p{L}\\p{M}-]*)*)\\s+${escapeRegExp(token.unit_end_placeholder)}`,
    "u",
  );
  const match = text.match(pattern);
  if (!match) throw new Error("Prevodljiva besedna enota tehničnega razpona manjka ali je poškodovana.");
  const translatedUnit = match[1].trim();
  if (sameEnglishUnit(translatedUnit, token.technical_range.unit)) {
    throw new Error("Prevodljiva besedna enota tehničnega razpona je ostala v izvornem jeziku.");
  }
  return text.replace(pattern, "").replace(token.placeholder, `${replacement}\u00a0${translatedUnit}`);
}

function sameEnglishUnit(translated: string, source: string): boolean {
  const families: Record<string, string> = {
    minute: "minute", minutes: "minute",
    hour: "hour", hours: "hour",
    day: "day", days: "day",
    month: "month", months: "month",
    year: "year", years: "year",
  };
  const sourceFamily = families[source.toLowerCase()];
  return sourceFamily !== undefined && families[translated.toLowerCase()] === sourceFamily;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function placeholderFor(index: number, strategy: PlaceholderStrategy): string {
  const value = String(index).padStart(3, "0");
  return strategy === "primary_tk" ? `TK${value}TK` : `PHX${value}XHP`;
}

function protectValues(text: string, tokens: ProtectedToken[], strategy: PlaceholderStrategy): string {
  // Digit-leading designations (e.g. coating systems) must not lose their letters.
  // Existing measurements/URLs win at the same offset; the code wins over a bare number.
  return text.replace(TOKEN_PATTERN, (value, ...captures: (string | number | undefined)[]) => {
    const offset = captures[captures.length - 2] as number;
    const index = String(tokens.length).padStart(3, "0");
    const placeholder = strategy === "primary_tk" ? `TK${index}TK` : `PHX${index}XHP`;
    const kind = tokenKind(value, offset, text);
    const decimalNormalization = kind === "number" && canonicalSiNumber(value) !== value;
    const replacement = kind === "measurement"
      ? typographicMeasurement(value)
      : decimalNormalization ? canonicalSiNumber(value) : value;
    tokens.push({
      placeholder,
      value,
      replacement_value: replacement,
      kind,
      ...(replacement !== value ? decimalNormalization
        ? { rule_id: "decimal_separator" as const, rule_version: "sl-general-sskj-v1" }
        : { rule_id: "technical_unit_spacing" as const, rule_version: SI_RULES_VERSION }
      : {}),
      profile_rule_ids: replacement !== value && /\d\.\d/.test(value) && /\d,\d/.test(replacement)
        ? ["SL-SSKJ-05-001"]
        : [],
    });
    return placeholder;
  });
}

function typographicMeasurement(value: string): string {
  const si = formatSiMeasurement(value);
  if (si !== null) return si;
  const normalized = normalizeDecimalDots(value.startsWith("-") ? `−${value.slice(1)}` : value);
  return normalized.replace(/(?<=\d)\s+(?=(?:°\s*C|MPa|kPa|Pa|ml|mL|L|kg|g|mm|cm|m|%))/u, "\u00a0");
}

function normalizeDecimalDots(value: string): string {
  return value.replace(/[+\-−]?\d+(?:[.,]\d+)?/g, part => {
    const normalized = canonicalSiNumber(part);
    return part.startsWith("−") ? `−${normalized.slice(1)}` : normalized;
  });
}

function tokenKind(value: string, offset: number, source: string): ProtectedToken["kind"] {
  if (/^https?:|^www\./i.test(value)) return "url";
  if (value.includes("@")) return "email";
  if (/^(?:\{\{|\$?\{|%(?:s|d)$|\[LINK\]$)/.test(value)) return "source_placeholder";
  if (isMachineValue(source, offset)) return "product_code";
  if (formatSiMeasurement(value) !== null) return "measurement";
  if (/\d(?:\s*(?:°\s*[CF]|MPa|kPa|Pa|ml|mL|L|kg|g|mm|cm|min|kW|W|h|m|%)|[ \u00a0]+K)$/u.test(value)) return "measurement";
  if (/^[+-]?\d+(?:[.,]\d+)?$/.test(value)) {
    const before = source[offset - 1] ?? "";
    const after = source[offset + value.length] ?? "";
    const prefix = source.slice(Math.max(0, offset - 24), offset);
    const beforeUnsafe = /[\p{L}\p{N}_\/-]/u.test(before) || (before === "." && /[\p{L}\p{N}]/u.test(source[offset - 2] ?? ""));
    const afterUnsafe = /[\p{L}\p{N}_\/-]/u.test(after) || (after === "." && /[\p{L}\p{N}]/u.test(source[offset + value.length + 1] ?? ""));
    const labeledMachineValue = /\b(?:version|ver|code|id|identifier|serial|s\/n|sn|ISO|EN|DIN)\s*$/i.test(prefix);
    return beforeUnsafe || afterUnsafe || labeledMachineValue ? "product_code" : "number";
  }
  if (/\d/.test(value)) return "product_code";
  return "technical_term";
}
