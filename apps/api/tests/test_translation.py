import inspect
import importlib.util
import json
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.translation import get_local_translation_provider
from app.main import app
from app.services.translation import (
    MODEL_ID,
    REQUIRED_MODEL_FILES,
    TARGET_LANGUAGE_TOKEN,
    LocalCTranslate2TranslationProvider,
    LocalOpusTranslationProvider,
    LocalTranslationUnavailableError,
    TranslationInput,
    RuntimeTranslation,
    decoding_length_for_source_tokens,
    encode_marian_source,
    encode_sentencepiece_source,
    load_runtime_model_spec,
)
from app.translation_profiles import MODEL_PROFILES, get_model_profile


class FakeRuntime:
    def __init__(self, outputs: list[str | Exception] | None = None) -> None:
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def translate_batch(self, texts: list[str]) -> list[str | Exception]:
        self.calls.append(texts)
        return self.outputs if self.outputs is not None else [f"SL:{text}" for text in texts]


def prepared_model(tmp_path: Path) -> Path:
    model_path = tmp_path / "model"
    model_path.mkdir()
    manifest = {"schema_version": 1, "model_id": MODEL_ID}
    for name in REQUIRED_MODEL_FILES:
        (model_path / name).write_text(json.dumps(manifest) if name == "manifest.json" else "test", encoding="utf-8")
    (model_path / "shared_vocabulary.json").write_text("{}", encoding="utf-8")
    return model_path


def provider_with_runtime(tmp_path: Path, runtime: FakeRuntime) -> LocalOpusTranslationProvider:
    return LocalOpusTranslationProvider(
        prepared_model(tmp_path),
        profile_name="opus-en-sla",
        runtime_factory=lambda _path, _device, _compute, _spec: runtime,
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
    assert runtime.calls == [["First", "Second"]]


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


def test_profiles_select_distinct_tokenizer_rules() -> None:
    class FakeSentencePiece:
        def encode(self, text: str, out_type: type) -> list[str]:
            assert out_type is str
            return [f"▁{text}"]

    opus = get_model_profile("opus-en-sla")
    hplt = get_model_profile("hplt-en-sl-v2")
    assert encode_sentencepiece_source(FakeSentencePiece(), "Text", opus.tokenizer) == [">>slv<<", "▁Text", "</s>"]
    assert encode_sentencepiece_source(FakeSentencePiece(), "Text", hplt.tokenizer) == ["▁Text"]
    assert hplt.tokenizer.source_file == hplt.tokenizer.target_file


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="Neznan modelni profil"):
        get_model_profile("unknown")


def test_profiles_pin_official_model_revisions_and_manifests(tmp_path: Path) -> None:
    for profile in MODEL_PROFILES.values():
        assert len(profile.revision) == 40
        int(profile.revision, 16)
        assert profile.source_url.startswith("https://huggingface.co/")
    model_path = prepared_model(tmp_path)
    spec = load_runtime_model_spec(model_path)
    assert spec.profile == "opus-en-sla"
    assert spec.tokenizer.source_prefix == (TARGET_LANGUAGE_TOKEN,)


def test_runtime_has_no_model_download_client() -> None:
    source = inspect.getsource(LocalCTranslate2TranslationProvider)
    assert "huggingface" not in source.lower()
    assert "snapshot_download" not in source


def test_provider_loads_runtime_once_for_multiple_batches(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    loads = 0

    def factory(_path: Path, _device: str, _compute: str) -> FakeRuntime:
        nonlocal loads
        loads += 1
        return runtime

    provider = LocalOpusTranslationProvider(
        prepared_model(tmp_path), profile_name="opus-en-sla", runtime_factory=lambda path, device, compute, _spec: factory(path, device, compute), dependency_checker=lambda _name: True
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


def test_missing_default_hplt_model_returns_preparation_instruction(tmp_path: Path) -> None:
    provider = LocalCTranslate2TranslationProvider(
        tmp_path / "missing-hplt",
        profile_name="hplt-en-sl-v2",
        dependency_checker=lambda _name: True,
    )

    status = provider.status()

    assert status.available is False
    assert status.profile_id == "hplt-en-sl-v2"
    assert status.model_id == "HPLT/translate-en-sl-v2.0-hplt"
    assert "--profile hplt-en-sl-v2 --download" in (status.message or "")
    assert str(tmp_path) not in (status.message or "")


def test_load_error_is_controlled_and_path_is_not_exposed(tmp_path: Path) -> None:
    def fail(_path: Path, _device: str, _compute: str, _spec: object) -> FakeRuntime:
        raise RuntimeError(f"secret path: {tmp_path}")

    provider = LocalOpusTranslationProvider(
        prepared_model(tmp_path), profile_name="opus-en-sla", runtime_factory=fail, dependency_checker=lambda _name: True
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


def test_runtime_metadata_and_dynamic_decoding_limit_are_exposed(tmp_path: Path) -> None:
    runtime = FakeRuntime(outputs=[RuntimeTranslation("Prevod", 40, 22, 112, False, True, raw_output_tokens=("▁Prevod", "</s>"))])
    result = provider_with_runtime(tmp_path, runtime).translate_batch([TranslationInput(segment_id="chunk", text="Source")])[0]
    assert result.source_token_count == 40
    assert result.output_token_count == 22
    assert result.max_decoding_length == 112
    assert result.has_end_token is True
    assert decoding_length_for_source_tokens(16) == 64
    assert decoding_length_for_source_tokens(400) == 768


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
        assert status.json()["profile_id"] == "opus-en-sla"
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


RUN_LOCAL_MODEL_INTEGRATION = os.getenv("RUN_LOCAL_MODEL_INTEGRATION") == "1"


OPUS_MODEL_PATH = Path("local-models/opus-mt-en-sla-ct2-int8")


@pytest.mark.skipif(
    not RUN_LOCAL_MODEL_INTEGRATION
    or not OPUS_MODEL_PATH.is_dir()
    or importlib.util.find_spec("ctranslate2") is None
    or importlib.util.find_spec("sentencepiece") is None,
    reason="Prepared local OPUS-MT model and optional runtime dependencies are required.",
)
def test_real_local_model_integration() -> None:
    provider = LocalOpusTranslationProvider(OPUS_MODEL_PATH, profile_name="opus-en-sla", device="cpu", compute_type="int8")

    result = provider.translate_batch([TranslationInput(segment_id="integration", text="Product description")])[0]

    assert result.translated_text
    assert result.error is None
    assert provider.status().model_id == "Helsinki-NLP/opus-mt-en-sla"


HPLT_MODEL_PATH = Path("local-models/hplt-en-sl-v2-ct2-int8")


@pytest.mark.skipif(
    not RUN_LOCAL_MODEL_INTEGRATION
    or not HPLT_MODEL_PATH.is_dir()
    or importlib.util.find_spec("ctranslate2") is None
    or importlib.util.find_spec("sentencepiece") is None,
    reason="Prepared local HPLT model and optional runtime dependencies are required.",
)
def test_real_hplt_model_integration() -> None:
    provider = LocalCTranslate2TranslationProvider(HPLT_MODEL_PATH, profile_name="hplt-en-sl-v2", device="cpu", compute_type="int8")

    result = provider.translate_batch([TranslationInput(segment_id="integration-hplt", text="Product description")])[0]

    assert result.translated_text
    assert result.error is None
    assert provider.status().model_id == "HPLT/translate-en-sl-v2.0-hplt"


def test_bounded_terminology_retry_preserves_ordinary_decoding(tmp_path: Path) -> None:
    class RetryRuntime(FakeRuntime):
        def translate_batch(self, texts, *, num_hypotheses=1):
            self.calls.append((texts, num_hypotheses))
            return [RuntimeTranslation("Obloga", 1, 1, 32, False, True, beam_size=4,
                                       alternative_translations=("Obloga", "Premaz")) for _ in texts]
    runtime = RetryRuntime()
    provider = provider_with_runtime(tmp_path, runtime)
    results = provider.translate_batch([TranslationInput("a", "Coating", terminology_retry=True),
                                       TranslationInput("b", "Ordinary")])
    assert runtime.calls == [(["Coating"], 4), (["Ordinary"], 1)]
    assert results[0].alternative_translations == ["Obloga", "Premaz"]


def test_terminology_retry_request_is_bounded() -> None:
    from app.translation_models import TranslationBatchRequest
    from pydantic import ValidationError
    context = {"mode": "preferred_terms", "source_segment_id": "a", "domain": "technical_datasheets",
               "preferred_terms": [{"source_term": "coating", "target_term": "premaz"}]}
    payload = {"source_language": "en", "target_language": "sl", "segments": [{"segment_id": "a", "text": "Coating", "context": context}]}
    assert TranslationBatchRequest.model_validate(payload).segments[0].context.domain == "technical_datasheets"
    context["preferred_terms"] *= 9
    with pytest.raises(ValidationError):
        TranslationBatchRequest.model_validate(payload)


def test_retry_endpoint_propagates_context_and_alternatives(tmp_path: Path) -> None:
    class RetryRuntime(FakeRuntime):
        def translate_batch(self, texts, *, num_hypotheses=1):
            self.calls.append(num_hypotheses)
            return [RuntimeTranslation("Obloga", 1, 1, 32, False, True, beam_size=4,
                                       alternative_translations=("Obloga", "Premaz"))]
    runtime = RetryRuntime()
    app.dependency_overrides[get_local_translation_provider] = lambda: provider_with_runtime(tmp_path, runtime)
    try:
        with TestClient(app) as client:
            response = client.post("/api/translation/providers/local/translate", json={
                "source_language": "en", "target_language": "sl", "segments": [{"segment_id": "a", "text": "Coating",
                "context": {"mode": "preferred_terms", "source_segment_id": "full-a", "domain": "technical_datasheets",
                            "preferred_terms": [{"source_term": "coating", "target_term": "premaz"}]}}]})
        assert response.status_code == 200
        assert runtime.calls == [4]
        assert response.json()["results"][0]["alternative_translations"] == ["Obloga", "Premaz"]
    finally:
        app.dependency_overrides.clear()


def test_retry_never_exposes_truncated_alternative() -> None:
    from types import SimpleNamespace
    from app.services.translation import CTranslate2SentencePieceRuntime, runtime_spec_for_profile
    class Piece:
        def encode(self, text, out_type): return ["source"]
        def eos_id(self): return 1
        def id_to_piece(self, value): return "EOS"
        def decode(self, tokens): return " ".join(tokens)
    class Translator:
        def translate_batch(self, texts, **kwargs):
            assert kwargs["beam_size"] == 4 and kwargs["num_hypotheses"] == 4
            return [SimpleNamespace(hypotheses=[["good", "EOS"], ["truncated"] * kwargs["max_decoding_length"], ["better", "EOS"]])]
    runtime = object.__new__(CTranslate2SentencePieceRuntime)
    runtime._spec = runtime_spec_for_profile(get_model_profile("hplt-en-sl-v2"))
    runtime._source = runtime._target = Piece()
    runtime._translator = Translator()
    assert runtime.translate_batch(["Source"], num_hypotheses=4)[0].alternative_translations == ("good", "better")
