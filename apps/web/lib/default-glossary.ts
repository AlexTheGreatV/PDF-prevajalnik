import type { GlossaryEntry } from "./translation-types";

const TERMS: ReadonlyArray<readonly [string, string, string]> = [
  ["product-description", "PRODUCT DESCRIPTION", "OPIS IZDELKA"],
  ["applications", "APPLICATIONS", "PODROČJA UPORABE"],
  ["advantages", "ADVANTAGES", "PREDNOSTI"],
  ["limitations", "LIMITATIONS", "OMEJITVE"],
  ["additional-properties", "ADDITIONAL PROPERTIES", "DODATNE LASTNOSTI"],
  ["recommended-accessories", "RECOMMENDED ACCESSORIES", "PRIPOROČENI DODATKI"],
  ["safety-recommendations", "SAFETY RECOMMENDATIONS", "VARNOSTNA PRIPOROČILA"],
  ["sustainability", "SUSTAINABILITY", "TRAJNOSTNOST"],
  ["packaging", "PACKAGING", "PAKIRANJE"],
  ["storage", "STORAGE", "SKLADIŠČENJE"],
  ["shelf-life", "SHELF LIFE", "ROK UPORABNOSTI"],
];

export function createDefaultGlossary(): GlossaryEntry[] {
  return TERMS.map(([suffix, source_term, target_term]) => ({
    id: `builtin-${suffix}`,
    source_term,
    target_term,
    default_target_term: target_term,
    case_sensitive: false,
    mode: "exact_segment",
    enabled: true,
    origin: "built_in",
  }));
}
