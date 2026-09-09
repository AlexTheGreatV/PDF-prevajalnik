import type { GlossaryEntry, GlossaryMatch } from "./translation-types";

export function findGlossaryMatches(text: string, glossary: GlossaryEntry[]): GlossaryMatch[] {
  const matches: GlossaryMatch[] = [];
  for (const entry of glossary) {
    if (!entry.source_term.trim()) continue;
    const flags = entry.case_sensitive ? "g" : "gi";
    const escaped = escapeRegExp(entry.source_term.trim());
    const pattern = new RegExp(entry.exact_match ? `(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])` : escaped, `${flags}u`);
    for (const match of text.matchAll(pattern)) {
      matches.push({
        glossary_id: entry.id,
        source_term: entry.source_term,
        target_term: entry.target_term,
        matched_text: match[0],
        do_not_translate: entry.do_not_translate,
      });
    }
  }
  return matches;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
