from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.translation_profiles import MODEL_PROFILES, TranslationModelProfile, get_model_profile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a pinned official translation model for local CTranslate2 use.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--download", action="store_true", help="Download the pinned official Hugging Face snapshot.")
    source.add_argument("--source-dir", type=Path, help="Use an already downloaded official snapshot.")
    parser.add_argument("--profile", choices=sorted(MODEL_PROFILES), default="opus-en-sla")
    parser.add_argument("--output", type=Path, help="Override the profile output directory inside local-models.")
    parser.add_argument("--force", action="store_true", help="Replace an existing prepared model only after conversion succeeds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile = get_model_profile(args.profile)
    repository_root = Path(__file__).resolve().parents[3]
    model_root = (repository_root / "local-models").resolve()
    output_arg = args.output or Path("local-models") / profile.output_dir
    output = (repository_root / output_arg).resolve() if not output_arg.is_absolute() else output_arg.resolve()

    model_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    source_dir = args.source_dir.resolve() if args.source_dir else model_root / f".source-{run_id}"
    staging = model_root / f".prepared-{run_id}"
    backup = model_root / f".backup-{run_id}"
    converter_vocab = model_root / f".vocab-{run_id}.yml"
    downloaded = args.source_dir is None
    started = time.perf_counter()
    try:
        ensure_child(output, model_root)
        if output.exists() and not args.force:
            raise RuntimeError(f"Ciljni model že obstaja: {output.name}. Uporabite --force samo za zavestno zamenjavo.")
        if args.download:
            from huggingface_hub import snapshot_download

            snapshot_download(
                repo_id=profile.model_id,
                revision=profile.revision,
                local_dir=source_dir,
                allow_patterns=list(profile.download_files),
            )
        source_files = validate_source(source_dir, profile)
        conversion_started = time.perf_counter()
        convert_model(profile, source_dir, staging, converter_vocab)
        conversion_ms = round((time.perf_counter() - conversion_started) * 1000)
        write_manifest(
            staging,
            profile,
            source_files,
            conversion_ms=conversion_ms,
            preparation_ms=round((time.perf_counter() - started) * 1000),
        )

        if output.exists():
            os.replace(output, backup)
        os.replace(staging, output)
        if backup.exists():
            shutil.rmtree(backup)
        print(f"Model je pripravljen v {output}")
        return 0
    except Exception as exc:
        print(f"Priprava modela ni uspela: {exc}", file=sys.stderr)
        return 1
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if converter_vocab.exists():
            converter_vocab.unlink()
        if downloaded and source_dir.exists():
            shutil.rmtree(source_dir)
        if backup.exists() and not output.exists():
            os.replace(backup, output)


def ensure_child(path: Path, root: Path) -> None:
    if path == root or root not in path.parents:
        raise RuntimeError("Model mora biti pripravljen znotraj projektne mape local-models.")


def validate_source(source_dir: Path, profile: TranslationModelProfile) -> dict[str, dict[str, int | str]]:
    missing = [name for name in profile.download_files if not (source_dir / name).is_file()]
    if missing:
        raise RuntimeError("Uradni snapshot nima vseh potrebnih datotek: " + ", ".join(missing))
    files = file_manifest(source_dir, profile.download_files)
    for name, expected in profile.expected_sha256.items():
        actual = files[name]["sha256"]
        if actual != expected:
            raise RuntimeError(f"SHA-256 se ne ujema za uradno datoteko {name}.")
    if profile.converter == "marian":
        validate_published_sentencepiece_vocab(source_dir, profile)
    return files


def validate_published_sentencepiece_vocab(source_dir: Path, profile: TranslationModelProfile) -> None:
    if not profile.marian_sentencepiece_file:
        raise RuntimeError("Marian profil nima SentencePiece modela.")
    import sentencepiece

    published_vocab = source_dir / "model.en-sl.vocab"
    processor = sentencepiece.SentencePieceProcessor(model_file=str(source_dir / profile.marian_sentencepiece_file))
    rows: list[tuple[str, float]] = []
    for line_number, line in enumerate(published_vocab.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            token, score = line.rsplit("\t", 1)
            rows.append((token, float(score)))
        except ValueError as exc:
            raise RuntimeError(f"Objavljeni SentencePiece vocab ni veljaven v vrstici {line_number}.") from exc
    pieces = [processor.id_to_piece(index) for index in range(processor.vocab_size())]
    if [token for token, _score in rows] != pieces:
        raise RuntimeError("Objavljeni vocab se ne ujema z uradnim SentencePiece modelom.")


def convert_model(
    profile: TranslationModelProfile,
    source_dir: Path,
    staging: Path,
    converter_vocab: Path,
) -> None:
    if profile.converter == "transformers":
        from ctranslate2.converters import TransformersConverter

        converter = TransformersConverter(
            str(source_dir),
            copy_files=list(profile.copy_files),
            low_cpu_mem_usage=True,
        )
        converter.convert(str(staging), quantization="int8", force=False)
        return
    if not profile.marian_model_file or not profile.marian_sentencepiece_file:
        raise RuntimeError("Marian profil nima modela ali SentencePiece konfiguracije.")
    from ctranslate2.converters import MarianConverter

    write_marian_vocabulary(source_dir / profile.marian_sentencepiece_file, converter_vocab)
    converter = MarianConverter(str(source_dir / profile.marian_model_file), [str(converter_vocab)])
    converter.convert(str(staging), quantization="int8", force=False)
    for name in profile.download_files:
        if name != profile.marian_model_file:
            shutil.copy2(source_dir / name, staging / name)


def write_marian_vocabulary(sentencepiece_path: Path, output_path: Path) -> None:
    import sentencepiece
    import yaml
    from ctranslate2.converters.marian import load_vocab

    processor = sentencepiece.SentencePieceProcessor(model_file=str(sentencepiece_path))
    pieces = [processor.id_to_piece(index) for index in range(processor.vocab_size())]
    output_path.write_text(
        yaml.safe_dump(dict(zip(pieces, range(len(pieces)))), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    if load_vocab(str(output_path)) != pieces:
        raise RuntimeError("Izpeljani Marian vocabulary se ne ujema s SentencePiece ID-ji.")


def write_manifest(
    model_dir: Path,
    profile: TranslationModelProfile,
    source_files: dict[str, dict[str, int | str]],
    conversion_ms: int,
    preparation_ms: int,
) -> None:
    manifest = {
        "schema_version": 2,
        "profile": profile.name,
        "provider_id": "local-opus-ctranslate2" if profile.name == "opus-en-sla" else "local-hplt-ctranslate2",
        "model_id": profile.model_id,
        "source_url": profile.source_url,
        "source_revision": profile.revision,
        "license": profile.license,
        "source_language": profile.source_language,
        "target_language": profile.target_language,
        "converter": profile.converter,
        "quantization": "int8",
        "tokenizer": {
            "type": "sentencepiece",
            "source_file": profile.tokenizer.source_file,
            "target_file": profile.tokenizer.target_file,
            "source_prefix": list(profile.tokenizer.source_prefix),
            "append_eos": profile.tokenizer.append_eos,
        },
        "prepared_at": datetime.now(UTC).isoformat(),
        "conversion_ms": conversion_ms,
        "preparation_ms": preparation_ms,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("ctranslate2", "sentencepiece", "transformers", "torch", "huggingface-hub", "PyYAML")
        },
        "source_files": source_files,
        "source_size_bytes": sum(int(item["size_bytes"]) for item in source_files.values()),
        "files": file_manifest(model_dir, (path.relative_to(model_dir).as_posix() for path in model_dir.rglob("*") if path.is_file())),
    }
    (model_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def file_manifest(root: Path, names: Iterable[str]) -> dict[str, dict[str, int | str]]:
    files: dict[str, dict[str, int | str]] = {}
    for name in sorted(str(value) for value in names):
        path = root / name
        files[name] = {"size_bytes": path.stat().st_size, "sha256": sha256(path)}
    return files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
