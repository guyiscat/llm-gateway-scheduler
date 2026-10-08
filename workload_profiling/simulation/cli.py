"""Replay original prompt pairs or portable token-length JSONL requests."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from ..common.io import sha256_file
from ..common.paths import DEFAULT_SOURCE, RESULTS
from ..common.tokenizer import load_tokenizer
from .config import CONFIG_PATH, load_config, positive_integer
from .engine import SimulationRunner
from .reporting import write_outputs
from .source import read_length_requests, read_prompt_requests

DEFAULT_OUTPUT = RESULTS / "simulation"


def execute(config, *, source=DEFAULT_SOURCE, source_format="prompt", limit=None,
            output=DEFAULT_OUTPUT, tokenizer=None, tokenizer_metadata=None, progress=None,
            strategy=None, batch_order=None):
    if limit is not None:
        positive_integer(limit, "limit")
    source, output = Path(source).resolve(), Path(output).resolve()
    # Fixed output names must never overwrite the replay's input file.
    if source.parent == output and source.name in {"config.json", "summary.json", "endpoints.json", "batches.json", "events.jsonl", "requests.csv", "report.md", "threshold_trace.csv", "classification_policy.json", "classification_metadata.json", "output_percentile_reference.parquet", "output_reference_metadata.json", "replay_config.json"}:
        raise ValueError("Output directory would overwrite the input file")
    source_hash = sha256_file(source)
    start = time.perf_counter()
    metadata = tokenizer_metadata or {}
    if source_format == "prompt":
        if tokenizer is None:
            tokenizer, metadata = load_tokenizer()
        requests = read_prompt_requests(source, tokenizer, limit=limit, progress=progress)
    elif source_format == "lengths":
        requests = read_length_requests(source, limit=limit)
    else:
        raise ValueError("source_format must be prompt or lengths")
    result = SimulationRunner(config, strategy=strategy, batch_order=batch_order).run(requests)
    if sha256_file(source) != source_hash:
        raise ValueError("Source changed during replay")
    result.summary["wall_time_seconds"] = round(time.perf_counter()-start, 3)
    provenance = {"created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                  "source": str(source), "source_sha256": source_hash, "source_format": source_format,
                  "source_record_semantics": "one physical prompt/response row; history not expanded" if source_format == "prompt" else "one token-length row",
                  "limit": limit, "tokenizer_id": metadata.get("tokenizer_id"),
                  "tokenizer_revision": metadata.get("revision"), "network_model_called": False}
    summary = write_outputs(result, config, output, provenance=provenance)
    return result, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--source-format", choices=["prompt", "lengths"], default="prompt")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, help="Replay the first N original records")
    parser.add_argument("--arrival-interval-ms", type=int)
    parser.add_argument("--arrival-mode", choices=["fixed", "burst"])
    parser.add_argument("--burst-size", type=int)
    parser.add_argument("--burst-span-ms", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--batch-wait-ms", type=int)
    parser.add_argument("--priority-assignment", choices=["uniform", "four_level"])
    parser.add_argument("--busy-rpm-threshold", type=float, help="Busy at RPM utilization >= this fraction, default 0.95")
    parser.add_argument("--busy-tpm-threshold", type=float, help="Busy at TPM utilization >= this fraction, default 0.95")
    parser.add_argument("--busy-concurrency-reserve", type=int, help="Busy at concurrency >= limit minus this margin, default 3")
    parser.add_argument("--priority-seed", type=int)
    parser.add_argument("--output-classification", choices=["tokens", "percentile_fixed", "percentile_dynamic"])
    parser.add_argument("--output-percentile-threshold", type=float)
    parser.add_argument("--output-policy", type=Path, help="Dynamic percentile mapping and pressure rules JSON")
    parser.add_argument("--output-reference", type=Path, help="Frozen output percentile reference Parquet")
    parser.add_argument("--output-reference-metadata", type=Path, help="Reference metadata JSON; required with --output-reference")
    parser.add_argument("--input-threshold", type=float)
    parser.add_argument("--output-threshold", type=float)
    parser.add_argument("--strategy", help="min_rpm or importable.module:class_or_factory")
    parser.add_argument("--batch-order", help="fifo, priority_then_light or importable.module:class_or_factory")
    args = parser.parse_args()
    overrides = {name: getattr(args, name) for name in ("arrival_interval_ms", "arrival_mode", "burst_size", "burst_span_ms", "batch_size", "batch_wait_ms", "priority_assignment", "priority_seed", "strategy", "batch_order") if getattr(args, name) is not None}
    for name in ("output_classification", "output_percentile_threshold"):
        if getattr(args, name) is not None:
            overrides[name] = getattr(args, name)
    for name in ("busy_rpm_threshold", "busy_tpm_threshold", "busy_concurrency_reserve"):
        if getattr(args, name) is not None:
            overrides[name] = getattr(args, name)
    for argument, field in (("output_policy", "output_policy_path"), ("output_reference", "output_reference_path"),
                            ("output_reference_metadata", "output_reference_metadata_path")):
        if getattr(args, argument) is not None:
            overrides[field] = str(getattr(args, argument).resolve())
    for argument, field in (("input_threshold", "input_threshold_tokens"), ("output_threshold", "output_threshold_tokens")):
        if getattr(args, argument) is not None:
            overrides[field] = getattr(args, argument)
    try:
        config = replace(load_config(args.config), **overrides)
        result, summary = execute(config, source=args.source, source_format=args.source_format,
                                  limit=args.limit, output=args.output_dir,
                                  progress=lambda n: print(f"Profiled {n} original requests", flush=True))
    except (ValueError, TypeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    print(f"Results: {args.output_dir.resolve()}")
    if result.summary["rejected_requests"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
