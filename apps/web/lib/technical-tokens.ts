import type { PlaceholderStrategy, ProtectedToken } from "./translation-types";

const TOKEN_PATTERN = /https?:\/\/[^\s]+|www\.[^\s]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\bRAL\s+\d{3,5}\b|[+-]?\d+(?:[.,]\d+)?\s*(?:°\s*C|MPa|kPa|Pa|ml|mL|L|kg|g|mm|cm|m|%)(?=$|\s|[),.;:])|(?<![\p{L}\p{N}])CO[₂2](?![\p{L}\p{N}])|\b[A-Z]{1,8}[-/]?(?=[A-Z0-9/-]*\d)[A-Z0-9/-]{1,20}\b|[+-]?\d+(?:[.,]\d+)?/gu;
export function protectTechnicalTokens(text: string, strategy: PlaceholderStrategy = "primary_tk"): { protected_text: string; tokens: ProtectedToken[] } {
  const tokens: ProtectedToken[] = [];
  const protected_text = text.replace(TOKEN_PATTERN, (value) => {
    const index = String(tokens.length).padStart(3, "0");
    const placeholder = strategy === "primary_tk" ? `TK${index}TK` : `PHX${index}XHP`;
    tokens.push({ placeholder, value, kind: tokenKind(value) });
    return placeholder;
  });
  return { protected_text, tokens };
}

export function restoreTechnicalTokens(text: string, tokens: ProtectedToken[]): string {
  for (const token of tokens) {
    const occurrences = text.split(token.placeholder).length - 1;
    if (occurrences === 0) throw new Error("Prevod vsebuje manjkajoč ali poškodovan tehnični placeholder.");
    if (occurrences !== 1) throw new Error("Prevod vsebuje podvojen tehnični placeholder.");
  }
  return tokens.reduce((result, token) => result.replace(token.placeholder, token.value), text);
}

function tokenKind(value: string): ProtectedToken["kind"] {
  if (/^https?:|^www\./i.test(value)) return "url";
  if (value.includes("@")) return "email";
  if (/\d\s*(?:°\s*C|MPa|kPa|Pa|ml|mL|L|kg|g|mm|cm|m|%)$/u.test(value)) return "measurement";
  if (/^[+-]?\d+(?:[.,]\d+)?$/.test(value)) return "number";
  if (/\d/.test(value)) return "product_code";
  return "technical_term";
}
