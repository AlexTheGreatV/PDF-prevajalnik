"""Resolve and back up the existing fresh-user-test collection, never production."""
import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/api"))
from app.core.config import Settings


def profile_configuration(root: Path = ROOT) -> dict[str, str]:
    directory = root.resolve() / "local-data/profiles/fresh-user-test"
    config = Settings(_env_file=None, local_data_profile="fresh-user-test",
        glossary_database_path=directory / "glossary.sqlite3",
        translation_memory_database_path=directory / "translation-memory.sqlite3", ocr_database_path=None)
    return {"profile": config.local_data_profile, "glossary_database_path": str(config.glossary_database_path),
        "translation_memory_database_path": str(config.translation_memory_database_path),
        "ocr_database_path": str(config.resolved_ocr_database_path)}


def backup_profile(root: Path = ROOT) -> dict:
    config = profile_configuration(root)
    directory = (root.resolve() / "local-data/profiles/fresh-user-test").resolve()
    sources = {name: Path(config[key]) for name, key in (("glossary", "glossary_database_path"),
        ("translation-memory", "translation_memory_database_path"), ("ocr-acceptance", "ocr_database_path"))}
    for name, path in sources.items():
        if path.resolve().parent != directory:
            raise ValueError("Backup source escapes fresh-user-test profile")
        if not path.is_file() and name != "ocr-acceptance":
            raise FileNotFoundError(path)
    destination = directory / "backups" / ("profile-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f"))
    destination.mkdir(parents=True, exist_ok=False)
    snapshots = {}
    for name, path in sources.items():
        if not path.is_file():
            snapshots[name] = {"source": str(path), "present": False, "reason": "No durable OCR acceptance database exists yet; not created."}
            continue
        target = destination / path.name
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(target) as copy:
            source.backup(copy)
            integrity = copy.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = copy.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != [("ok",)] or foreign_keys:
                raise ValueError(f"Invalid SQLite backup: {name}")
            version = copy.execute("PRAGMA user_version").fetchone()[0]
            schema = copy.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        snapshots[name] = {"source": str(path), "backup": str(target), "present": True,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "integrity_check": "ok",
            "foreign_key_check": [], "user_version": version, "schema": schema}
    manifest = {"profile": config["profile"], "method": "SQLite backup API; consistent per database",
        "created_at": datetime.now(UTC).isoformat(), "directory": str(destination), "databases": snapshots}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", action="store_true")
    args = parser.parse_args()
    print(json.dumps(backup_profile() if args.backup else profile_configuration(), indent=2))
