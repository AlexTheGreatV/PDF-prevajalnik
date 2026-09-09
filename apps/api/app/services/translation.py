from __future__ import annotations

import importlib.util
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.translation_models import LocalTranslationStatus, TranslationError, TranslationTextResult

PROVIDER_ID = "local-opus-ctranslate2"
MODEL_ID = "Helsinki-NLP/opus-mt-en-sla"
TARGET_LANGUAGE_TOKEN = ">>slv<<"
REQUIRED_MODEL_FILES = (
    "config.json",
    "model.bin",
    "source.spm",
    "target.spm",
    "manifest.json",
)


class TranslationRuntime(Protocol):
    def translate_batch(self, texts: list[str], target_language_token: str) -> list[str | Exception]: ...


class LocalTranslationUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranslationInput:
    segment_id: str
    text: str


RuntimeFactory = Callable[[Path, str, str], TranslationRuntime]
DependencyChecker = Callable[[str], bool]


class LocalOpusTranslationProvider:
    def __init__(
        self,
        model_path: Path,
        device: str = "cpu",
        compute_type: str = "int8",
        runtime_factory: RuntimeFactory | None = None,
        dependency_checker: DependencyChecker | None = None,
    ) -> None:
        self.model_path = model_path
        self.device = device
        self.compute_type = compute_type
        self._runtime_factory = runtime_factory or CTranslate2SentencePieceRuntime
        self._dependency_checker = dependency_checker or (lambda name: importlib.util.find_spec(name) is not None)
        self._runtime: TranslationRuntime | None = None
        self._load_error: str | None = None
        self._load_time_ms: int | None = None
        self._load_lock = threading.Lock()
        self._translation_lock = threading.Lock()

    def status(self) -> LocalTranslationStatus:
        missing = self._missing_requirements()
        message = None
        if missing:
            message = "Lokalni prevajalnik ni nameščen: manjkajo " + ", ".join(missing) + "."
        elif self._load_error:
            message = self._load_error
        return LocalTranslationStatus(
            available=not missing and self._load_error is None,
            loaded=self._runtime is not None,
            provider_id=PROVIDER_ID,
            model_id=self._manifest_model_id(),
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
                outputs = runtime.translate_batch([item.text for item in inputs], TARGET_LANGUAGE_TOKEN)
        except Exception as exc:
            raise LocalTranslationUnavailableError("Lokalni prevod ni uspel zaradi napake prevajalskega pogona.") from exc
        elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
        if len(outputs) != len(inputs):
            raise LocalTranslationUnavailableError("Prevajalski pogon ni vrnil rezultata za vsak segment.")
        per_item_ms = round(elapsed_ms / len(inputs)) if inputs else 0
        results: list[TranslationTextResult] = []
        for item, output in zip(inputs, outputs, strict=True):
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
            missing = self._missing_requirements()
            if missing:
                raise LocalTranslationUnavailableError("Lokalni prevajalnik ni nameščen ali model ni pripravljen.")
            started = time.perf_counter()
            try:
                self._runtime = self._runtime_factory(self.model_path, self.device, self.compute_type)
            except Exception as exc:
                self._load_error = "Lokalnega prevajalskega modela ni bilo mogoče naložiti."
                raise LocalTranslationUnavailableError(self._load_error) from exc
            self._load_time_ms = max(0, round((time.perf_counter() - started) * 1000))
            return self._runtime

    def _missing_requirements(self) -> list[str]:
        missing = [name for name in ("ctranslate2", "sentencepiece") if not self._dependency_checker(name)]
        if not self.model_path.is_dir():
            missing.append("lokalni model")
        elif missing_files := [name for name in REQUIRED_MODEL_FILES if not (self.model_path / name).is_file()]:
            missing.append("modelne datoteke: " + ", ".join(missing_files))
        elif not (self.model_path / "shared_vocabulary.json").is_file() and not all(
            (self.model_path / name).is_file()
            for name in ("source_vocabulary.json", "target_vocabulary.json")
        ):
            missing.append("modelno besedišče")
        return missing

    def _manifest_model_id(self) -> str:
        manifest_path = self.model_path / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            value = manifest.get("model_id")
            return value if isinstance(value, str) else MODEL_ID
        except (OSError, ValueError):
            return MODEL_ID


class CTranslate2SentencePieceRuntime:
    def __init__(self, model_path: Path, device: str, compute_type: str) -> None:
        import ctranslate2
        import sentencepiece

        self._source = sentencepiece.SentencePieceProcessor(model_file=str(model_path / "source.spm"))
        self._target = sentencepiece.SentencePieceProcessor(model_file=str(model_path / "target.spm"))
        self._translator = ctranslate2.Translator(
            str(model_path),
            device=device,
            compute_type=compute_type,
            inter_threads=1,
            max_queued_batches=1,
        )

    def translate_batch(self, texts: list[str], target_language_token: str) -> list[str | Exception]:
        tokenized = [encode_marian_source(self._source, text, target_language_token) for text in texts]
        translated = self._translator.translate_batch(
            tokenized,
            beam_size=2,
            max_batch_size=32,
            max_input_length=512,
            max_decoding_length=512,
        )
        return [self._target.decode(result.hypotheses[0]) for result in translated]


def encode_marian_source(source_processor: object, text: str, target_language_token: str) -> list[str]:
    encode = getattr(source_processor, "encode")
    return [target_language_token, *encode(text, out_type=str), "</s>"]
