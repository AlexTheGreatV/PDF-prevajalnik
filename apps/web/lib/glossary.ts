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
      const trimmed = text.trim();
      if (equals(trimmed, entry.source_term.trim(), entry.case_sensitive)) {
        const start = text.indexOf(trimmed);
        matches.push(toMatch(entry, trimmed, start, start + trimmed.length));
      }
      continue;
    }
    const flags = entry.case_sensitive ? "gu" : "giu";
    const escaped = entry.mode === "preferred"
      ? preferredSourcePattern(entry.source_term.trim())
      : escapeRegExp(entry.source_term.trim());
    const pattern = new RegExp(`(?<![\\p{L}\\p{N}])${escaped}(?![\\p{L}\\p{N}])`, flags);
    for (const match of text.matchAll(pattern)) {
      const start = match.index!;
      matches.push(toMatch(entry, match[0], start, start + match[0].length));
    }
  }
  return matches;
}

function toMatch(entry: GlossaryEntry, matched_text: string, start_offset: number, end_offset: number): GlossaryMatch {
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
    start_offset,
    end_offset,
  };
}

function equals(left: string, right: string, caseSensitive: boolean): boolean {
  return caseSensitive ? left === right : left.toLocaleLowerCase() === right.toLocaleLowerCase();
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Finite orthographic alternatives, never stemming or substring matching.
// They only discover advisory preferred-term matches; they never rewrite a translation.
function preferredSourcePattern(term: string): string {
  return term.split(/([ -])/u).map(part => {
    if (part === " " || part === "-") return "[ -]";
    if (/^(?:fibre|fiber)$/u.test(part)) return "f(?:ibre|iber)";
    if (/^(?:Fibre|Fiber)$/u.test(part)) return "F(?:ibre|iber)";
    return escapeRegExp(part);
  }).join("");
}
