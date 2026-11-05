import {
  createTranslationProfileSnapshot,
  createTranslationQualityChecklist,
  resolveTranslationRuleConflict,
  TRANSLATION_PROFILE_CANONICAL_SHA256,
  TRANSLATION_PROFILE_ID,
  TRANSLATION_PROFILE_RULES,
  TRANSLATION_PROFILE_SOURCE_SHA256,
  TRANSLATION_PROFILE_VERSION,
  type TranslationProfileDiagnostic,
  type TranslationProfileRule,
  type TranslationQualityCheck,
  type TranslationRuleEvaluation,
  type TerminologySourceRecord,
} from "./translation-profile";
import type { GlossaryEntry, TranslationJob, TranslationSegment } from "./translation-types";
import { findGlossaryMatches } from "./glossary";
import { protectTechnicalTokens } from "./technical-tokens";
import { TECHNICAL_RANGE_RULES_VERSION } from "./technical-ranges";
import { proposeSiMeasurements, reviewedUnitTarget, type ReviewedUnitContext } from "./si-measurements";
import { createTranslationContentRevision, validateTranslationTechnicalIntegrity, PROTECTED_DATA_RULES_VERSION } from "./translation-integrity";

export const CURRENT_QC_RULES_REVISION = `current-segment-qc-v2:${PROTECTED_DATA_RULES_VERSION}:${TRANSLATION_PROFILE_ID}:${TRANSLATION_PROFILE_VERSION}:${TRANSLATION_PROFILE_CANONICAL_SHA256}:${TECHNICAL_RANGE_RULES_VERSION}`;

const RULE = {
  userGlossary: "SL-SSKJ-03-002",
  glossaryConsistency: "SL-SSKJ-03-003",
  decimalSeparator: "SL-SSKJ-05-001",
  unitSpacing: "SL-SSKJ-05-004",
  preserveValues: "SL-SSKJ-05-005",
  preservePrecision: "SL-SSKJ-05-006",
  preserveSymbols: "SL-SSKJ-10-004",
  preserveMachineReadable: "SL-SSKJ-13-005",
  preservePlaceholders: "SL-SSKJ-13-004",
  terminologyConsistency: "SL-SSKJ-14-001",
} as const;

export function refreshTranslationProfileMetadata(job: TranslationJob): TranslationJob {
  const profileRevision = job.translation_profile ? JSON.stringify([
    job.translation_profile.profile_id, job.translation_profile.profile_version,
    job.translation_profile.canonical_sha256, job.translation_profile.source_sha256,
  ]) : "legacy-profile-unspecified";
  const currentJob = { ...job, segments: job.segments.map((segment) => refreshCurrentSegmentQc({
    ...segment,
    glossary_matches: findGlossaryMatches(segment.source_text, job.glossary),
  }, `${profileRevision}:${job.reviewed_unit_correction?.evidence_sha256 ?? "no-unit-correction"}`, {
    document_sha256: job.document_identity?.sha256 ?? "", segment_id: segment.segment_id, page_number: segment.page_number, source_bbox: segment.source_bbox, source_kind: segment.source_text_origin === "native" ? "native" : "accepted_ocr", record: job.reviewed_unit_correction,
  })) };
  if (!currentJob.translation_profile) return { ...currentJob, quality_checklist: currentJob.quality_checklist ?? [] };
  const profile = currentJob.translation_profile;
  if (profile.profile_id !== TRANSLATION_PROFILE_ID || profile.profile_version !== TRANSLATION_PROFILE_VERSION) {
    return { ...currentJob, translation_profile: { ...profile, evaluation_status: "unknown_profile" } };
  }
  if (profile.source_sha256 !== TRANSLATION_PROFILE_SOURCE_SHA256 || profile.canonical_sha256 !== TRANSLATION_PROFILE_CANONICAL_SHA256) {
    return invalidateChangedProfile(currentJob);
  }
  const applied = new Set<string>();
  for (const segment of currentJob.segments) collectAppliedRules(segment, applied);
  const qualityChecklist = evaluateTranslationQuality(currentJob.segments);
  const evaluations = evaluateTranslationRules(currentJob.segments, currentJob.glossary, applied, qualityChecklist);
  return {
    ...currentJob,
    translation_profile: {
      ...profile,
      evaluation_status: "current",
      applied_rule_ids: [...applied].sort(),
      manual_review_rule_ids: evaluations.filter((item) => item.status === "needs_review").map((item) => item.rule_id),
      unverifiable_rule_ids: evaluations.filter((item) => item.status === "not_checkable").map((item) => item.rule_id),
      rule_evaluations: evaluations,
      diagnostics: profileDiagnostics(currentJob.glossary, currentJob.segments, profile.terminology_sources),
      conflicts: glossaryConflicts(currentJob.glossary),
    },
    quality_checklist: qualityChecklist,
  };
}

export function refreshCurrentSegmentQc(segment: TranslationSegment, profileRevision = "segment-check", context?: ReviewedUnitContext): TranslationSegment {
  const target = ["pending", "failed", "translating"].includes(segment.status) && !segment.edited_translation
    ? "" : segment.edited_translation || segment.translated_text;
  const contentRevision = createTranslationContentRevision(segment);
  const sourceProtection = protectTechnicalTokens(segment.source_text);
  const rulesRevision = JSON.stringify({
    engine: CURRENT_QC_RULES_REVISION, profile: profileRevision,
    languages: [segment.source_language, segment.target_language],
    glossary_matches: segment.glossary_matches,
  });
  const sourceFindings: NonNullable<TranslationSegment["current_qc"]>["findings"] = sourceProtection.warnings.includes("technical_range_ambiguous") ? [{
    code: "technical_range_ambiguous",
    severity: "warning",
    message: "Izvor vsebuje dvoumen tehnični razpon, ki zahteva ročni pregled.",
  }] : [];
  for (const warning of proposeSiMeasurements(segment.source_text).warnings) sourceFindings.push({
    code: "technical_integrity", severity: "warning",
    message: reviewedUnitTarget(segment.source_text, context)
      ? `Izvorna enota ima dokumentno odobreno popravo po dokazilu ${context?.record?.evidence_sha256}. Izvor ostaja nespremenjen.`
      : `Izvorni merski zapis zahteva sledljiv pregled; ni bil samodejno popravljen: ${warning}`,
  });
  if (segment.skip_reason || !target.trim()) return {
    ...segment,
    current_qc: { content_revision: contentRevision, rules_revision: rulesRevision, status: "not_checked", findings: segment.skip_reason ? [] : sourceFindings },
  };

  const findings: NonNullable<TranslationSegment["current_qc"]>["findings"] = [...sourceFindings];
  let technicalValid = false;
  let technicalDifference = "";
  try {
    const checked = validateTranslationTechnicalIntegrity(segment.source_text, target, context);
    technicalValid = checked.valid;
    technicalDifference = ` Pričakovano: ${JSON.stringify(checked.expected)}. Zaznano: ${JSON.stringify(checked.detected)}.`;
  }
  catch { /* A malformed unit or range must leave the edited text intact and fail QC. */ }
  if (!technicalValid) findings.push({
    code: "technical_integrity",
    severity: "blocking",
    message: `Trenutni prevod ne ohranja vseh zaščitenih števil, enot, kod ali tehničnih zapisov.${technicalDifference}`,
  });

  const missingPlaceholder = sourceProtection.tokens.some((token) =>
    token.kind === "source_placeholder" && !target.includes(token.replacement_value ?? token.value),
  );
  if (missingPlaceholder) findings.push({
    code: "placeholder_integrity",
    severity: "blocking",
    message: "Trenutni prevod ne ohranja izvornega placeholderja.",
  });

  // Match order already follows glossary priority; overlapping inline protection uses the first owner.
  const owned: Array<{ start: number; end: number }> = [];
  for (const match of segment.glossary_matches.filter((item) => ["do_not_translate", "locked_inline"].includes(item.mode))) {
    const start = match.start_offset ?? -1;
    const end = match.end_offset ?? -1;
    if (start < 0 || end <= start || owned.some((range) => start < range.end && end > range.start)) continue;
    owned.push({ start, end });
    const expected = match.mode === "do_not_translate" ? match.matched_text : match.target_term;
    if (expected && !target.includes(expected)) findings.push({
      code: "protected_glossary", severity: "blocking", glossary_entry_id: match.glossary_id,
      message: `Trenutni prevod ne ohranja zaščitenega izraza »${expected}«.`,
    });
  }

  for (const match of segment.glossary_matches.filter((item) =>
    item.mode === "preferred" && item.target_term.trim() && !containsTerm(target, item.target_term),
  )) findings.push({
    code: "preferred_term_missing",
    severity: "warning",
    message: `Trenutni prevod ne vsebuje priporočenega izraza »${match.target_term}«.`,
    glossary_entry_id: match.glossary_id,
  });

  return {
    ...segment,
    current_qc: {
      content_revision: contentRevision,
      rules_revision: rulesRevision,
      status: findings.some((finding) => finding.severity === "blocking") ? "blocked" : findings.length ? "warnings" : "passed",
      findings,
    },
  };
}

function invalidateChangedProfile(job: TranslationJob): TranslationJob {
  const checklist = createTranslationQualityChecklist().map((check) => ({
    ...check,
    status: "not_checkable" as const,
    message: "Profil se je spremenil; prejšnji rezultat preverjanja ni več veljaven.",
  }));
  return {
    ...job,
    translation_profile: {
      ...job.translation_profile!,
      evaluation_status: "stale",
      applied_rule_ids: [],
      manual_review_rule_ids: [],
      unverifiable_rule_ids: [],
      rule_evaluations: [],
      diagnostics: [createDiagnostic("warning", null, "Profil se je spremenil; pravila je treba ponovno ovrednotiti.")],
    },
    quality_checklist: checklist,
  };
}

export function updateTerminologySources(job: TranslationJob, sources: TerminologySourceRecord[]): TranslationJob {
  if (!job.translation_profile) return job;
  return refreshTranslationProfileMetadata({
    ...job,
    translation_profile: {
      ...job.translation_profile,
      terminology_sources: sources.map((source) => ({ ...source })),
    },
  });
}

export function createActiveTranslationProfileMetadata(): Pick<TranslationJob, "translation_profile" | "quality_checklist"> {
  return {
    translation_profile: createTranslationProfileSnapshot(),
    quality_checklist: createTranslationQualityChecklist(),
  };
}

export function evaluateTranslationQuality(segments: TranslationSegment[]): TranslationQualityCheck[] {
  const checklist = createTranslationQualityChecklist();
  const translated = segments.filter((segment) => !segment.skip_reason && ["translated", "reviewed"].includes(segment.status));
  const technicalTokens = translated.flatMap((segment) => segment.protected_tokens.map((token) => ({ segment, token })));
  const technicalCheck = checklist.find((item) => item.source_rule_id === "SL-SSKJ-16-006");
  if (technicalCheck && technicalTokens.length > 0) {
    const failures = translated.filter((segment) => segment.current_qc?.findings.some((finding) => finding.code === "technical_integrity"));
    technicalCheck.status = failures.length ? "failed" : "needs_review";
    technicalCheck.message = failures.length
      ? `Tehnični zapis ni ohranjen v ${failures.length} primeru oziroma primerih.`
      : "Prepoznani zaščiteni tehnični zapisi so ohranjeni; celotna točka še zahteva ročni pregled neprepoznanih zapisov.";
  } else if (technicalCheck) {
    technicalCheck.status = "not_applicable";
    technicalCheck.message = "V prevedenih segmentih ni bilo prepoznanih zaščitenih tehničnih zapisov.";
  }

  const structureCheck = checklist.find((item) => item.source_rule_id === "SL-SSKJ-16-009");
  if (structureCheck) {
    const hasApplicableStructure = segments.some((segment) => hasSourcePlaceholder(segment) || hasTechnicalMarkup(segment.source_text));
    const placeholderFailure = segments.some((segment) => segment.current_qc?.findings.some((finding) => finding.code === "placeholder_integrity"));
    if (placeholderFailure) {
      structureCheck.status = "failed";
      structureCheck.message = "Vsaj en tehnični placeholder je manjkal ali je bil poškodovan.";
    } else if (hasApplicableStructure) {
      structureCheck.status = "needs_review";
      structureCheck.message = "Prepoznani placeholderji so ohranjeni; celotno tehnično strukturo mora potrditi uporabnik.";
    } else {
      structureCheck.status = "not_applicable";
      structureCheck.message = "V dokumentu ni bilo zaznanih HTML, XML, Markdown ali nadomestnih oznak.";
    }
  }
  return checklist;
}

export function evaluateTranslationRules(
  segments: TranslationSegment[],
  glossary: GlossaryEntry[],
  applied: ReadonlySet<string> = new Set(),
  checklist: TranslationQualityCheck[] = evaluateTranslationQuality(segments),
): TranslationRuleEvaluation[] {
  const context = createRuleContext(segments, glossary);
  return TRANSLATION_PROFILE_RULES.map((rule) => {
    const quality = checklist.find((item) => item.source_rule_id === rule.rule_id);
    if (quality) return {
      rule_id: rule.rule_id,
      scope: "document",
      applicability: quality.status === "not_applicable" ? "not_applicable" : "applicable",
      status: quality.status,
      segment_ids: [],
      message: quality.message,
    };

    const trigger = ruleTrigger(rule, context, applied);
    if (trigger.applicability !== "applicable") return {
      rule_id: rule.rule_id,
      scope: trigger.scope,
      applicability: trigger.applicability,
      status: trigger.applicability === "not_applicable" ? "not_applicable" : "not_checkable",
      segment_ids: trigger.segment_ids,
      message: trigger.message,
    };

    const failed = ruleFailed(rule.rule_id, trigger.segment_ids, segments);
    const status = failed
      ? "failed"
      : applied.has(rule.rule_id)
        ? "passed"
        : rule.execution_mode === "enforce"
          ? "not_checkable"
          : "needs_review";
    return {
      rule_id: rule.rule_id,
      scope: trigger.scope,
      applicability: "applicable",
      status,
      segment_ids: trigger.segment_ids,
      message: failed
        ? "Samodejno preverjanje je zaznalo kršitev pravila."
        : status === "passed"
          ? "Pravilo je bilo dejansko uporabljeno in preverjeno z omejenim avtomatskim mehanizmom."
          : status === "needs_review"
            ? "Obstaja konkreten sprožilec; rezultat zahteva ročni pregled."
            : "Pravilo je uporabno, vendar ga ni mogoče zanesljivo samodejno preveriti.",
    };
  });
}

type RuleContext = ReturnType<typeof createRuleContext>;

function createRuleContext(segments: TranslationSegment[], glossary: GlossaryEntry[]) {
  const active = segments.filter((segment) => !segment.skip_reason);
  const ids = (items: TranslationSegment[]) => items.map((segment) => segment.segment_id);
  const legal = active.filter((segment) => /\b(?:regulation|directive|statute|article|legal|law|shall comply|uredba|zakon|člen|odstavek)\b/i.test(segment.source_text));
  const markup = active.filter((segment) => hasTechnicalMarkup(segment.source_text));
  const placeholders = active.filter(hasSourcePlaceholder);
  const machineReadable = active.filter((segment) => segment.protected_tokens.some((token) => ["url", "email", "product_code"].includes(token.kind)));
  const technical = active.filter((segment) => segment.block_type === "table_cell" || segment.protected_tokens.some((token) => ["measurement", "technical_range", "technical_unit_boundary", "technical_term", "product_code"].includes(token.kind)));
  const numeric = active.filter((segment) => /\d/.test(segment.source_text));
  const properNames = active.filter((segment) => /\b[A-Z][a-z]+(?:\s+(?:[A-Z][A-Za-z0-9-]+|Europe|Panel|Bond))+\b/.test(segment.source_text));
  const abbreviations = active.filter((segment) => /\b[A-ZČŠŽ]{2,6}\b|[%°]/u.test(segment.source_text));
  const suspiciousSource = active.filter((segment) => /\b[a-z][A-Z]{3,}\b/.test(segment.source_text));
  const terminology = active.filter((segment) => segment.glossary_matches.length > 0 || technical.includes(segment));
  const ambiguousRanges = active.filter((segment) => segment.current_qc?.findings.some((finding) => finding.code === "technical_range_ambiguous"));
  return {
    active, legal, markup, placeholders, machineReadable, technical, numeric, properNames, abbreviations,
    suspiciousSource, terminology, ambiguousRanges, ids,
    inconsistentGlossary: hasInconsistentGlossary(glossary),
    isTechnicalDocument: technical.length > 0 || active.some((segment) => segment.block_type === "table_cell"),
  };
}

function ruleTrigger(rule: TranslationProfileRule, context: RuleContext, applied: ReadonlySet<string>) {
  const segment = (items: TranslationSegment[], message: string) => ({ scope: "segment" as const, applicability: items.length ? "applicable" as const : "not_applicable" as const, segment_ids: context.ids(items), message });
  const document = (applicability: "applicable" | "not_applicable" | "unknown", message: string) => ({ scope: "document" as const, applicability, segment_ids: [] as string[], message });
  if (applied.has(rule.rule_id)) return segment(context.active.filter((item) => segmentUsesRule(item, rule.rule_id)), "Pravilo je sprožil dejanski avtomatski poseg.");
  if (rule.source_section === 9) return segment(context.legal, "Pravni ali upravni kontekst ni bil zaznan.");
  if (rule.source_section === 11) return context.isTechnicalDocument
    ? document("not_applicable", "Dokument je tehnični; literarni ali oglaševalski kontekst ni zaznan.")
    : document("unknown", "Besedilna vrsta ni določena dovolj zanesljivo.");
  if (rule.source_section === 13) {
    if (rule.rule_id === RULE.preservePlaceholders) return segment(context.placeholders, "V izvirniku ni bilo zaznanih placeholderjev.");
    if (rule.rule_id === RULE.preserveMachineReadable) return segment(context.machineReadable, "V izvirniku ni bilo zaznanih strojno berljivih elementov.");
    return segment(context.markup, "HTML, XML, Markdown ali programska struktura ni bila zaznana.");
  }
  if (rule.source_section === 8) return segment(context.properNames, "Kandidat za lastno ali uradno ime ni bil zaznan.");
  if (rule.source_section === 10) return segment(context.technical, "Tehnična ali znanstvena vsebina ni bila zaznana.");
  if (rule.source_section === 3) return segment(context.terminology, "Strokovni izraz ali glosarski sprožilec ni bil zaznan.");
  if (rule.source_section === 6) return segment(context.abbreviations, "Kratica, okrajšava ali simbol ni bil zaznan.");
  if (rule.source_section === 5) return segment(context.numeric, "Številčni, datumski, valutni ali merski zapis ni bil zaznan.");
  if (rule.source_section === 4) {
    if (rule.rule_id === "SL-SSKJ-04-001") return segment(context.suspiciousSource, "Očitna tipografska anomalija ni bila zaznana.");
    return document("unknown", "Vsebinske napake izvirnika ni mogoče zanesljivo zaznati brez presoje.");
  }
  if (rule.source_section === 14) {
    if (rule.rule_id === RULE.terminologyConsistency && context.inconsistentGlossary) return document("applicable", "Glosar vsebuje isti izvorni izraz z različnimi ciljnimi izrazi.");
    return context.terminology.length
      ? document("applicable", "Dokument vsebuje strokovne ali glosarske izraze.")
      : document("not_applicable", "Terminološki sprožilec ni bil zaznan.");
  }
  if (["SL-SSKJ-01-008", "SL-SSKJ-01-009"].includes(rule.rule_id)) return segment(context.ambiguousRanges, "Konkretna dvoumnost ni bila samodejno zaznana.");
  if ([1, 2, 12, 15, 17].includes(rule.source_section)) return context.active.length
    ? document("applicable", "Pravilo velja na ravni dokumenta.")
    : document("not_applicable", "Dokument nima prevodljive vsebine.");
  if (rule.source_section === 7) return document("unknown", "Tujejezičnega izraza ni mogoče zanesljivo razvrstiti brez jezikovne presoje.");
  return document(context.active.length ? "applicable" : "not_applicable", "Pravilo je dokumentno in zahteva presojo.");
}

function segmentUsesRule(segment: TranslationSegment, ruleId: string): boolean {
  if (segment.translation_rule_adjustments?.some((item) => {
    if (ruleId === RULE.decimalSeparator) return item.rule_id === "decimal_separator";
    return [RULE.unitSpacing, RULE.preserveValues, RULE.preservePrecision, RULE.preserveSymbols].some((candidate) => candidate === ruleId) && ["technical_range", "technical_unit_spacing"].includes(item.rule_id);
  })) return true;
  if ([RULE.userGlossary, RULE.glossaryConsistency].some((candidate) => candidate === ruleId)) return segment.glossary_matches.some((match) => match.origin === "user");
  if (ruleId === RULE.preserveMachineReadable) return segment.protected_tokens.some((token) => ["url", "email", "product_code"].includes(token.kind));
  if (ruleId === RULE.preservePlaceholders) return hasSourcePlaceholder(segment);
  return false;
}

function ruleFailed(ruleId: string, segmentIds: string[], segments: TranslationSegment[]): boolean {
  const relevant = segments.filter((segment) => segmentIds.includes(segment.segment_id));
  if (ruleId === RULE.preservePlaceholders) return relevant.some((segment) => segment.current_qc?.findings.some((finding) => finding.code === "placeholder_integrity"));
  if ([RULE.preserveValues, RULE.preservePrecision, RULE.preserveSymbols, RULE.preserveMachineReadable].some((candidate) => candidate === ruleId)) {
    return relevant.some((segment) => segment.current_qc?.findings.some((finding) => finding.code === "technical_integrity"));
  }
  return false;
}

function collectAppliedRules(segment: TranslationSegment, applied: Set<string>): void {
  if (!["translated", "reviewed"].includes(segment.status)) return;
  const automaticGlossaryApplied = segment.translation_origin === "glossary" ||
    segment.protected_tokens.some((token) => token.kind === "glossary");
  if (automaticGlossaryApplied && segment.glossary_matches.some((match) => match.origin === "user")) {
    applied.add(RULE.userGlossary);
    applied.add(RULE.glossaryConsistency);
  }
  for (const adjustment of segment.translation_rule_adjustments ?? []) {
    if (adjustment.rule_id === "decimal_separator") applied.add(RULE.decimalSeparator);
    if (adjustment.rule_id === "technical_range" || adjustment.rule_id === "technical_unit_spacing") {
      applied.add(RULE.unitSpacing);
      applied.add(RULE.preserveValues);
      applied.add(RULE.preservePrecision);
      applied.add(RULE.preserveSymbols);
    }
  }
  if (segment.translation_origin !== "provider") return;
  for (const token of segment.protected_tokens) {
    if (["url", "email", "product_code"].includes(token.kind)) applied.add(RULE.preserveMachineReadable);
    if (token.kind === "source_placeholder") applied.add(RULE.preservePlaceholders);
  }
}

function profileDiagnostics(
  glossary: GlossaryEntry[],
  segments: TranslationSegment[],
  terminologySources: TerminologySourceRecord[],
): TranslationProfileDiagnostic[] {
  const diagnostics: TranslationProfileDiagnostic[] = [];
  const hasVerifiedSource = terminologySources.some((source) =>
    source.selected_term.trim() && source.domain.trim() && source.source_or_citation?.trim() && source.user_confirmed,
  );
  const hasTerminology = segments.some((segment) => segment.glossary_matches.length || segment.protected_tokens.some((token) => token.kind === "technical_term"));
  if (hasTerminology && !hasVerifiedSource) diagnostics.push(createDiagnostic(
    "external_source_unchecked", null, "Terminološki vir ni preverjen", null, null,
    "strokovni ali uradni terminološki vir", "not_supplied",
  ));
  for (const source of terminologySources.filter((item) => item.selected_term.trim() && (!item.source_or_citation?.trim() || !item.user_confirmed))) {
    const matching = segments.filter((segment) => (segment.edited_translation || segment.translated_text).toLocaleLowerCase().includes(source.selected_term.toLocaleLowerCase()));
    diagnostics.push(createDiagnostic(
      "external_source_unchecked", null, `Terminološko vprašanje »${source.selected_term}« nima uporabniško potrjenega vira.`,
      matching[0]?.segment_id ?? null, matching[0]?.source_text ?? null,
      source.domain.trim() ? `vir za področje ${source.domain.trim()}` : "strokovni terminološki vir",
      source.source_or_citation?.trim() ? "supplied_unconfirmed" : "not_supplied",
    ));
  }
  if (hasInconsistentGlossary(glossary)) diagnostics.push(createDiagnostic(
    "warning", RULE.terminologyConsistency, "Za isti izvorni izraz je vključenih več različnih ciljnih izrazov.",
  ));
  for (const segment of segments) for (const finding of segment.current_qc?.findings.filter((item) => item.severity === "blocking") ?? []) diagnostics.push(createDiagnostic(
    "blocking_error", finding.code === "placeholder_integrity" ? RULE.preservePlaceholders : RULE.preserveValues,
    finding.message, segment.segment_id, segment.source_text,
  ));
  for (const segment of segments.filter((item) => item.current_qc?.findings.some((finding) => finding.code === "technical_range_ambiguous"))) diagnostics.push(createDiagnostic(
    "warning", RULE.preserveValues, "Zapis je podoben številčnemu razponu, vendar enota ali meja ni bila dovolj zanesljivo prepoznana.", segment.segment_id, segment.source_text,
  ));
  return diagnostics;
}

function createDiagnostic(
  kind: TranslationProfileDiagnostic["kind"],
  ruleId: string | null,
  message: string,
  segmentId: string | null = null,
  sourceText: string | null = null,
  requiredSourceType: string | null = null,
  sourceStatus: TranslationProfileDiagnostic["source_status"] = "not_required",
): TranslationProfileDiagnostic {
  return {
    kind,
    rule_id: ruleId,
    message,
    segment_id: segmentId,
    source_text: sourceText,
    required_source_type: requiredSourceType,
    source_status: sourceStatus,
  };
}

function glossaryConflicts(glossary: GlossaryEntry[]) {
  const conflicts = [];
  const builtInTerms = new Set(glossary.filter((entry) => entry.enabled && entry.origin === "built_in").map(normalizedTerm));
  if (glossary.some((entry) => entry.enabled && entry.origin === "user" && builtInTerms.has(normalizedTerm(entry)))) {
    conflicts.push(resolveTranslationRuleConflict("user_glossary", "professional_correctness"));
  }
  return conflicts;
}

function hasInconsistentGlossary(glossary: GlossaryEntry[]): boolean {
  const targets = new Map<string, Set<string>>();
  for (const entry of glossary.filter((item) => item.enabled && item.target_term.trim())) {
    const source = normalizedTerm(entry);
    const values = targets.get(source) ?? new Set<string>();
    values.add(entry.target_term.trim().toLocaleLowerCase());
    targets.set(source, values);
  }
  return [...targets.values()].some((values) => values.size > 1);
}

function normalizedTerm(entry: GlossaryEntry): string {
  return entry.source_term.trim().toLocaleLowerCase();
}

function containsTerm(text: string, term: string): boolean {
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?<![\\p{L}\\p{N}_])${escaped}(?![\\p{L}\\p{N}_])`, "iu").test(text);
}

function hasSourcePlaceholder(segment: TranslationSegment): boolean {
  return segment.protected_tokens.some((token) => token.kind === "source_placeholder") || /\{\{[^}]+\}\}|\$?\{[^}]+\}|%(?:s|d)|\[LINK\]/.test(segment.source_text);
}

function hasTechnicalMarkup(text: string): boolean {
  return /<\/?[A-Za-z][^>]*>|```|(?:^|\n)#{1,6}\s|\[[^\]]+\]\([^)]+\)|&[A-Za-z]+;/.test(text);
}
