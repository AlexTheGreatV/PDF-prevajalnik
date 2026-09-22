import type { GlossaryEntry, GlossaryMatch } from "./translation-types";

export function orderedGlossary(entries: GlossaryEntry[]): GlossaryEntry[] {
  const priority = (entry: GlossaryEntry) => {
    if (entry.scope === "document") return 4;
    if (entry.scope === "persistent_user" && entry.source !== "import") return 3;
    if (entry.scope === "persistent_user" || entry.origin === "user") return 2;
    return 1;
  };
  return [...entries]
    .filter((entry) => entry.status === undefined || entry.status === "approved")
    .sort((a, b) =>
      priority(b) - priority(a) ||
      b.source_term.trim().length - a.source_term.trim().length ||
      a.id.localeCompare(b.id),
    );
}

export function findExactSegmentGlossaryMatch(text: string, glossary: GlossaryEntry[]): GlossaryEntry | null {
  const source = text.trim();
  return orderedGlossary(glossary).find((entry) =>
    entry.enabled &&
    entry.mode === "exact_segment" &&
    Boolean(entry.source_term.trim()) &&
    equals(source, entry.source_term.trim(), entry.case_sensitive),
  ) ?? null;
}

export function findGlossaryMatches(text: string, glossary: GlossaryEntry[]): GlossaryMatch[] {
  const matches: GlossaryMatch[] = [];
  for (const entry of orderedGlossary(glossary)) {
    if (!entry.enabled || !entry.source_term.trim()) continue;
    if (entry.mode === "exact_segment") {
      if (equals(text.trim(), entry.source_term.trim(), entry.case_sensitive)) matches.push(toMatch(entry, text.trim()));
      continue;
    }
    const flags = entry.case_sensitive ? "gu" : "giu";
    const escaped = escapeRegExp(entry.source_term.trim());
    const pattern = new RegExp(`(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])`, flags);
    for (const match of text.matchAll(pattern)) matches.push(toMatch(entry, match[0]));
  }
  return matches;
}

function toMatch(entry: GlossaryEntry, matched_text: string): GlossaryMatch {
  return {
    glossary_id: entry.id,
    source_term: entry.source_term,
    target_term: entry.target_term,
    matched_text,
    mode: entry.mode,
    origin: entry.origin,
    revision: entry.revision,
    scope: entry.scope,
    domain: entry.domain,
    external_source: entry.external_source,
    external_record_id: entry.external_record_id,
  };
}

function equals(left: string, right: string, caseSensitive: boolean): boolean {
  return caseSensitive ? left === right : left.toLocaleLowerCase() === right.toLocaleLowerCase();
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
