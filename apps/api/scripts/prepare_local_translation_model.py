from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

MODEL_ID = "Helsinki-NLP/opus-mt-en-sla"
MODEL_REVISION = "0bc26914f2f82c3dd5b235e420aa2c711a5ed3d8"
MODEL_LICENSE = "Apache-2.0"
MODEL_URL = f"https://huggingface.co/{MODEL_ID}"
DOWNLOAD_FILES = (
    "README.md",
    "config.json",
    "generation_config.json",
    "metadata.json",
    "pytorch_model.bin",
    "source.spm",
    "target.spm",
    "tokenizer_config.json",
    "vocab.json",
)
COPY_FILES = (
    "source.spm",
    "target.spm",
    "tokenizer_config.json",
    "vocab.json",
    "generation_config.json",
    "metadata.json",
    "README.md",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare the official OPUS-MT English-to-Slavic model for local CTranslate2 use.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--download", action="store_true", help="Download the pinned official Hugging Face snapshot.")
    source.add_argument("--source-dir", type=Path, help="Use an already downloaded official snapshot.")
    parser.add_argument("--output", type=Path, default=Path("local-models/opus-mt-en-sla-ct2-int8"))
    parser.add_argument("--force", action="store_true", help="Replace an existing prepared model only after conversion succeeds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository_root = Path(__file__).resolve().parents[3]
    model_root = (repository_root / "local-models").resolve()
    output = (repository_root / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    ensure_child(output, model_root)
    if output.exists() and not args.force:
        raise RuntimeError(f"Ciljni model že obstaja: {output.name}. Uporabite --force samo za zavestno zamenjavo.")

    model_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    source_dir = args.source_dir.resolve() if args.source_dir else model_root / f".source-{run_id}"
    staging = model_root / f".prepared-{run_id}"
    backup = model_root / f".backup-{run_id}"
    downloaded = args.source_dir is None
    try:
        if args.download:
            from huggingface_hub import snapshot_download

            snapshot_download(
                repo_id=MODEL_ID,
                revision=MODEL_REVISION,
                local_dir=source_dir,
                allow_patterns=list(DOWNLOAD_FILES),
            )
        validate_source(source_dir)

        from ctranslate2.converters import TransformersConverter

        converter = TransformersConverter(str(source_dir), copy_files=list(COPY_FILES), low_cpu_mem_usage=True)
        converter.convert(str(staging), quantization="int8", force=False)
        write_manifest(staging)

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
        if downloaded and source_dir.exists():
            shutil.rmtree(source_dir)
        if backup.exists() and not output.exists():
            os.replace(backup, output)


def ensure_child(path: Path, root: Path) -> None:
    if path == root or root not in path.parents:
        raise RuntimeError("Model mora biti pripravljen znotraj projektne mape local-models.")


def validate_source(source_dir: Path) -> None:
    missing = [name for name in DOWNLOAD_FILES if not (source_dir / name).is_file()]
    if missing:
        raise RuntimeError("Uradni snapshot nima vseh potrebnih datotek: " + ", ".join(missing))


def write_manifest(model_dir: Path) -> None:
    files = {}
    for path in sorted(model_dir.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[path.relative_to(model_dir).as_posix()] = {
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
    manifest = {
        "schema_version": 1,
        "model_id": MODEL_ID,
        "source_url": MODEL_URL,
        "source_revision": MODEL_REVISION,
        "license": MODEL_LICENSE,
        "quantization": "int8",
        "prepared_at": datetime.now(UTC).isoformat(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("ctranslate2", "sentencepiece", "transformers", "torch", "huggingface-hub")
        },
        "files": files,
    }
    (model_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
