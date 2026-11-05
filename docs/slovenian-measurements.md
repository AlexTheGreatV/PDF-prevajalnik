# Slovenian measurement notation and OCR acceptance identity

## Measurement contract

`apps/web/lib/si-measurement-rules.json` is the versioned data contract consumed
by the existing TypeScript tokenizer/QC and Python protected-data validator.
It declares supported symbols, case-sensitive prefixes, compound units and
powers; it does not guess unrecognized source units or convert quantities.

The numerical value and unit are separated by a space (NBSP in new Slovenian
output). Plane-angle symbols °, ′ and ″ have no intervening space; °C is a
temperature unit and does have one. See [NIST SI Guide, 7.2](https://www.nist.gov/pml/special-publication-811/nist-guide-si-chapter-7-rules-and-style-conventions-expressing-values).

Decimal comma is an explicit choice of this project's Slovenian profile, not
a claim that SI forbids decimal points. [CIPM Recommendation 2 (2003)](https://www.bipm.org/en/-/committees/ci/cipm/92-2003/resolution-2)
permits either marker. Preserve every digit, sign, exponent and ordered range
bound. Do not perform floating-point rounding or unit/prefix conversion.
Three-digit fractional/grouping forms such as `1,000` are left unchanged and
flagged as ambiguous. Codes, standards, product identifiers, URLs and e-mail
addresses are not localized numbers. `2K` remains a protected designation.

No global `gr -> g` or `Mpa -> MPa` rule exists. Such source spellings are
reported for traceable review. An approved correction of a source unit is
different from formatting equivalence and must not silently weaken integrity.

One explicitly approved source-unit spelling may be enabled using
`REVIEWED_UNIT_CORRECTION_PATH` and `REVIEWED_UNIT_CORRECTION_SHA256`.
Both are disabled by default. The record is a single hash-pinned object, not
a list of exceptions. It binds the current profile, actual PDF hash, segment,
page, exact source bbox and source kind, complete source quantity and reviewed target unit. It cannot specify arbitrary
translated paragraphs or change the quantity's digits, sign or precision.
Only a recognized problematic source-unit spelling qualifies. The normal
validator continues to check the authoritative source before considering this
record for the target. Plans, writes and table reconstruction repeat the same
check. Client claims and imported JSON do not grant permission.

The non-secret verified record is exposed through the existing environment
response. The workspace reloads it from the API; imports discard any saved
permission claim. Its hash and approval note are retained in the before/after
adjustment history. Keep the record and its pinned SHA with the profile's
backups, outside Git. Do not compute a new trusted pin automatically when a
record changes. A missing, changed or wrong-profile record fails closed.

For an existing job, a separate preview lists full before/after text. Only
the explicit apply action edits targets, using the ordinary edit path: changed
reviews become invalid, QC is recomputed and old PDF plans/previews are stale.
Unchanged reviews, source text, OCR provenance and reconstruction choices are
preserved. Each applied change records rule version and before/after notation.

## OCR duplicates

Identical durable OCR content reuses its existing acceptance and actual
registration/acceptance metadata. Registration may prefer an existing job's
reference only after the server validates that reference against durable
evidence. A JSON reference never grants acceptance on its own.

Legacy duplicate references remain valid only across a continuous sequence of
identical durable records for the same profile, document, review ID, page,
exact bbox, text, source and reason. A -> B -> A does not resurrect the first A.
Missing evidence fails closed. Import/verify never writes an acceptance.

The OCR SQLite schema is backward-readable at version 1; the next explicit
write migrates to version 2 and adds invalidation barriers. Administrative
revocation invalidates the head and records the barrier, so accepting identical
content later cannot revive revoked references. No new revoke UI is introduced.
Keep the entire OCR database and its history in the existing SQLite backup.
Do not delete acceptance history rows or independently manipulate their rowids.

OCR source acceptance and translation review remain separate. Reusing an
unchanged source reference neither creates nor invents a translation approval.

## Relative humidity (Slovenian profile)

Version sl-si-measurements-v2 treats the numeric percentage together with its
explicit relative-humidity qualifier as one protected measurement. Supported
Rh./RH and r.v./r. v. notations canonicalize to `% r. v.` in integrity checks.
The Slovenian preview proposes NBSP between number, percent and qualifier.
Digits, precision and sign stay unchanged. Removing the qualifier, percent,
or changing the value/unit remains a mismatch. URL, code and machine-label
protection still precedes normalization. This is a notation rule, not a general
translation or permission to approve a changed segment.

## Source style groups

The opt-in document-role typography profile uses observed source emphasis:
a structurally detected heading with regular source text is not promoted to
bold. Equivalent regular row labels and codes therefore share the same body
style. Section headings retain their distinct source size, bold face and color.
The layout revision participates in the profile fingerprint and export revision.
Only proven-uniform, exactly equal background patches may intersect each other;
their intersections with measured glyphs or erasure rectangles still block.

## Temperaturni razponi — dogovorjeni slovenski predlog

Dokazani temperaturni razponi z `to` oziroma `from ... to` se pri novih predlogih zapisujejo `od −40 do 90 °C`: enota je na koncu, nepotrebni pozitivni plus se izpusti, negativni predznak in natančnost ostaneta. Pravilo je v obstoječem `technical-ranges.ts` (v4), brez izjem za dokumente. Izvor ostane nespremenjen; obstoječih potrjenih prevodov ne prepisujemo. Oblike `between ... and` in dvoumni zapisi ostanejo v dosedanji konservativni obravnavi. Backend še vedno preverja obe meji in enoto in ne izvaja pretvarjanja med enotami.
