from __future__ import annotations

import argparse
import ctypes
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.translation import encode_sentencepiece_source, load_runtime_model_spec

BATCH_SIZE = 32
MEASURED_REPETITIONS = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two already prepared local CTranslate2 models.")
    parser.add_argument("--input-job", type=Path, required=True, help="Ignored translation-job JSON containing source segments.")
    parser.add_argument("--opus-model", type=Path, required=True)
    parser.add_argument("--hplt-model", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--worker-model", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.worker_model and args.worker_output:
        args.worker_output.write_text(
            json.dumps(run_model(args.worker_model, args.input_job), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return 0
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for name, model_path in (("opus", args.opus_model), ("hplt", args.hplt_model)):
        worker_output = output_dir / f".{name}-worker.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--input-job",
            str(args.input_job.resolve()),
            "--opus-model",
            str(args.opus_model.resolve()),
            "--hplt-model",
            str(args.hplt_model.resolve()),
            "--output-dir",
            str(output_dir),
            "--worker-model",
            str(model_path.resolve()),
            "--worker-output",
            str(worker_output),
        ]
        subprocess.run(command, check=True)
        results[name] = json.loads(worker_output.read_text(encoding="utf-8"))
        worker_output.unlink()
    comparison = merge_results(args.input_job, results)
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "comparison.md").write_text(render_markdown(comparison), encoding="utf-8")
    print(f"Primerjava je zapisana v {output_dir}")
    return 0


def run_model(model_path: Path, input_job: Path) -> dict[str, object]:
    import ctranslate2
    import sentencepiece

    spec = load_runtime_model_spec(model_path)
    manifest = json.loads((model_path / "manifest.json").read_text(encoding="utf-8"))
    segments = load_segments(input_job)
    source_processor = sentencepiece.SentencePieceProcessor(
        model_file=str(model_path / spec.tokenizer.source_file)
    )
    target_processor = sentencepiece.SentencePieceProcessor(
        model_file=str(model_path / spec.tokenizer.target_file)
    )
    prepared = [protect_segment(segment) for segment in segments]
    tokenized = [
        encode_sentencepiece_source(source_processor, item["protected_input"], spec.tokenizer)
        for item in prepared
    ]
    load_started = time.perf_counter()
    translator = ctranslate2.Translator(
        str(model_path), device="cpu", compute_type="int8", inter_threads=1, max_queued_batches=1
    )
    load_ms = round((time.perf_counter() - load_started) * 1000)
    _warmup_outputs, warmup_batches = translate_batches(translator, tokenized)
    measured_runs = []
    measured_outputs = []
    for _iteration in range(MEASURED_REPETITIONS):
        outputs, batch_timings = translate_batches(translator, tokenized)
        measured_outputs.append(outputs)
        measured_runs.append(
            {
                "batch_ms": batch_timings,
                "total_ms": sum(batch_timings),
            }
        )
    benchmark_outputs = measured_outputs[0]
    rows = []
    for segment, protected, input_tokens, result in zip(
        segments, prepared, tokenized, benchmark_outputs, strict=True
    ):
        raw_tokens = result.hypotheses[0]
        decoded = target_processor.decode(raw_tokens)
        placeholder = placeholder_diagnostics(decoded, protected["tokens"])
        warnings = []
        if not decoded.strip():
            warnings.append("empty_output")
        if len(decoded) > max(100, len(segment["source_text"]) * 5):
            warnings.append("suspicious_output_length")
        if not placeholder["valid"]:
            warnings.append("placeholder_integrity")
        rows.append(
            {
                "segment_id": segment["segment_id"],
                "source_text": segment["source_text"],
                "protected_input": protected["protected_input"],
                "input_tokens": input_tokens,
                "raw_output_tokens": raw_tokens,
                "decoded_output": decoded,
                "restored_output": restore_placeholders(decoded, protected["tokens"]) if placeholder["valid"] else None,
                "placeholder": placeholder,
                "warnings": warnings,
            }
        )
    return {
        "profile": spec.profile,
        "model_id": spec.model_id,
        "model_path_name": model_path.name,
        "manifest": manifest,
        "prepared_size_bytes": sum(path.stat().st_size for path in model_path.rglob("*") if path.is_file()),
        "load_ms": load_ms,
        "warmup_batch_ms": warmup_batches,
        "warmup_total_ms": sum(warmup_batches),
        "measured_runs": measured_runs,
        "timing_ms": timing_summary([int(run["total_ms"]) for run in measured_runs]),
        "peak_working_set_bytes": peak_working_set_bytes(),
        "successful_segments": sum(not row["warnings"] for row in rows),
        "placeholder_failures": sum(not row["placeholder"]["valid"] for row in rows),
        "empty_outputs": sum("empty_output" in row["warnings"] for row in rows),
        "invalid_outputs": sum(bool(row["warnings"]) for row in rows),
        "segments": rows,
    }


def translate_batches(translator: object, tokenized: list[list[str]]) -> tuple[list[object], list[int]]:
    outputs: list[object] = []
    timings = []
    for offset in range(0, len(tokenized), BATCH_SIZE):
        started = time.perf_counter()
        batch = translator.translate_batch(
            tokenized[offset : offset + BATCH_SIZE],
            beam_size=2,
            max_batch_size=BATCH_SIZE,
            max_input_length=512,
            max_decoding_length=512,
        )
        timings.append(round((time.perf_counter() - started) * 1000))
        outputs.extend(batch)
    return outputs, timings


def timing_summary(values: list[int]) -> dict[str, int]:
    if not values:
        raise ValueError("Za izračun časovnih metrik je potrebna vsaj ena ponovitev.")
    return {
        "minimum": min(values),
        "median": round(statistics.median(values)),
        "maximum": max(values),
    }


def load_segments(input_job: Path) -> list[dict[str, object]]:
    payload = json.loads(input_job.read_text(encoding="utf-8"))
    return [
        segment
        for segment in payload["segments"]
        if not segment.get("skip_reason") and isinstance(segment.get("source_text"), str)
    ]


def protect_segment(segment: dict[str, object]) -> dict[str, object]:
    protected = str(segment["source_text"])
    tokens = segment.get("protected_tokens") or []
    for token in tokens:
        protected = protected.replace(str(token["value"]), str(token["placeholder"]), 1)
    return {"protected_input": protected, "tokens": tokens}


def placeholder_diagnostics(text: str, tokens: list[dict[str, object]]) -> dict[str, object]:
    expected = [str(token["placeholder"]) for token in tokens]
    counts = {placeholder: text.count(placeholder) for placeholder in expected}
    missing = [placeholder for placeholder, count in counts.items() if count == 0]
    duplicated = [placeholder for placeholder, count in counts.items() if count > 1]
    changed = [placeholder for placeholder in expected if counts[placeholder] != 1]
    return {
        "valid": not missing and not duplicated,
        "expected": expected,
        "missing": missing,
        "duplicated": duplicated,
        "changed": changed,
    }


def restore_placeholders(text: str, tokens: list[dict[str, object]]) -> str:
    restored = text
    for token in tokens:
        restored = restored.replace(
            str(token["placeholder"]), str(token.get("replacement_value") or token["value"])
        )
    return restored


def merge_results(input_job: Path, results: dict[str, dict[str, object]]) -> dict[str, object]:
    by_model = {
        name: {row["segment_id"]: row for row in result["segments"]}
        for name, result in results.items()
    }
    segment_ids = [row["segment_id"] for row in results["opus"]["segments"]]
    return {
        "schema_version": 1,
        "source_job_name": input_job.name,
        "comparison_rules": {
            "same_source_text": True,
            "same_placeholders": True,
            "batch_size": BATCH_SIZE,
            "beam_size": 2,
            "glossary": False,
            "manual_corrections": False,
            "automatic_winner": False,
            "warmup_runs": 1,
            "measured_repetitions": MEASURED_REPETITIONS,
            "model_execution_order": ["opus", "hplt"],
            "system_cache_note": (
                "Models run sequentially in isolated processes; OS file-cache and concurrent system load may affect timings."
            ),
        },
        "models": {name: {key: value for key, value in result.items() if key != "segments"} for name, result in results.items()},
        "segments": [
            {
                "segment_id": segment_id,
                "source_text": by_model["opus"][segment_id]["source_text"],
                "opus": {key: value for key, value in by_model["opus"][segment_id].items() if key != "source_text"},
                "hplt": {key: value for key, value in by_model["hplt"][segment_id].items() if key != "source_text"},
            }
            for segment_id in segment_ids
        ],
    }


def render_markdown(comparison: dict[str, object]) -> str:
    lines = ["# Local translation model comparison", "", "No automatic winner is selected.", ""]
    for name, model in comparison["models"].items():
        lines.extend(
            [
                f"## {name.upper()}",
                "",
                f"- Model: `{model['model_id']}`",
                f"- Prepared size: {model['prepared_size_bytes']} bytes",
                f"- Load: {model['load_ms']} ms",
                f"- Warm-up batches: {model['warmup_batch_ms']} ms",
                f"- Measured totals: {[run['total_ms'] for run in model['measured_runs']]} ms",
                f"- Measured minimum/median/maximum: {model['timing_ms']['minimum']} / {model['timing_ms']['median']} / {model['timing_ms']['maximum']} ms",
                f"- Peak working set: {model['peak_working_set_bytes']} bytes",
                f"- Placeholder failures: {model['placeholder_failures']}",
                f"- Empty/invalid outputs: {model['invalid_outputs']}",
                "",
            ]
        )
    lines.extend(["## Segments", "", "| Source | OPUS | HPLT | Placeholder status |", "|---|---|---|---|"])
    for row in comparison["segments"]:
        source = markdown_cell(row["source_text"])
        opus = markdown_cell(row["opus"]["restored_output"] or row["opus"]["decoded_output"])
        hplt = markdown_cell(row["hplt"]["restored_output"] or row["hplt"]["decoded_output"])
        status = f"OPUS: {row['opus']['placeholder']['valid']}; HPLT: {row['hplt']['placeholder']['valid']}"
        lines.append(f"| {source} | {opus} | {hplt} | {status} |")
    return "\n".join(lines) + "\n"


def markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def peak_working_set_bytes() -> int | None:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    process = get_current_process()
    get_memory_info = getattr(
        ctypes.windll.kernel32,
        "K32GetProcessMemoryInfo",
        ctypes.windll.psapi.GetProcessMemoryInfo,
    )
    get_memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
    get_memory_info.restype = wintypes.BOOL
    if not get_memory_info(process, ctypes.byref(counters), counters.cb):
        return None
    return int(counters.PeakWorkingSetSize)


if __name__ == "__main__":
    raise SystemExit(main())
