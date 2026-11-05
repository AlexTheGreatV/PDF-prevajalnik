"""Ordered protected-data contract, parity-tested against frontend QC.

Only proven temperature ranges permit a shared end unit or an omitted plus.
Callers must obtain the source from the PDF or the accepted OCR registry first.
This is not a linguistic validator or evidence of human approval.
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from dataclasses import dataclass

SI_RULES = json.loads((Path(__file__).resolve().parents[4] / "apps/web/lib/si-measurement-rules.json").read_text(encoding="utf-8"))
RULES_VERSION = "protected-data-v2:" + SI_RULES["version"]
LETTER = r"[^\W\d_]"
ALNUM = r"[^\W_]"
NUMBER = r"[+-]?[0-9]+(?:[.,][0-9]+)?"
TEMPERATURE_UNIT = r"(?:°[ \u00a0]?[CF]|K)"
TEMPERATURE = re.compile(
    rf"(?<![\w.,+\-])(?:from |od )?({NUMBER})[ \u00a0]*({TEMPERATURE_UNIT})?"
    rf"[ \u00a0]*(?:to|do)[ \u00a0]*({NUMBER})[ \u00a0]*({TEMPERATURE_UNIT})(?!\w)"
)
IMMUTABLE = SI_RULES["unit_pattern"]
WORD_UNIT = r"(?:minutes?|hours?|days?|months?|years?)"
RANGE = re.compile(
    rf"(?<![^\W_])(?:(from|between)\s+)?({NUMBER})\s*({IMMUTABLE}|{WORD_UNIT})?"
    rf"\s*(to|and|[-–—])\s*({NUMBER})\s*({IMMUTABLE}|{WORD_UNIT})(?![^\W_])"
)
MEASUREMENT = (r"(?<![\w.,])(" + SI_RULES["number_pattern"] + r")[ \u00a0]*((?:"
    + SI_RULES["unit_pattern"] + "|" + SI_RULES["review_unit_pattern"] + r"))(?![\w/^⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹·])")
OPAQUE_MEASUREMENT = (r"(?<![\w.,])" + SI_RULES["number_pattern"] + r"[ \u00a0]*(?:"
    + SI_RULES["suspicious_unit_pattern"] + "|" + SI_RULES["opaque_unit_pattern"] + r")(?![\w/^⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹·])")
TOKEN = re.compile(
    r"https?://[^\s]+|www\.[^\s]+|[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+\.[A-Za-z]{2,}|"
    r"\{\{[A-Za-z_][A-Za-z0-9_.-]*\}\}|\$\{[A-Za-z_][A-Za-z0-9_.-]*\}|\{[A-Za-z_][A-Za-z0-9_.-]*\}|%(?:s|d)|\[LINK\]|"
    r"(?<![A-Za-z0-9_])RAL\s+[0-9]{3,5}(?![A-Za-z0-9_])|"
    r"(?<![^\W_])[+-]\s*/\s*[A-Z]\s+[0-9]+(?![^\W_])|"
    rf"(?<!{ALNUM}){SI_RULES['phone_pattern']}(?!{ALNUM})|"
    r"(?<![^\W_])\+[0-9](?:[0-9 -]{5,}[0-9])(?![^\W_])|"
    + MEASUREMENT + "|" + OPAQUE_MEASUREMENT + r"|(?<![^\W_])CO[₂2](?![^\W_])|"
    rf"(?<!{LETTER})(?=[A-Za-z0-9₂₃₄₅₆₇₈₉]*[₂₃₄₅₆₇₈₉])(?:[A-Z][a-z]?[0-9₂₃₄₅₆₇₈₉]*){{2,}}(?!{LETTER})|"
    r"(?<![A-Za-z0-9_])[A-Z]{1,8}[-/]?(?=[A-Z0-9/-]*[0-9])[A-Z0-9/-]{1,20}(?![A-Za-z0-9_])|"
    r"(?<![^\W_])[0-9]+[A-Z][A-Z0-9/-]*(?![^\W_])|" + NUMBER
)


@dataclass(frozen=True)
class IntegrityResult:
    valid: bool
    expected: list[str]
    detected: list[str]


def validate_technical_integrity(source: str, target: str, *, document_sha256: str | None = None,
                                 segment_id: str | None = None, page_number: int | None = None,
                                 source_bbox: dict | None = None, source_kind: str | None = None) -> IntegrityResult:
    source, target = source.replace("−", "-"), target.replace("−", "-")
    expected, detected = _signatures(source), _signatures(target)
    bad_percent = re.compile(r"[+-]?[0-9]+(?:[.,][0-9]+)?(?:[ \u00a0]{2,}|[\t\r\n]+)%")
    valid = expected == detected and not bad_percent.search(source) and not bad_percent.search(target)
    valid = valid and not any(s.startswith("inconsistent-temperature-units:") for s in expected + detected)
    if not valid and document_sha256 and segment_id:
        from app.services.reviewed_unit_correction import load_reviewed_unit_correction
        row = load_reviewed_unit_correction()
        a, b = re.fullmatch(MEASUREMENT, source.strip()), re.fullmatch(MEASUREMENT, target.strip())
        if (row is not None and a and b and row["document_sha256"] == document_sha256
                and row["page_number"] == page_number and row["source_bbox"] == source_bbox and row["source_kind"] == source_kind
                and row["segment_id"] == segment_id and row["source_text"] == source.strip()
                and re.fullmatch(SI_RULES["review_unit_pattern"], a[2])
                and not re.fullmatch(SI_RULES["review_unit_pattern"], b[2])
                and _si_number(a[1]) == _si_number(b[1]) and row["target_unit"] == b[2]):
            valid = True
    return IntegrityResult(bool(valid), expected, detected)


def _normalize(value: str) -> str:
    value = re.sub(SI_RULES["number_pattern"], lambda m: _si_number(m[0]), value)
    percentage = re.fullmatch(r"([+-]?[0-9]+(?:,[0-9]+)?)(?: |\u00a0)?%", value)
    return percentage[1] + "%" if percentage else " ".join(value.split())


def _si_number(value: str) -> str:
    value = value.replace("−", "-")
    return value if re.fullmatch(SI_RULES["ambiguous_number_pattern"], value) else value.replace(".", ",")


def _si_signature(value: str) -> str:
    match = re.fullmatch(MEASUREMENT, value)
    if re.fullmatch(SI_RULES["humidity_unit_pattern"], match[2]):
        return _si_number(match[1]) + " % r. v."
    unit = SI_RULES["accepted_unit_aliases"].get(match[2], re.sub(r"[ \u00a0]", "", match[2]))
    unit = re.sub(r"[⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+", lambda m: "^" + "".join(SI_RULES["superscript"][c] for c in m[0]), unit)
    return _si_number(match[1]) + ("" if unit in {"°", "′", "″"} else " ") + unit


def _signatures(text: str) -> list[str]:
    values, cursor = [], 0
    for match in TEMPERATURE.finditer(text):
        values.extend(_range_signatures(text[cursor:match.start()]))
        first = re.sub(r"[ \u00a0]", "", match[2] or "")
        last = re.sub(r"[ \u00a0]", "", match[4])
        if first and first != last:
            values.append("inconsistent-temperature-units:" + match[0])
        else:
            values.append(f"temperature-range:{_normalize(match[1]).lstrip('+')}:{_normalize(match[3]).lstrip('+')}:{last}")
        cursor = match.end()
    return values + _range_signatures(text[cursor:])


def _range_signatures(text: str) -> list[str]:
    values, cursor = [], 0
    contacts = [(m.start(), m.end()) for m in re.finditer(SI_RULES["phone_pattern"], text)]
    for match in RANGE.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in contacts):
            continue
        prefix, start, first, connector, end, last = match.groups()
        if any(re.fullmatch(SI_RULES["ambiguous_number_pattern"], value) for value in (start, end)):
            continue
        prefix, connector = (prefix or "").lower(), connector.lower()
        first, last = re.sub(r"\s+", "", first or ""), re.sub(r"\s+", "", last)
        family = lambda value: re.sub(r"s$", "", value.lower()) if re.fullmatch(WORD_UNIT, value, re.I) else value
        if (prefix == "from" and connector != "to") or (prefix == "between" and connector != "and") or (not prefix and connector == "and") or (first and family(first) != family(last)):
            continue
        values.extend(_tokens(text[cursor:match.start()]))
        if re.fullmatch(WORD_UNIT, last, re.I):
            values.extend([_normalize(start), _normalize(end)])
        else:
            values.extend([_normalize(start + (" " + first if first else "")), _normalize(end + " " + last)])
        cursor = match.end()
    return values + _tokens(text[cursor:])


def _tokens(text: str) -> list[str]:
    values = []
    for match in TOKEN.finditer(text):
        value = match[0]
        if re.fullmatch(MEASUREMENT, value):
            labeled = re.search(SI_RULES["machine_label_pattern"], text[max(0, match.start()-24):match.start()], re.I)
            labeled = labeled or (text[max(0, match.start()-1):match.start()] == "/" and
                re.fullmatch(r"[A-Za-z0-9_]", text[max(0, match.start()-2):match.start()-1]))
            values.append(value if labeled else _si_signature(value))
        elif re.fullmatch(NUMBER, value):
            before, after = text[max(0, match.start()-1):match.start()], text[match.end():match.end()+1]
            prefix = text[max(0, match.start()-24):match.start()]
            unsafe = (re.search(r"[\w/-]", before + after)
                      or (before == "." and re.search(r"[^\W_]", text[max(0, match.start()-2):match.start()-1]))
                      or (after == "." and re.search(r"[^\W_]", text[match.end()+1:match.end()+2]))
                      or re.search(r"\b(?:version|ver|code|id|identifier|serial|s/n|sn|ISO|EN|DIN)\s*$", prefix, re.I))
            values.append(value if unsafe else _normalize(value))
        else:
            # URLs, identifiers, formulae and literal placeholders are not decimals.
            values.append(value)
    return values
