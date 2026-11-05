"""One hash-pinned, explicitly approved unit spelling, disabled by default.

No list, write endpoint, arbitrary target text, or client-supplied permission.
"""
import hashlib
import json
import re
import math

from app.core.config import settings


def load_reviewed_unit_correction() -> dict | None:
    path, digest = settings.reviewed_unit_correction_path, settings.reviewed_unit_correction_sha256
    if path is None and digest is None:
        return None
    if path is None or digest is None or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Manjka pripeto dokazilo odobrene izvorne enote.")
    data = path.read_bytes()
    if len(data) > 4096 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Dokazilo izvorne enote se ne ujema s pripetim SHA-256.")
    row = json.loads(data)
    keys = {"version", "profile", "document_sha256", "segment_id", "source_text", "target_unit", "approval_note", "recorded_at", "page_number", "source_bbox", "source_kind"}
    if (not isinstance(row, dict) or set(row) != keys or row["version"] != 1
            or row["profile"] != settings.local_data_profile
            or any(not isinstance(row[k], str) or not row[k] for k in keys - {"version", "page_number", "source_bbox"})
            or not re.fullmatch(r"[a-f0-9]{64}", row["document_sha256"])):
        raise ValueError("Dokazilo ni en sam veljaven, profilno vezan merski popravek.")
    box = row["source_bbox"]
    if (type(row["page_number"]) is not int or row["page_number"] < 1 or row["source_kind"] not in {"native", "accepted_ocr"}
            or not isinstance(box, dict) or set(box) != {"x0", "y0", "x1", "y1"}
            or any(type(v) not in {int, float} or not math.isfinite(v) for v in box.values())
            or box["x0"] >= box["x1"] or box["y0"] >= box["y1"]):
        raise ValueError("Dokazilo nima natančnega izvornega območja.")
    return {**row, "evidence_sha256": digest}
