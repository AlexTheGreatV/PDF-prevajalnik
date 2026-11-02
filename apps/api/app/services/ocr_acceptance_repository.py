"""Authoritative local OCR source acceptance, written only by explicit acceptance."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import sqlite3

from app.models import BBox


class OcrAcceptanceStorageError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Lokalna evidenca sprejetih OCR virov ni na voljo; sprejema ni mogoce potrditi.")


@dataclass(frozen=True)
class OcrAcceptance:
    revision_id: str
    document_sha256: str
    review_id: str
    page_number: int
    bbox: BBox
    text: str
    source: str
    reason: str
    registered_at: str
    accepted_at: str


SCHEMA = (
    """CREATE TABLE ocr_acceptances (
        profile TEXT NOT NULL, revision_id TEXT NOT NULL,
        document_sha256 TEXT NOT NULL, review_id TEXT NOT NULL,
        page_number INTEGER NOT NULL CHECK(page_number > 0), bbox_json TEXT NOT NULL,
        text TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL,
        registered_at TEXT NOT NULL, accepted_at TEXT NOT NULL,
        PRIMARY KEY(profile, revision_id))""",
    """CREATE TABLE ocr_acceptance_heads (
        profile TEXT NOT NULL, document_sha256 TEXT NOT NULL, review_id TEXT NOT NULL,
        revision_id TEXT NOT NULL, PRIMARY KEY(profile, document_sha256, review_id),
        FOREIGN KEY(profile, revision_id) REFERENCES ocr_acceptances(profile, revision_id))""",
)


class SqliteOcrAcceptanceRepository:
    def __init__(self, database_path: Path, profile: str) -> None:
        self.database_path = database_path.resolve()
        self.profile = profile

    @contextmanager
    def _connection(self, *, write: bool = False):
        connection = None
        try:
            if write:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
                connection.execute("PRAGMA foreign_keys=ON")
            else:
                # mode=ro neither creates a missing database nor initializes its schema.
                connection = sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True,
                                             timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if write and version == 0 and not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=1")
            elif version != 1:
                raise OcrAcceptanceStorageError()
            yield connection
            connection.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise OcrAcceptanceStorageError() from exc
        finally:
            if connection is not None:
                connection.close()  # An exceptional write transaction rolls back.

    def accept(self, *, revision_id: str, document_sha256: str, review_id: str, page_number: int,
               bbox: BBox, text: str, source: str, reason: str, registered_at: str) -> bool:
        self._validate_source(revision_id, document_sha256, page_number, bbox)
        with self._connection(write=True) as db:
            existing = db.execute("SELECT * FROM ocr_acceptances WHERE profile=? AND revision_id=?",
                                  (self.profile, revision_id)).fetchone()
            if existing is not None:
                # Retrying an old accept must never reactivate a superseded revision.
                current = db.execute("SELECT revision_id FROM ocr_acceptance_heads WHERE profile=? "
                    "AND document_sha256=? AND review_id=?", (self.profile, document_sha256, review_id)).fetchone()
                if self._record(existing) != OcrAcceptance(revision_id, document_sha256, review_id, page_number,
                        bbox, text, source, reason, registered_at, existing["accepted_at"]):
                    raise OcrAcceptanceStorageError()
                return current is not None and current["revision_id"] == revision_id
            accepted_at = datetime.now(UTC).isoformat()
            db.execute("INSERT INTO ocr_acceptances (profile,revision_id,document_sha256,review_id,page_number,"
                "bbox_json,text,source,reason,registered_at,accepted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.profile, revision_id, document_sha256, review_id, page_number,
                 bbox.model_dump_json(), text, source, reason, registered_at, accepted_at))
            db.execute("INSERT INTO ocr_acceptance_heads VALUES (?,?,?,?) "
                "ON CONFLICT(profile,document_sha256,review_id) DO UPDATE SET revision_id=excluded.revision_id",
                (self.profile, document_sha256, review_id, revision_id))
            return True

    def lookup(self, revision_id: str) -> tuple[OcrAcceptance | None, bool]:
        if not self.database_path.exists():
            return None, False
        with self._connection() as db:
            row = db.execute("SELECT * FROM ocr_acceptances WHERE profile=? AND revision_id=?",
                             (self.profile, revision_id)).fetchone()
            if row is None:
                return None, False
            record = self._record(row)
            head = db.execute("SELECT revision_id FROM ocr_acceptance_heads WHERE profile=? "
                "AND document_sha256=? AND review_id=?", (self.profile, record.document_sha256, record.review_id)).fetchone()
            if head is None:
                raise OcrAcceptanceStorageError()
            return record, head["revision_id"] == revision_id

    @staticmethod
    def _validate_source(revision_id: str, document_sha256: str, page_number: int, bbox: BBox) -> None:
        if (not re.fullmatch(r"[a-f0-9]{32}", revision_id)
                or not re.fullmatch(r"[a-f0-9]{64}", document_sha256)
                or page_number < 1 or not all(math.isfinite(v) for v in (bbox.x0, bbox.y0, bbox.x1, bbox.y1))
                or bbox.x0 >= bbox.x1 or bbox.y0 >= bbox.y1):
            raise OcrAcceptanceStorageError()

    @staticmethod
    def _record(row: sqlite3.Row) -> OcrAcceptance:
        registered_at, accepted_at = (datetime.fromisoformat(row[key]) for key in ("registered_at", "accepted_at"))
        if registered_at.utcoffset() is None or accepted_at.utcoffset() is None or accepted_at < registered_at:
            raise OcrAcceptanceStorageError()
        record = OcrAcceptance(revision_id=row["revision_id"], document_sha256=row["document_sha256"],
            review_id=row["review_id"], page_number=row["page_number"], bbox=BBox.model_validate(json.loads(row["bbox_json"])),
            text=row["text"], source=row["source"], reason=row["reason"],
            registered_at=row["registered_at"], accepted_at=row["accepted_at"])
        SqliteOcrAcceptanceRepository._validate_source(record.revision_id, record.document_sha256, record.page_number, record.bbox)
        return record
