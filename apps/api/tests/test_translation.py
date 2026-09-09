import importlib.util
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.translation import get_local_translation_provider
from app.core.config import settings
from app.main import app
from app.services.translation import (
    MODEL_ID,
    REQUIRED_MODEL_FILES,
    TARGET_LANGUAGE_TOKEN,
    LocalOpusTranslationProvider,
    LocalTranslationUnavailableError,
    TranslationInput,
    encode_marian_source,
)


class FakeRuntime:
    def __init__(self, outputs: list[str | Exception] | None = None) -> None:
        self.outputs = outputs
        self.calls: list[tuple[list[str], str]] = []

    def translate_batch(self, texts: list[str], target_language_token: str) -> list[str | Exception]:
        self.calls.append((texts, target_language_token))
        return self.outputs if self.outputs is not None else [f"SL:{text}" for text in texts]


def prepared_model(tmp_path: Path) -> Path:
    model_path = tmp_path / "model"
    model_path.mkdir()
    for name in REQUIRED_MODEL_FILES:
        (model_path / name).write_text("{}" if name.endswith(".json") else "test", encoding="utf-8")
    (model_path / "shared_vocabulary.json").write_text("{}", encoding="utf-8")
    return model_path


def provider_with_runtime(tmp_path: Path, runtime: FakeRuntime) -> LocalOpusTranslationProvider:
    return LocalOpusTranslationProvider(
        prepared_model(tmp_path),
        runtime_factory=lambda _path, _device, _compute: runtime,
        dependency_checker=lambda _name: True,
    )


def test_provider_preserves_segment_ids_order_and_adds_target_tag(tmp_path: Path) -> None:
    runtime = FakeRuntime(outputs=["Prvi", "Drugi"])
    provider = provider_with_runtime(tmp_path, runtime)

    results = provider.translate_batch([
        TranslationInput(segment_id="a", text="First"),
        TranslationInput(segment_id="b", text="Second"),
    ])

    assert [result.segment_id for result in results] == ["a", "b"]
    assert [result.translated_text for result in results] == ["Prvi", "Drugi"]
    assert runtime.calls == [(["First", "Second"], TARGET_LANGUAGE_TOKEN)]


def test_marian_source_tokens_include_target_tag_and_eos() -> None:
    class FakeSentencePiece:
        def encode(self, text: str, out_type: type) -> list[str]:
            assert text == "Product description"
            assert out_type is str
            return ["▁Product", "▁description"]

    assert encode_marian_source(FakeSentencePiece(), "Product description", TARGET_LANGUAGE_TOKEN) == [
        TARGET_LANGUAGE_TOKEN,
        "▁Product",
        "▁description",
        "</s>",
    ]


def test_provider_loads_runtime_once_for_multiple_batches(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    loads = 0

    def factory(_path: Path, _device: str, _compute: str) -> FakeRuntime:
        nonlocal loads
        loads += 1
        return runtime

    provider = LocalOpusTranslationProvider(
        prepared_model(tmp_path), runtime_factory=factory, dependency_checker=lambda _name: True
    )
    provider.translate_batch([TranslationInput(segment_id="a", text="One")])
    provider.translate_batch([TranslationInput(segment_id="b", text="Two")])

    assert loads == 1
    assert provider.status().loaded is True


def test_missing_model_returns_safe_unavailable_status(tmp_path: Path) -> None:
    provider = LocalOpusTranslationProvider(tmp_path / "private" / "missing", dependency_checker=lambda _name: True)

    status = provider.status()

    assert status.available is False
    assert status.loaded is False
    assert str(tmp_path) not in (status.message or "")


def test_load_error_is_controlled_and_path_is_not_exposed(tmp_path: Path) -> None:
    def fail(_path: Path, _device: str, _compute: str) -> FakeRuntime:
        raise RuntimeError(f"secret path: {tmp_path}")

    provider = LocalOpusTranslationProvider(
        prepared_model(tmp_path), runtime_factory=fail, dependency_checker=lambda _name: True
    )

    with pytest.raises(LocalTranslationUnavailableError, match="ni bilo mogoče naložiti"):
        provider.translate_batch([TranslationInput(segment_id="a", text="Text")])
    assert str(tmp_path) not in (provider.status().message or "")


def test_partial_error_and_placeholder_text_remain_structured(tmp_path: Path) -> None:
    runtime = FakeRuntime(outputs=["Ohrani TK000TK", RuntimeError("private failure")])
    provider = provider_with_runtime(tmp_path, runtime)

    results = provider.translate_batch([
        TranslationInput(segment_id="ok", text="Keep TK000TK"),
        TranslationInput(segment_id="bad", text="Bad"),
    ])

    assert results[0].translated_text == "Ohrani TK000TK"
    assert results[1].error is not None
    assert results[1].error.code == "translation_failed"
    assert "private failure" not in results[1].error.message


def test_status_and_translate_endpoints(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runtime = FakeRuntime(outputs=["Prevod"])
    provider = provider_with_runtime(tmp_path, runtime)
    app.dependency_overrides[get_local_translation_provider] = lambda: provider
    try:
        with TestClient(app) as client, caplog.at_level(logging.INFO):
            status = client.get("/api/translation/providers/local/status")
            response = client.post(
                "/api/translation/providers/local/translate",
                json={
                    "source_language": "en",
                    "target_language": "sl",
                    "segments": [{"segment_id": "segment-1", "text": "CONFIDENTIAL SOURCE TEXT"}],
                },
            )
        assert status.status_code == 200
        assert status.json()["available"] is True
        assert response.status_code == 200
        assert response.json()["model_id"] == MODEL_ID
        assert response.json()["results"][0]["segment_id"] == "segment-1"
        assert "CONFIDENTIAL SOURCE TEXT" not in caplog.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"source_language": "de", "target_language": "sl", "segments": [{"segment_id": "a", "text": "Text"}]}, 422),
        ({"source_language": "en", "target_language": "de", "segments": [{"segment_id": "a", "text": "Text"}]}, 422),
        ({"source_language": "en", "target_language": "sl", "segments": [{"segment_id": str(index), "text": "Text"} for index in range(33)]}, 422),
        ({"source_language": "en", "target_language": "sl", "segments": [{"segment_id": "a", "text": "x" * 5001}]}, 422),
    ],
)
def test_translate_endpoint_validates_languages_and_limits(payload: dict, expected_status: int) -> None:
    with TestClient(app) as client:
        assert client.post("/api/translation/providers/local/translate", json=payload).status_code == expected_status


def test_translate_endpoint_returns_unavailable_without_model(tmp_path: Path) -> None:
    provider = LocalOpusTranslationProvider(tmp_path / "missing", dependency_checker=lambda _name: True)
    app.dependency_overrides[get_local_translation_provider] = lambda: provider
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/translation/providers/local/translate",
                json={"source_language": "en", "target_language": "sl", "segments": [{"segment_id": "a", "text": "Text"}]},
            )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "provider_unavailable"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.skipif(
    not settings.local_translation_model_path.is_dir()
    or importlib.util.find_spec("ctranslate2") is None
    or importlib.util.find_spec("sentencepiece") is None,
    reason="Prepared local OPUS-MT model and optional runtime dependencies are required.",
)
def test_real_local_model_integration() -> None:
    provider = LocalOpusTranslationProvider(
        settings.local_translation_model_path,
        device=settings.local_translation_device,
        compute_type=settings.local_translation_compute_type,
    )

    result = provider.translate_batch([TranslationInput(segment_id="integration", text="Product description")])[0]

    assert result.translated_text
    assert result.error is None
