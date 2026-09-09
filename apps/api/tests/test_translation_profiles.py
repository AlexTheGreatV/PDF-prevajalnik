import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from app.services.translation import LocalCTranslate2TranslationProvider, load_runtime_model_spec
from app.translation_profiles import MODEL_PROFILES, get_model_profile

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def load_script(name: str):
    path = REPOSITORY_ROOT / "apps" / "api" / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profiles_are_pinned_and_keep_default_opus_output() -> None:
    opus = get_model_profile("opus-en-sla")
    hplt = get_model_profile("hplt-en-sl-v2")
    assert opus.output_dir == "opus-mt-en-sla-ct2-int8"
    assert opus.model_id == "Helsinki-NLP/opus-mt-en-sla"
    assert hplt.model_id == "HPLT/translate-en-sl-v2.0-hplt"
    assert hplt.converter == "marian"
    assert hplt.license == "CC-BY-4.0"
    assert all(len(profile.revision) == 40 for profile in MODEL_PROFILES.values())
    assert hplt.expected_sha256["model.npz.best-chrf.npz"] == "e2a9d7f680e6fbd9de1bf909ed6fae282d8e96418430b10d2eb9f7c3edb90fe6"


def test_hplt_manifest_selects_shared_tokenizer_without_prefix(tmp_path: Path) -> None:
    model = tmp_path / "hplt"
    model.mkdir()
    (model / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": "hplt-en-sl-v2",
                "model_id": "HPLT/translate-en-sl-v2.0-hplt",
                "tokenizer": {
                    "source_file": "model.en-sl.spm",
                    "target_file": "model.en-sl.spm",
                    "source_prefix": [],
                    "append_eos": False,
                },
            }
        ),
        encoding="utf-8",
    )
    runtime = load_runtime_model_spec(model)
    assert runtime.profile == "hplt-en-sl-v2"
    assert runtime.tokenizer.source_file == runtime.tokenizer.target_file
    assert runtime.tokenizer.source_prefix == ()
    assert runtime.tokenizer.append_eos is False


def test_manifest_rejects_tokenizer_escape(tmp_path: Path) -> None:
    model = tmp_path / "unsafe"
    model.mkdir()
    (model / "manifest.json").write_text(
        json.dumps(
            {
                "profile": "hplt-en-sl-v2",
                "model_id": "HPLT/translate-en-sl-v2.0-hplt",
                "tokenizer": {
                    "source_file": "../outside.spm",
                    "target_file": "model.en-sl.spm",
                    "source_prefix": [],
                    "append_eos": False,
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="relativna"):
        load_runtime_model_spec(model)


def test_manifest_rejects_model_id_that_does_not_match_profile(tmp_path: Path) -> None:
    model = tmp_path / "mismatch"
    model.mkdir()
    (model / "manifest.json").write_text(
        json.dumps(
            {
                "profile": "hplt-en-sl-v2",
                "model_id": "untrusted/other-model",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ne ujema"):
        load_runtime_model_spec(model)


def test_missing_profile_model_is_safe_and_does_not_expose_path(tmp_path: Path) -> None:
    provider = LocalCTranslate2TranslationProvider(
        tmp_path / "private" / "missing", dependency_checker=lambda _name: True
    )
    status = provider.status()
    assert status.available is False
    assert str(tmp_path) not in (status.message or "")


def test_preparation_and_comparison_scripts_do_not_download_on_import() -> None:
    preparation = load_script("prepare_local_translation_model.py")
    comparison = load_script("compare_local_translation_models.py")
    assert "snapshot_download" not in inspect.getsource(preparation).split("def main", 1)[0]
    assert "huggingface_hub" not in inspect.getsource(comparison)


def test_comparison_preserves_placeholder_failures_as_diagnostics() -> None:
    comparison = load_script("compare_local_translation_models.py")
    tokens = [{"placeholder": "TK000TK", "value": "20 °C"}]
    valid = comparison.placeholder_diagnostics("Temperatura TK000TK", tokens)
    invalid = comparison.placeholder_diagnostics("Temperatura TKOOOTK", tokens)
    assert valid["valid"] is True
    assert invalid["valid"] is False
    assert invalid["missing"] == ["TK000TK"]


def test_comparison_memory_probe_returns_a_positive_value_on_windows() -> None:
    comparison = load_script("compare_local_translation_models.py")
    value = comparison.peak_working_set_bytes()
    assert value is None or value > 0


def test_comparison_timing_summary_reports_minimum_median_and_maximum() -> None:
    comparison = load_script("compare_local_translation_models.py")
    assert comparison.MEASURED_REPETITIONS >= 5
    assert comparison.timing_summary([900, 500, 700, 600, 800]) == {
        "minimum": 500,
        "median": 700,
        "maximum": 900,
    }


def test_local_models_and_comparison_outputs_are_ignored() -> None:
    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "local-models/" in gitignore
    assert "tmp/" in gitignore
