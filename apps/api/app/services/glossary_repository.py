from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Protocol

from app.glossary_models import (
    GlossaryAction,
    GlossaryEntry,
    GlossaryEntryCreate,
    GlossaryEntryUpdate,
    GlossaryHistoryRecord,
    GlossaryStats,
)

SCHEMA_VERSION = 1


class GlossaryRepositoryError(RuntimeError):
    pass


class GlossaryNotFoundError(GlossaryRepositoryError):
    pass


class GlossaryRevisionConflictError(GlossaryRepositoryError):
    def __init__(self, current: GlossaryEntry) -> None:
        super().__init__("Vnos je bil medtem spremenjen. Osvežite podatke in poskusite znova.")
        self.current = current


class GlossaryReadOnlyError(GlossaryRepositoryError):
    pass


class GlossaryRepository(Protocol):
    def list_entries(self, **filters: object) -> tuple[list[GlossaryEntry], int]: ...
    def get_entry(self, entry_id: str) -> GlossaryEntry: ...
    def create_entry(self, payload: GlossaryEntryCreate, action: GlossaryAction = "create") -> GlossaryEntry: ...
    def update_entry(self, entry_id: str, payload: GlossaryEntryUpdate) -> GlossaryEntry: ...
    def transition(self, entry_id: str, expected_revision: int, status: str, action: GlossaryAction) -> GlossaryEntry: ...
    def history(self, entry_id: str) -> list[GlossaryHistoryRecord]: ...
    def stats(self) -> GlossaryStats: ...
    def active_set_hash(self) -> str: ...
    def import_overwrite(self, entry_id: str, expected_revision: int, payload: GlossaryEntryCreate) -> GlossaryEntry: ...
    def record_usage(self, entry_ids: list[str]) -> None: ...


MIGRATIONS: tuple[tuple[int, str], ...] = ((1, """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary_entries (
    id TEXT PRIMARY KEY,
    source_term TEXT NOT NULL,
    target_term TEXT NOT NULL,
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    match_mode TEXT NOT NULL CHECK (match_mode IN ('exact_segment', 'preferred_term', 'do_not_translate')),
    case_sensitive INTEGER NOT NULL CHECK (case_sensitive IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'archived')),
    origin TEXT NOT NULL CHECK (origin IN ('manual', 'reviewed_correction', 'import', 'built_in')),
    note TEXT NOT NULL DEFAULT '',
    source_label TEXT,
    document_hash TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    usage_count INTEGER NOT NULL DEFAULT 0 CHECK (usage_count >= 0),
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS glossary_history (
    history_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create', 'update', 'approve', 'reject', 'archive', 'restore', 'import')),
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (entry_id) REFERENCES glossary_entries(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_glossary_lookup
    ON glossary_entries(source_language, target_language, status, match_mode);
CREATE INDEX IF NOT EXISTS idx_glossary_terms
    ON glossary_entries(source_term, target_term);
CREATE INDEX IF NOT EXISTS idx_glossary_history_entry
    ON glossary_history(entry_id, revision);
"""),)


class SqliteGlossaryRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._migration_lock = threading.Lock()
        self._migrated = False

    def initialize(self) -> None:
        if self._migrated:
            return
        with self._migration_lock:
            if self._migrated:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                current = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
                applied = set()
                if current:
                    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
                for version, script in MIGRATIONS:
                    if version in applied:
                        continue
                    try:
                        timestamp = _now().replace("'", "''")
                        connection.executescript(
                            f"BEGIN IMMEDIATE;\n{script}\n"
                            f"INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES ({version}, '{timestamp}');\n"
                            "COMMIT;"
                        )
                    except Exception:
                        if connection.in_transaction:
                            connection.rollback()
                        raise
            self._migrated = True

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
            raise GlossaryRepositoryError("Lokalna glosarska baza ni na voljo.") from exc
        finally:
            if "connection" in locals():
                connection.close()

    def schema_version(self) -> int:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
            return int(row[0])

    def list_entries(
        self,
        *,
        query: str | None = None,
        source_language: str | None = None,
        target_language: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        match_mode: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[GlossaryEntry], int]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[object] = []
        if query:
            clauses.append("(source_term LIKE ? ESCAPE '\\' OR target_term LIKE ? ESCAPE '\\')")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.extend((f"%{escaped}%", f"%{escaped}%"))
        for column, value in (
            ("source_language", source_language), ("target_language", target_language),
            ("domain", domain), ("status", status), ("match_mode", match_mode),
        ):
            if value:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM glossary_entries{where}", parameters).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM glossary_entries{where} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return [_entry(row) for row in rows], total

    def get_entry(self, entry_id: str) -> GlossaryEntry:
        self.initialize()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM glossary_entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise GlossaryNotFoundError("Glosarski vnos ne obstaja.")
        return _entry(row)

    def create_entry(self, payload: GlossaryEntryCreate, action: GlossaryAction = "create") -> GlossaryEntry:
        self.initialize()
        entry_id = str(uuid.uuid4())
        timestamp = _now()
        values = payload.model_dump()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO glossary_entries(
                        id, source_term, target_term, source_language, target_language, domain,
                        match_mode, case_sensitive, status, origin, note, source_label,
                        document_hash, revision, created_at, updated_at, usage_count, last_used_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, NULL)""",
                    (
                        entry_id, values["source_term"], values["target_term"], values["source_language"],
                        values["target_language"], values["domain"], values["match_mode"],
                        int(values["case_sensitive"]), values["status"], values["origin"], values["note"],
                        values["source_label"], values["document_hash"], timestamp, timestamp,
                    ),
                )
                entry = self._get_in_connection(connection, entry_id)
                self._append_history(connection, entry, action)
                connection.commit()
                return entry
            except Exception:
                connection.rollback()
                raise

    def update_entry(self, entry_id: str, payload: GlossaryEntryUpdate) -> GlossaryEntry:
        current = self.get_entry(entry_id)
        if current.origin == "built_in":
            raise GlossaryReadOnlyError("Vgrajenega vnosa ni mogoče neposredno urejati.")
        updates = payload.model_dump(exclude={"expected_revision"}, exclude_unset=True)
        if not updates:
            return current
        merged = current.model_dump()
        merged.update(updates)
        validated = GlossaryEntryCreate.model_validate({
            key: merged[key] for key in GlossaryEntryCreate.model_fields
        } | {"origin": current.origin})
        normalized_updates = {key: getattr(validated, key) for key in updates}
        return self._write_update(entry_id, payload.expected_revision, normalized_updates, "update")

    def transition(self, entry_id: str, expected_revision: int, status: str, action: GlossaryAction) -> GlossaryEntry:
        current = self.get_entry(entry_id)
        if current.origin == "built_in":
            raise GlossaryReadOnlyError("Vgrajenega vnosa ni mogoče spreminjati.")
        return self._write_update(entry_id, expected_revision, {"status": status}, action)

    def import_overwrite(self, entry_id: str, expected_revision: int, payload: GlossaryEntryCreate) -> GlossaryEntry:
        current = self.get_entry(entry_id)
        if current.origin == "built_in":
            raise GlossaryReadOnlyError("Vgrajenega vnosa ni mogoče prepisati.")
        updates = payload.model_dump(exclude={"origin"})
        return self._write_update(entry_id, expected_revision, updates, "import")

    def _write_update(self, entry_id: str, expected_revision: int, updates: dict[str, object], action: GlossaryAction) -> GlossaryEntry:
        allowed = {
            "source_term", "target_term", "source_language", "target_language", "domain",
            "match_mode", "case_sensitive", "status", "note", "source_label", "document_hash",
        }
        if not set(updates).issubset(allowed):
            raise GlossaryRepositoryError("Posodobitev vsebuje nedovoljena polja.")
        self.initialize()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._get_in_connection(connection, entry_id)
                if current.revision != expected_revision:
                    raise GlossaryRevisionConflictError(current)
                next_revision = current.revision + 1
                assignments = [f"{key} = ?" for key in updates]
                values = [int(value) if key == "case_sensitive" else value for key, value in updates.items()]
                assignments.extend(("revision = ?", "updated_at = ?"))
                cursor = connection.execute(
                    f"UPDATE glossary_entries SET {', '.join(assignments)} WHERE id = ? AND revision = ?",
                    (*values, next_revision, _now(), entry_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise GlossaryRevisionConflictError(self._get_in_connection(connection, entry_id))
                entry = self._get_in_connection(connection, entry_id)
                self._append_history(connection, entry, action)
                connection.commit()
                return entry
            except Exception:
                connection.rollback()
                raise

    def history(self, entry_id: str) -> list[GlossaryHistoryRecord]:
        self.get_entry(entry_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM glossary_history WHERE entry_id = ? ORDER BY revision, created_at", (entry_id,)
            ).fetchall()
        return [GlossaryHistoryRecord(
            history_id=row["history_id"], entry_id=row["entry_id"], revision=row["revision"],
            action=row["action"], snapshot=GlossaryEntry.model_validate_json(row["snapshot_json"]),
            created_at=row["created_at"],
        ) for row in rows]

    def stats(self) -> GlossaryStats:
        self.initialize()
        with self._connection() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM glossary_entries").fetchone()[0])
            by_status = {row[0]: row[1] for row in connection.execute("SELECT status, COUNT(*) FROM glossary_entries GROUP BY status")}
            by_mode = {row[0]: row[1] for row in connection.execute("SELECT match_mode, COUNT(*) FROM glossary_entries GROUP BY match_mode")}
        return GlossaryStats(total=total, by_status=by_status, by_mode=by_mode, active_set_hash=self.active_set_hash())

    def active_set_hash(self) -> str:
        self.initialize()
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, revision, source_term, target_term, source_language, target_language,
                          domain, match_mode, case_sensitive
                   FROM glossary_entries WHERE status = 'approved' ORDER BY id"""
            ).fetchall()
        canonical = json.dumps([dict(row) for row in rows], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def find_same_source(self, row: GlossaryEntryCreate) -> list[GlossaryEntry]:
        entries, _ = self.list_entries(
            source_language=row.source_language,
            target_language=row.target_language,
            limit=1_000,
        )
        return [entry for entry in entries if _term_equal(entry.source_term, row.source_term, row.case_sensitive or entry.case_sensitive)]

    def record_usage(self, entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        self.initialize()
        timestamp = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    "UPDATE glossary_entries SET usage_count = usage_count + 1, last_used_at = ? WHERE id = ? AND status = 'approved'",
                    [(timestamp, entry_id) for entry_id in sorted(set(entry_ids))],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _get_in_connection(self, connection: sqlite3.Connection, entry_id: str) -> GlossaryEntry:
        row = connection.execute("SELECT * FROM glossary_entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise GlossaryNotFoundError("Glosarski vnos ne obstaja.")
        return _entry(row)

    def _append_history(self, connection: sqlite3.Connection, entry: GlossaryEntry, action: GlossaryAction) -> None:
        connection.execute(
            "INSERT INTO glossary_history(history_id, entry_id, revision, action, snapshot_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), entry.id, entry.revision, action, entry.model_dump_json(), _now()),
        )


def _entry(row: sqlite3.Row) -> GlossaryEntry:
    return GlossaryEntry(
        id=row["id"], source_term=row["source_term"], target_term=row["target_term"],
        source_language=row["source_language"], target_language=row["target_language"],
        domain=row["domain"], match_mode=row["match_mode"], case_sensitive=bool(row["case_sensitive"]),
        status=row["status"], origin=row["origin"], note=row["note"], source_label=row["source_label"],
        document_hash=row["document_hash"], revision=row["revision"], created_at=row["created_at"],
        updated_at=row["updated_at"], usage_count=row["usage_count"], last_used_at=row["last_used_at"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _term_equal(left: str, right: str, case_sensitive: bool) -> bool:
    return left == right if case_sensitive else left.casefold() == right.casefold()
