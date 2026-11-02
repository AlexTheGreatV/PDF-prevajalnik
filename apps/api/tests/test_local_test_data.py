import importlib.util
from pathlib import Path
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location("local_test_data", Path(__file__).resolve().parents[1] / "scripts/local_test_data.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_profile_configuration_ignores_inherited_ocr_and_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_DATABASE_PATH", str(tmp_path / "wrong-profile.sqlite3"))
    first = module.profile_configuration(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert module.profile_configuration(tmp_path) == first
    assert first["profile"] == "fresh-user-test"
    assert Path(first["ocr_database_path"]).parent == tmp_path / "local-data/profiles/fresh-user-test"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("with_ocr", [False, True])
def test_backup_uses_sqlite_api_and_includes_ocr_without_creating_missing_source(tmp_path, with_ocr):
    config = module.profile_configuration(tmp_path)
    profile = Path(config["glossary_database_path"]).parent
    profile.mkdir(parents=True)
    for key in ["glossary_database_path", "translation_memory_database_path"] + (["ocr_database_path"] if with_ocr else []):
        with sqlite3.connect(config[key]) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO evidence VALUES(1,'unchanged')")
    result = module.backup_profile(tmp_path)
    assert result["profile"] == "fresh-user-test"
    for data in result["databases"].values():
        if data["present"]:
            with sqlite3.connect(data["backup"]) as db:
                assert db.execute("SELECT * FROM evidence").fetchall() == [(1, "unchanged")]
            assert data["integrity_check"] == "ok" and data["foreign_key_check"] == []
    assert result["databases"]["ocr-acceptance"]["present"] == with_ocr
    assert Path(config["ocr_database_path"]).exists() == with_ocr
