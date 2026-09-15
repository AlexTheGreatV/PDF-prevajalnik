from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass

from app.models import BBox

OCR_REVIEW_TTL_SECONDS = 30 * 60


@dataclass(frozen=True)
class OcrReviewRevision:
    revision_id: str
    document_sha256: str
    review_id: str
    page_number: int
    bbox: BBox
    text: str
    source: str
    reason: str
    accepted: bool
    expires_at: float


class OcrReviewRegistry:
    """Process-local, expiring registry; deliberately fail-closed after restart."""

    def __init__(self, ttl_seconds: int = OCR_REVIEW_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._records: dict[str, OcrReviewRevision] = {}
        self._latest: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def register(self, *, document_sha256: str, review_id: str, page_number: int, bbox: BBox, text: str, source: str, reason: str) -> OcrReviewRevision:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            revision = OcrReviewRevision(
                revision_id=uuid.uuid4().hex,
                document_sha256=document_sha256,
                review_id=review_id,
                page_number=page_number,
                bbox=bbox,
                text=text,
                source=source,
                reason=reason,
                accepted=False,
                expires_at=now + self._ttl_seconds,
            )
            self._records[revision.revision_id] = revision
            self._latest[(document_sha256, review_id)] = revision.revision_id
            return revision

    def accept(self, revision_id: str) -> OcrReviewRevision | None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            record = self._records.get(revision_id)
            if record is None or self._latest.get((record.document_sha256, record.review_id)) != revision_id:
                return None
            accepted = OcrReviewRevision(**{**record.__dict__, "accepted": True})
            self._records[revision_id] = accepted
            return accepted

    def validate(self, *, revision_id: str, document_sha256: str, review_id: str, page_number: int, bbox: BBox, text: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            record = self._records.get(revision_id)
            if record is None:
                return "unknown"
            if self._latest.get((record.document_sha256, record.review_id)) != revision_id:
                return "stale"
            if not record.accepted:
                return "unaccepted"
            if record.document_sha256 != document_sha256:
                return "document_mismatch"
            if record.review_id != review_id or record.page_number != page_number or record.text != text or not _same_bbox(record.bbox, bbox):
                return "source_mismatch"
            return None

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._latest.clear()

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._records.items() if value.expires_at <= now]
        for key in expired:
            record = self._records.pop(key)
            latest_key = (record.document_sha256, record.review_id)
            if self._latest.get(latest_key) == key:
                self._latest.pop(latest_key, None)


def document_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _same_bbox(left: BBox, right: BBox) -> bool:
    return all(abs(getattr(left, name) - getattr(right, name)) <= 0.01 for name in ("x0", "y0", "x1", "y1"))


ocr_review_registry = OcrReviewRegistry()
