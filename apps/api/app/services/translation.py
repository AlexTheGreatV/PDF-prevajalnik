from __future__ import annotations

import importlib.util
import json
import threading
import time
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.translation_models import LocalTranslationStatus, TranslationError, TranslationTextResult
from app.translation_profiles import (
    DEFAULT_TRANSLATION_PROFILE,
    TokenizerProfile,
    TranslationModelProfile,
    get_model_profile,
    profile_for_model_id,
)

PROVIDER_ID = "local-opus-ctranslate2"
MODEL_ID = "Helsinki-NLP/opus-mt-en-sla"
TARGET_LANGUAGE_TOKEN = ">>slv<<"
BASE_MODEL_FILES = ("config.json", "model.bin", "manifest.json")


@dataclass(frozen=True)
class RuntimeModelSpec:
    profile: str
    model_id: str
    provider_id: str
    tokenizer: TokenizerProfile


@dataclass(frozen=True)
class RuntimeTranslation:
    translated_text: str | None
    source_token_count: int
    output_token_count: int
    max_decoding_length: int
    reached_decoding_limit: bool
    has_end_token: bool | None
    beam_size: int = 2
    raw_output_tokens: tuple[str, ...] = ()
    error_code: str | None = None


class TranslationRuntime(Protocol):
    def translate_batch(self, texts: list[str]) -> list[RuntimeTranslation | str | Exception]: ...


class LocalTranslationUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranslationInput:
    segment_id: str
    text: str


RuntimeFactory = Callable[[Path, str, str, RuntimeModelSpec], TranslationRuntime]
DependencyChecker = Callable[[str], bool]


class LocalCTranslate2TranslationProvider:
    def __init__(
        self,
        model_path: Path,
        device: str = "cpu",
        compute_type: str = "int8",
        profile_name: str = DEFAULT_TRANSLATION_PROFILE,
        runtime_factory: RuntimeFactory | None = None,
        dependency_checker: DependencyChecker | None = None,
    ) -> None:
        self.model_path = model_path
        self.device = device
        self.compute_type = compute_type
        self.profile_name = profile_name
        self._expected_profile = get_model_profile(profile_name)
        self._runtime_factory = runtime_factory or CTranslate2SentencePieceRuntime
        self._dependency_checker = dependency_checker or (lambda name: importlib.util.find_spec(name) is not None)
        self._runtime: TranslationRuntime | None = None
        self._load_error: str | None = None
        self._load_time_ms: int | None = None
        self._load_lock = threading.Lock()
        self._translation_lock = threading.Lock()

    def status(self) -> LocalTranslationStatus:
        spec, spec_error = self._safe_runtime_spec()
        missing = self._missing_requirements(spec, spec_error)
        displayed_spec = spec or runtime_spec_for_profile(self._expected_profile)
        message = None
        if missing:
            message = (
                f"Lokalni model {self.profile_name} ni pripravljen. Zaženite "
                f"prepare_local_translation_model.py --profile {self.profile_name} --download. "
                "Manjka: " + ", ".join(missing) + "."
            )
        elif self._load_error:
            message = self._load_error
        return LocalTranslationStatus(
            available=not missing and self._load_error is None,
            loaded=self._runtime is not None,
            provider_id=displayed_spec.provider_id,
            model_id=displayed_spec.model_id,
            profile_id=displayed_spec.profile,
            device=self.device,
            compute_type=self.compute_type,
            message=message,
            load_time_ms=self._load_time_ms,
        )

    def translate_batch(self, inputs: list[TranslationInput]) -> list[TranslationTextResult]:
        runtime = self._get_runtime()
        started = time.perf_counter()
        try:
            with self._translation_lock:
                outputs = runtime.translate_batch([item.text for item in inputs])
        except Exception as exc:
            raise LocalTranslationUnavailableError("Lokalni prevod ni uspel zaradi napake prevajalskega pogona.") from exc
        elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
        if len(outputs) != len(inputs):
            raise LocalTranslationUnavailableError("Prevajalski pogon ni vrnil rezultata za vsak segment.")
        per_item_ms = round(elapsed_ms / len(inputs)) if inputs else 0
        results: list[TranslationTextResult] = []
        for item, output in zip(inputs, outputs, strict=True):
            if isinstance(output, RuntimeTranslation):
                results.append(
                    TranslationTextResult(
                        segment_id=item.segment_id,
                        translated_text=output.translated_text,
                        error=TranslationError(code=output.error_code, message="Modelni del zahteva varnejše razbijanje.", retryable=True) if output.error_code else None,
                        processing_time_ms=per_item_ms,
                        source_token_count=output.source_token_count,
                        output_token_count=output.output_token_count,
                        max_decoding_length=output.max_decoding_length,
                        reached_decoding_limit=output.reached_decoding_limit,
                        has_end_token=output.has_end_token,
                        beam_size=output.beam_size,
                    )
                )
                continue
            if isinstance(output, Exception):
                results.append(
                    TranslationTextResult(
                        segment_id=item.segment_id,
                        error=TranslationError(
                            code="translation_failed",
                            message="Segmenta ni bilo mogoče prevesti.",
                            retryable=True,
                        ),
                        processing_time_ms=per_item_ms,
                    )
                )
            else:
                results.append(
                    TranslationTextResult(
                        segment_id=item.segment_id,
                        translated_text=output,
                        processing_time_ms=per_item_ms,
                    )
                )
        return results

    def _get_runtime(self) -> TranslationRuntime:
        if self._runtime is not None:
            return self._runtime
        with self._load_lock:
            if self._runtime is not None:
                return self._runtime
            spec, spec_error = self._safe_runtime_spec()
            missing = self._missing_requirements(spec, spec_error)
            if missing or spec is None:
                raise LocalTranslationUnavailableError("Lokalni prevajalnik ni nameščen ali model ni pripravljen.")
            started = time.perf_counter()
            try:
                self._runtime = self._runtime_factory(self.model_path, self.device, self.compute_type, spec)
            except Exception as exc:
                self._load_error = "Lokalnega prevajalskega modela ni bilo mogoče naložiti."
                raise LocalTranslationUnavailableError(self._load_error) from exc
            self._load_time_ms = max(0, round((time.perf_counter() - started) * 1000))
            return self._runtime

    def _safe_runtime_spec(self) -> tuple[RuntimeModelSpec | None, str | None]:
        try:
            return load_runtime_model_spec(self.model_path, self.profile_name), None
        except (OSError, ValueError, TypeError, KeyError):
            return None, "neveljaven modelni manifest"

    def _missing_requirements(self, spec: RuntimeModelSpec | None, spec_error: str | None) -> list[str]:
        missing = [name for name in ("ctranslate2", "sentencepiece") if not self._dependency_checker(name)]
        if not self.model_path.is_dir():
            missing.append("lokalni model")
            return missing
        if spec_error:
            missing.append(spec_error)
            return missing
        assert spec is not None
        required = (*BASE_MODEL_FILES, spec.tokenizer.source_file, spec.tokenizer.target_file)
        if missing_files := sorted({name for name in required if not (self.model_path / name).is_file()}):
            missing.append("modelne datoteke: " + ", ".join(missing_files))
        if not (self.model_path / "shared_vocabulary.json").is_file() and not all(
            (self.model_path / name).is_file()
            for name in ("source_vocabulary.json", "target_vocabulary.json")
        ):
            missing.append("modelno besedišče")
        return missing


class CTranslate2SentencePieceRuntime:
    MAX_SOURCE_TOKENS = 384
    MAX_DECODING_TOKENS = 768
    def __init__(self, model_path: Path, device: str, compute_type: str, spec: RuntimeModelSpec) -> None:
        import ctranslate2
        import sentencepiece

        self._spec = spec
        self._source = sentencepiece.SentencePieceProcessor(model_file=str(model_path / spec.tokenizer.source_file))
        self._target = sentencepiece.SentencePieceProcessor(model_file=str(model_path / spec.tokenizer.target_file))
        self._translator = ctranslate2.Translator(
            str(model_path),
            device=device,
            compute_type=compute_type,
            inter_threads=1,
            max_queued_batches=1,
        )

    def translate_batch(self, texts: list[str]) -> list[RuntimeTranslation | Exception]:
        tokenized = [encode_sentencepiece_source(self._source, text, self._spec.tokenizer) for text in texts]
        outputs: list[RuntimeTranslation | Exception | None] = [None] * len(texts)
        valid = [(index, tokens) for index, tokens in enumerate(tokenized) if len(tokens) <= self.MAX_SOURCE_TOKENS]
        for index, tokens in enumerate(tokenized):
            if len(tokens) > self.MAX_SOURCE_TOKENS:
                outputs[index] = RuntimeTranslation(None, len(tokens), 0, 0, False, None, error_code="source_token_limit")
        if not valid:
            return [output for output in outputs if output is not None]
        max_decoding_length = decoding_length_for_source_tokens(max(len(tokens) for _, tokens in valid))
        translated = self._translator.translate_batch(
            [tokens for _, tokens in valid],
            beam_size=2,
            max_batch_size=32,
            max_input_length=0,
            max_decoding_length=max_decoding_length,
            return_end_token=True,
        )
        eos_id = self._target.eos_id()
        eos_piece = self._target.id_to_piece(eos_id) if eos_id >= 0 else None
        for (index, source_tokens), result in zip(valid, translated, strict=True):
            raw_tokens = tuple(result.hypotheses[0])
            has_end_token = bool(eos_piece and raw_tokens and raw_tokens[-1] == eos_piece)
            decoded_tokens = raw_tokens[:-1] if has_end_token else raw_tokens
            reached_limit = len(raw_tokens) >= max_decoding_length and not has_end_token
            outputs[index] = RuntimeTranslation(
                None if reached_limit else self._target.decode(list(decoded_tokens)),
                len(source_tokens),
                len(raw_tokens),
                max_decoding_length,
                reached_limit,
                has_end_token,
                raw_output_tokens=raw_tokens,
                error_code="decoding_limit" if reached_limit else None,
            )
        return [output if output is not None else RuntimeError("Manjkajoč modelni rezultat.") for output in outputs]


def decoding_length_for_source_tokens(source_token_count: int) -> int:
    return min(CTranslate2SentencePieceRuntime.MAX_DECODING_TOKENS, max(64, math.ceil(source_token_count * 2.0) + 32))


def load_runtime_model_spec(model_path: Path, expected_profile_name: str | None = None) -> RuntimeModelSpec:
    manifest = json.loads((model_path / "manifest.json").read_text(encoding="utf-8"))
    model_id = manifest.get("model_id")
    if not isinstance(model_id, str):
        raise ValueError("Manifest nima model_id.")
    profile_name = manifest.get("profile")
    profile = get_model_profile(profile_name) if isinstance(profile_name, str) else profile_for_model_id(model_id)
    if profile is None:
        raise ValueError("Manifest uporablja neznan model.")
    if model_id != profile.model_id:
        raise ValueError("Model ID se ne ujema z izbranim profilom.")
    if expected_profile_name is not None and profile.name != expected_profile_name:
        raise ValueError("Modelni profil se ne ujema s konfiguracijo.")
    tokenizer_data = manifest.get("tokenizer")
    if tokenizer_data is None:
        tokenizer = profile.tokenizer
    elif isinstance(tokenizer_data, dict):
        tokenizer = TokenizerProfile(
            source_file=_safe_relative_file(tokenizer_data.get("source_file")),
            target_file=_safe_relative_file(tokenizer_data.get("target_file")),
            source_prefix=tuple(_string_list(tokenizer_data.get("source_prefix", []))),
            append_eos=bool(tokenizer_data.get("append_eos", False)),
        )
    else:
        raise ValueError("Manifest ima neveljaven tokenizer.")
    return RuntimeModelSpec(
        profile=profile.name,
        model_id=model_id,
        provider_id=str(manifest.get("provider_id") or (PROVIDER_ID if profile.name == "opus-en-sla" else "local-hplt-ctranslate2")),
        tokenizer=tokenizer,
    )


def runtime_spec_for_profile(profile: TranslationModelProfile) -> RuntimeModelSpec:
    return RuntimeModelSpec(
        profile=profile.name,
        model_id=profile.model_id,
        provider_id=PROVIDER_ID if profile.name == "opus-en-sla" else "local-hplt-ctranslate2",
        tokenizer=profile.tokenizer,
    )


def encode_sentencepiece_source(source_processor: object, text: str, tokenizer: TokenizerProfile) -> list[str]:
    encode = getattr(source_processor, "encode")
    tokens = [*tokenizer.source_prefix, *encode(text, out_type=str)]
    if tokenizer.append_eos:
        tokens.append("</s>")
    return tokens


def encode_marian_source(source_processor: object, text: str, target_language_token: str) -> list[str]:
    return encode_sentencepiece_source(
        source_processor,
        text,
        TokenizerProfile(
            source_file="source.spm",
            target_file="target.spm",
            source_prefix=(target_language_token,),
            append_eos=True,
        ),
    )


def _safe_relative_file(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Tokenizer nima veljavne poti.")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Tokenizer pot mora biti relativna znotraj modela.")
    return path.as_posix()


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Tokenizer predpona mora biti seznam nizov.")
    return value


LocalOpusTranslationProvider = LocalCTranslate2TranslationProvider
REQUIRED_MODEL_FILES = (*BASE_MODEL_FILES, "source.spm", "target.spm")
