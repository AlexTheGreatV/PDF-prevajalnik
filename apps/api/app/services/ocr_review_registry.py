from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Callable

from app.models import BBox
from app.services.ocr_acceptance_repository import SqliteOcrAcceptanceRepository

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
    registered_at: str


class OcrReviewRegistry:
    """Expiring registrations, with optional authoritative durable source acceptance."""

    def __init__(self, ttl_seconds: int = OCR_REVIEW_TTL_SECONDS,
                 *, repository_factory: Callable[[], SqliteOcrAcceptanceRepository] | None = None) -> None:
        self._ttl_seconds = ttl_seconds
        self._records: dict[str, OcrReviewRevision] = {}
        self._latest: dict[tuple[object, str, str], str] = {}
        self._scopes: dict[str, object] = {}
        self._repository_factory = repository_factory
        self._lock = threading.Lock()

    def _context(self):
        repository = self._repository_factory() if self._repository_factory is not None else None
        scope = (str(repository.database_path), repository.profile) if repository is not None else None
        return repository, scope

    def register(self, *, document_sha256: str, review_id: str, page_number: int, bbox: BBox, text: str, source: str, reason: str,
                 preferred_revision_id: str | None = None) -> OcrReviewRevision:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            repository, scope = self._context()
            current = repository.current(document_sha256, review_id) if repository is not None else None
            if current is not None and (current.page_number, current.bbox, current.text, current.source, current.reason) == (page_number, bbox, text, source, reason):
                if preferred_revision_id:
                    preferred, valid = repository.lookup(preferred_revision_id)
                    if valid and preferred is not None and repository._source_key(preferred) == repository._source_key(current):
                        current = preferred  # Keep an old job's proven alias and its separate translation review.
                revision = OcrReviewRevision(**{k: v for k, v in current.__dict__.items() if k != "accepted_at"}, accepted=True, expires_at=now + self._ttl_seconds)
                self._records[revision.revision_id] = revision
                self._scopes[revision.revision_id] = scope
                self._latest[(scope, document_sha256, review_id)] = revision.revision_id
                return revision
            revision = OcrReviewRevision(
                revision_id=uuid.uuid4().hex,
                document_sha256=document_sha256,
                review_id=review_id,
                page_number=page_number,
                bbox=bbox.model_copy(deep=True),
                text=text,
                source=source,
                reason=reason,
                accepted=False,
                expires_at=now + self._ttl_seconds,
                registered_at=datetime.now(UTC).isoformat(),
            )
            self._records[revision.revision_id] = revision
            self._scopes[revision.revision_id] = scope
            self._latest[(scope, document_sha256, review_id)] = revision.revision_id
            return revision

    def accept(self, revision_id: str) -> OcrReviewRevision | None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            repository, scope = self._context()
            record = self._records.get(revision_id)
            if (record is None or self._scopes.get(revision_id) != scope
                    or self._latest.get((scope, record.document_sha256, record.review_id)) != revision_id):
                return None
            if repository is not None and record.accepted:
                persisted, valid = repository.lookup(revision_id)
                return record if valid and persisted is not None else None
            durable = repository.accept(**{key: value for key, value in record.__dict__.items()
                if key not in {"accepted", "expires_at"}}) if repository is not None else None
            if repository is not None and durable is None:
                return None
            accepted = (OcrReviewRevision(**{k: v for k, v in durable.__dict__.items() if k != "accepted_at"}, accepted=True, expires_at=record.expires_at)
                if durable is not None else OcrReviewRevision(**{**record.__dict__, "accepted": True}))
            self._scopes[accepted.revision_id] = scope
            self._latest[(scope, record.document_sha256, record.review_id)] = accepted.revision_id
            self._records[accepted.revision_id] = accepted
            self._records[revision_id] = accepted
            return accepted

    def validate(self, *, revision_id: str, document_sha256: str, review_id: str, page_number: int, bbox: BBox, text: str,
                 source: str | None = None, reason: str | None = None) -> str | None:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            repository, scope = self._context()
            record = self._records.get(revision_id)
            if self._scopes.get(revision_id) != scope:
                record = None
            if repository is not None:
                if record is not None and not record.accepted:
                    return "unaccepted" if self._latest.get((scope, record.document_sha256, record.review_id)) == revision_id else "stale"
                record, current = repository.lookup(revision_id)
                if record is None:
                    return "unknown"
                if not current:
                    return "stale"
            else:
                if record is None:
                    return "unknown"
                if self._latest.get((scope, record.document_sha256, record.review_id)) != revision_id:
                    return "stale"
                if not record.accepted:
                    return "unaccepted"
            if record.document_sha256 != document_sha256:
                return "document_mismatch"
            bbox_matches = record.bbox == bbox if repository is not None else _same_bbox(record.bbox, bbox)
            if record.review_id != review_id or record.page_number != page_number or record.text != text or not bbox_matches:
                return "source_mismatch"
            if (source is not None and source != record.source) or (reason is not None and reason != record.reason):
                return "source_mismatch"
            return None

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._latest.clear()
            self._scopes.clear()

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._records.items() if value.expires_at <= now]
        for key in expired:
            record = self._records.pop(key)
            scope = self._scopes.pop(key, None)
            latest_key = (scope, record.document_sha256, record.review_id)
            if self._latest.get(latest_key) == key:
                self._latest.pop(latest_key, None)


def document_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _same_bbox(left: BBox, right: BBox) -> bool:
    return all(abs(getattr(left, name) - getattr(right, name)) <= 0.01 for name in ("x0", "y0", "x1", "y1"))


def _configured_repository() -> SqliteOcrAcceptanceRepository:
    from app.core.config import settings
    return SqliteOcrAcceptanceRepository(settings.resolved_ocr_database_path, settings.local_data_profile)


ocr_review_registry = OcrReviewRegistry(repository_factory=_configured_repository)
