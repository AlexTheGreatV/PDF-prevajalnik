from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ConverterKind = Literal["transformers", "marian"]
DEFAULT_TRANSLATION_PROFILE = "hplt-en-sl-v2"


@dataclass(frozen=True)
class TokenizerProfile:
    source_file: str
    target_file: str
    source_prefix: tuple[str, ...] = ()
    append_eos: bool = False


@dataclass(frozen=True)
class TranslationModelProfile:
    name: str
    model_id: str
    revision: str
    license: str
    source_url: str
    source_language: str
    target_language: str
    converter: ConverterKind
    output_dir: str
    download_files: tuple[str, ...]
    tokenizer: TokenizerProfile
    expected_sha256: dict[str, str]
    copy_files: tuple[str, ...] = ()
    marian_model_file: str | None = None
    marian_sentencepiece_file: str | None = None


MODEL_PROFILES: dict[str, TranslationModelProfile] = {
    "opus-en-sla": TranslationModelProfile(
        name="opus-en-sla",
        model_id="Helsinki-NLP/opus-mt-en-sla",
        revision="0bc26914f2f82c3dd5b235e420aa2c711a5ed3d8",
        license="Apache-2.0",
        source_url="https://huggingface.co/Helsinki-NLP/opus-mt-en-sla",
        source_language="en",
        target_language="sl",
        converter="transformers",
        output_dir="opus-mt-en-sla-ct2-int8",
        download_files=(
            "README.md",
            "config.json",
            "generation_config.json",
            "metadata.json",
            "pytorch_model.bin",
            "source.spm",
            "target.spm",
            "tokenizer_config.json",
            "vocab.json",
        ),
        copy_files=(
            "source.spm",
            "target.spm",
            "tokenizer_config.json",
            "vocab.json",
            "generation_config.json",
            "metadata.json",
            "README.md",
        ),
        tokenizer=TokenizerProfile(
            source_file="source.spm",
            target_file="target.spm",
            source_prefix=(">>slv<<",),
            append_eos=True,
        ),
        expected_sha256={
            "pytorch_model.bin": "1352ae4ef442420c47e9a9693a4baa2628be4579b167e04a801e57096f390196",
        },
    ),
    "hplt-en-sl-v2": TranslationModelProfile(
        name="hplt-en-sl-v2",
        model_id="HPLT/translate-en-sl-v2.0-hplt",
        revision="6604d94dd36c3e29fe0ebbf8de8197668445ec0c",
        license="CC-BY-4.0",
        source_url="https://huggingface.co/HPLT/translate-en-sl-v2.0-hplt",
        source_language="en",
        target_language="sl",
        converter="marian",
        output_dir="hplt-en-sl-v2-ct2-int8",
        download_files=(
            "README.md",
            "model.en-sl.spm",
            "model.en-sl.vocab",
            "model.npz.best-chrf.npz",
        ),
        tokenizer=TokenizerProfile(
            source_file="model.en-sl.spm",
            target_file="model.en-sl.spm",
            source_prefix=(),
            append_eos=False,
        ),
        expected_sha256={
            "README.md": "43f4ee4ac84ce0d23ba984c32bc8d819cc1c832b781f05bbc786f1e356647f48",
            "model.en-sl.spm": "a04b8a45b64da34895cab975a8d68b106efd03fef75137f3e458819e394c0c9d",
            "model.en-sl.vocab": "a8a5f1242caf32e5428f04a383c3a9cc499e12040a76dde25189ab38fb2d2b19",
            "model.npz.best-chrf.npz": "e2a9d7f680e6fbd9de1bf909ed6fae282d8e96418430b10d2eb9f7c3edb90fe6",
        },
        marian_model_file="model.npz.best-chrf.npz",
        marian_sentencepiece_file="model.en-sl.spm",
    ),
}


def get_model_profile(name: str) -> TranslationModelProfile:
    try:
        return MODEL_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Neznan modelni profil: {name}") from exc


def profile_for_model_id(model_id: str) -> TranslationModelProfile | None:
    return next((profile for profile in MODEL_PROFILES.values() if profile.model_id == model_id), None)
