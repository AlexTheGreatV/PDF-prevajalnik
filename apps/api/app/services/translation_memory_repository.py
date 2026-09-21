from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from app.translation_memory_models import (
    TranslationMemoryEntry,
    TranslationMemoryEntryCreate,
    TranslationMemoryEntryUpdate,
    TranslationMemoryHistoryRecord,
    TranslationMemoryLookupResponse,
    TranslationMemoryStats,
)

SCHEMA_VERSION = 1


class TranslationMemoryRepositoryError(RuntimeError):
    pass


class TranslationMemoryNotFoundError(TranslationMemoryRepositoryError):
    pass


class TranslationMemoryRevisionConflictError(TranslationMemoryRepositoryError):
    def __init__(self, current: TranslationMemoryEntry) -> None:
        super().__init__("Vnos prevajalskega spomina je bil medtem spremenjen.")
        self.current = current


MIGRATIONS: tuple[tuple[int, str], ...] = ((1, """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translation_memory_entries (
    id TEXT PRIMARY KEY,
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    normalized_source TEXT NOT NULL,
    normalized_source_sha256 TEXT NOT NULL,
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'archived')),
    origin TEXT NOT NULL CHECK (origin = 'manual_review'),
    source_document_sha256 TEXT,
    source_segment_id TEXT,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('native', 'accepted_ocr')),
    note TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    usage_count INTEGER NOT NULL DEFAULT 0 CHECK (usage_count >= 0),
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS translation_memory_history (
    history_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'approve', 'reject', 'update', 'archive', 'restore')),
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES translation_memory_entries(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_tm_exact ON translation_memory_entries(
    normalized_source_sha256, source_language, target_language, status, domain
);
CREATE INDEX IF NOT EXISTS idx_tm_history ON translation_memory_history(entry_id, revision);
"""),)


def normalize_source_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def source_sha256(value: str) -> str:
    return hashlib.sha256(normalize_source_text(value).encode("utf-8")).hexdigest()


class SqliteTranslationMemoryRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._migration_lock = threading.Lock()
        self._migrated = False

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        except (OSError, sqlite3.Error) as exc:
            raise TranslationMemoryRepositoryError("Lokalni prevajalski spomin ni na voljo.") from exc
        finally:
            if "connection" in locals():
                connection.close()

    def initialize(self) -> None:
        if self._migrated:
            return
        with self._migration_lock:
            if self._migrated:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")} if exists else set()
                for version, script in MIGRATIONS:
                    if version in applied:
                        continue
                    try:
                        timestamp = _now().replace("'", "''")
                        connection.executescript(
                            f"BEGIN IMMEDIATE;\n{script}\n"
                            f"INSERT INTO schema_migrations(version, applied_at) VALUES ({version}, '{timestamp}');\nCOMMIT;"
                        )
                    except Exception:
                        if connection.in_transaction:
                            connection.rollback()
                        raise
            self._migrated = True

    def schema_version(self) -> int:
        self.initialize()
        with self._connection() as connection:
            return int(connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])

    def list_entries(
        self, *, query: str | None = None, source_language: str | None = None,
        target_language: str | None = None, domain: str | None = None,
        status: str | None = None, limit: int = 200, offset: int = 0,
    ) -> tuple[list[TranslationMemoryEntry], int]:
        self.initialize()
        clauses: list[str] = []
        values: list[object] = []
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(source_text LIKE ? ESCAPE '\\' OR target_text LIKE ? ESCAPE '\\')")
            values.extend((f"%{escaped}%", f"%{escaped}%"))
        for column, value in (("source_language", source_language), ("target_language", target_language),
                              ("domain", domain), ("status", status)):
            if value is not None and value != "":
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM translation_memory_entries{where}", values).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM translation_memory_entries{where} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return [_entry(row) for row in rows], total

    def list_entries_snapshot(self, **filters: object) -> tuple[list[TranslationMemoryEntry], int, str]:
        self.initialize()
        query = filters.get("query")
        clauses: list[str] = []
        values: list[object] = []
        if isinstance(query, str) and query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(source_text LIKE ? ESCAPE '\\' OR target_text LIKE ? ESCAPE '\\')")
            values.extend((f"%{escaped}%", f"%{escaped}%"))
        for column in ("source_language", "target_language", "domain", "status"):
            value = filters.get(column)
            if isinstance(value, str) and value:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = int(filters.get("limit", 200))
        offset = int(filters.get("offset", 0))
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                total = int(connection.execute(f"SELECT COUNT(*) FROM translation_memory_entries{where}", values).fetchone()[0])
                rows = connection.execute(
                    f"SELECT * FROM translation_memory_entries{where} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                    (*values, limit, offset),
                ).fetchall()
                active_hash = self.active_set_hash(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return [_entry(row) for row in rows], total, active_hash

    def get_entry(self, entry_id: str) -> TranslationMemoryEntry:
        self.initialize()
        with self._connection() as connection:
            return self._get(connection, entry_id)

    def create_entry(self, payload: TranslationMemoryEntryCreate) -> TranslationMemoryEntry:
        self.initialize()
        entry_id = str(uuid.uuid4())
        timestamp = _now()
        normalized = normalize_source_text(payload.source_text)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO translation_memory_entries(
                    id, source_text, target_text, normalized_source, normalized_source_sha256,
                    source_language, target_language, domain, status, origin, source_document_sha256,
                    source_segment_id, source_kind, note, revision, created_at, updated_at,
                    approved_at, usage_count, last_used_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', 'manual_review', ?, ?, ?, ?, 1, ?, ?, NULL, 0, NULL)""",
                    (entry_id, payload.source_text, payload.target_text, normalized, source_sha256(normalized),
                     payload.source_language, payload.target_language, payload.domain,
                     payload.source_document_sha256, payload.source_segment_id, payload.source_kind,
                     payload.note, timestamp, timestamp),
                )
                entry = self._get(connection, entry_id)
                self._history(connection, entry, "create")
                connection.commit()
                return entry
            except Exception:
                connection.rollback()
                raise

    def update_entry(self, entry_id: str, payload: TranslationMemoryEntryUpdate) -> TranslationMemoryEntry:
        updates = payload.model_dump(exclude={"expected_revision"}, exclude_unset=True)
        if "source_text" in updates:
            normalized = normalize_source_text(str(updates["source_text"]))
            updates.update(normalized_source=normalized, normalized_source_sha256=source_sha256(normalized))
        updates.update(status="proposed", approved_at=None)
        return self._write(entry_id, payload.expected_revision, updates, "update")

    def transition(self, entry_id: str, expected_revision: int, status: str, action: str) -> TranslationMemoryEntry:
        updates: dict[str, object] = {"status": status}
        updates["approved_at"] = _now() if status == "approved" else None
        return self._write(entry_id, expected_revision, updates, action)

    def _write(self, entry_id: str, expected_revision: int, updates: dict[str, object], action: str) -> TranslationMemoryEntry:
        allowed = {
            "source_text", "target_text", "normalized_source", "normalized_source_sha256",
            "source_language", "target_language", "domain", "source_document_sha256",
            "source_segment_id", "source_kind", "note", "status", "approved_at",
        }
        if not updates or not set(updates).issubset(allowed):
            raise TranslationMemoryRepositoryError("Posodobitev prevajalskega spomina ni veljavna.")
        self.initialize()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._get(connection, entry_id)
                if current.revision != expected_revision:
                    raise TranslationMemoryRevisionConflictError(current)
                assignments = [f"{key} = ?" for key in updates]
                cursor = connection.execute(
                    f"UPDATE translation_memory_entries SET {', '.join(assignments)}, revision = ?, updated_at = ? "
                    "WHERE id = ? AND revision = ?",
                    (*updates.values(), expected_revision + 1, _now(), entry_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise TranslationMemoryRevisionConflictError(self._get(connection, entry_id))
                entry = self._get(connection, entry_id)
                self._history(connection, entry, action)
                connection.commit()
                return entry
            except Exception:
                connection.rollback()
                raise

    def history(self, entry_id: str) -> list[TranslationMemoryHistoryRecord]:
        self.get_entry(entry_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM translation_memory_history WHERE entry_id = ? ORDER BY revision, created_at", (entry_id,)
            ).fetchall()
        return [TranslationMemoryHistoryRecord(
            history_id=row["history_id"], entry_id=row["entry_id"], revision=row["revision"],
            action=row["action"], snapshot=TranslationMemoryEntry.model_validate_json(row["snapshot_json"]),
            created_at=row["created_at"],
        ) for row in rows]

    def exact_lookup(self, source_text: str, source_language: str, target_language: str, domain: str = "") -> TranslationMemoryLookupResponse:
        normalized = normalize_source_text(source_text)
        digest = source_sha256(normalized)
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM translation_memory_entries
                WHERE normalized_source_sha256 = ? AND normalized_source = ?
                AND source_language = ? AND target_language = ? AND status = 'approved'
                AND (domain = ? OR domain = '') ORDER BY domain DESC, updated_at DESC""",
                (digest, normalized, source_language, target_language, domain),
            ).fetchall()
        entries = [_entry(row) for row in rows]
        specific = [entry for entry in entries if domain and entry.domain == domain]
        candidates = specific or [entry for entry in entries if entry.domain == ""]
        if not candidates:
            return TranslationMemoryLookupResponse(status="none", normalized_source_sha256=digest)
        distinct_targets = {entry.target_text for entry in candidates}
        if len(distinct_targets) > 1:
            return TranslationMemoryLookupResponse(status="conflict", candidates=candidates, normalized_source_sha256=digest)
        return TranslationMemoryLookupResponse(status="match", match=candidates[0], candidates=candidates, normalized_source_sha256=digest)

    def record_usage(self, entry_ids: list[str]) -> None:
        self.initialize()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                timestamp = _now()
                for entry_id in set(entry_ids):
                    connection.execute(
                        "UPDATE translation_memory_entries SET usage_count = usage_count + 1, last_used_at = ? WHERE id = ?",
                        (timestamp, entry_id),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def active_set_hash(self, connection: sqlite3.Connection | None = None) -> str:
        self.initialize()
        if connection is None:
            with self._connection() as opened:
                return self.active_set_hash(opened)
        rows = connection.execute(
            "SELECT id, revision, normalized_source_sha256, target_text, domain FROM translation_memory_entries "
            "WHERE status = 'approved' ORDER BY id"
        ).fetchall()
        return hashlib.sha256(json.dumps([tuple(row) for row in rows], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def stats(self) -> TranslationMemoryStats:
        self.initialize()
        with self._connection() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM translation_memory_entries").fetchone()[0])
            by_status = dict(connection.execute("SELECT status, COUNT(*) FROM translation_memory_entries GROUP BY status").fetchall())
            active_hash = self.active_set_hash(connection)
        return TranslationMemoryStats(total=total, by_status=by_status, active_set_hash=active_hash)

    def export_snapshot(self) -> dict[str, object]:
        self.initialize()
        with self._connection() as connection:
            connection.execute("BEGIN")
            try:
                entries = [_entry(row) for row in connection.execute(
                    "SELECT * FROM translation_memory_entries ORDER BY created_at, id"
                ).fetchall()]
                active_hash = self.active_set_hash(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"schema_version": SCHEMA_VERSION, "active_set_hash": active_hash,
                "entries": [entry.model_dump(mode="json") for entry in entries]}

    def _get(self, connection: sqlite3.Connection, entry_id: str) -> TranslationMemoryEntry:
        row = connection.execute("SELECT * FROM translation_memory_entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise TranslationMemoryNotFoundError("Vnos prevajalskega spomina ne obstaja.")
        return _entry(row)

    def _history(self, connection: sqlite3.Connection, entry: TranslationMemoryEntry, action: str) -> None:
        connection.execute(
            "INSERT INTO translation_memory_history VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), entry.id, entry.revision, action,
             entry.model_dump_json(), _now()),
        )


def _entry(row: sqlite3.Row) -> TranslationMemoryEntry:
    return TranslationMemoryEntry.model_validate(dict(row))


def _now() -> str:
    return datetime.now(UTC).isoformat()
