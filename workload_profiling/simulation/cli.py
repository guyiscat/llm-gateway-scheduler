"""Replay local requests and record dispatched LiteLLM parameters as JSONL."""
import argparse
from dataclasses import replace
import os
from pathlib import Path

from ..adapters import RouteOutputRecorder
from ..common.io import sha256_file
from ..common.paths import DEFAULT_SOURCE, RESULTS
from ..common.tokenizer import load_tokenizer
from ..common.run_log import RunLog
from .config import CONFIG_PATH, load_config, positive_integer
from .engine import SimulationRunner
from .source import read_length_requests, read_prompt_requests

DEFAULT_OUTPUT = RESULTS / "routes.jsonl"


def execute(config, *, source=DEFAULT_SOURCE, source_format="prompt", limit=None,
            output=DEFAULT_OUTPUT, tokenizer=None, progress=None,
            strategy=None, batch_order=None, on_request=None):
    if limit is not None:
        positive_integer(limit, "limit")
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or (output.exists() and os.path.samefile(source, output)):
        raise ValueError("Route output must not overwrite the source file")
    if output.suffix.lower() != ".jsonl":
        raise ValueError("Route output must be a .jsonl file")
    source_hash = sha256_file(source)
    if source_format == "prompt":
        if tokenizer is None:
            tokenizer, _ = load_tokenizer()
        requests = read_prompt_requests(source, tokenizer, limit=limit, progress=progress)
    elif source_format == "lengths":
        requests = read_length_requests(source, limit=limit)
    else:
        raise ValueError("source_format must be prompt or lengths")
    with RouteOutputRecorder(output, (e.to_core() for e in config.endpoints)) as recorder:
        result = SimulationRunner(config, strategy=strategy, batch_order=batch_order,
                                  on_route=recorder, on_request=on_request).run(requests)
        if sha256_file(source) != source_hash:
            raise ValueError("Source changed during replay")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--source-format", choices=["prompt", "lengths"], default="prompt")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Local route handoff JSONL file")
    logs = parser.add_mutually_exclusive_group()
    logs.add_argument("--log-dir", type=Path, help="New run directory with one JSONL file per request")
    logs.add_argument("--log-file", dest="log_dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, help="Replay the first N original records")
    parser.add_argument("--arrival-interval-ms", type=int)
    parser.add_argument("--arrival-mode", choices=["fixed", "burst", "random"])
    parser.add_argument("--burst-size", type=int)
    parser.add_argument("--burst-span-ms", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--batch-wait-ms", type=int)
    parser.add_argument("--priority-assignment", choices=["uniform", "four_level", "binary"])
    parser.add_argument("--high-priority-ratio", type=float, help="High priority proportion in binary simulation mode")
    for name in ("arrival_seed", "random_min_interval_ms", "random_max_interval_ms", "cooldown_ms",
                 "history_window_ms", "history_max_samples", "max_tokens", "predicted_output_tokens"):
        parser.add_argument("--" + name.replace("_", "-"), type=int)
    for name in ("busy_concurrency_threshold", "input_weight", "output_weight"):
        parser.add_argument("--" + name.replace("_", "-"), type=float)
    parser.add_argument("--ranking-direction", choices=["ascending", "descending"])
    parser.add_argument("--prediction-mode", choices=["oracle", "fixed"])
    parser.add_argument("--target-model")
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None)
    for name in ("ttft_ms", "tpot_ms", "e2e_ms"):
        parser.add_argument("--slo-" + name.replace("_", "-"), type=float)
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
    parser.add_argument("--batch-order", help="fifo, priority_then_light, weighted_length or importable.module:class_or_factory")
    args = parser.parse_args()
    overrides = {name: getattr(args, name) for name in ("arrival_interval_ms", "arrival_mode", "burst_size", "burst_span_ms", "batch_size", "batch_wait_ms", "priority_assignment", "priority_seed", "strategy", "batch_order") if getattr(args, name) is not None}
    for name in ("high_priority_ratio", "arrival_seed", "random_min_interval_ms", "random_max_interval_ms",
                 "cooldown_ms", "history_window_ms", "history_max_samples", "max_tokens", "predicted_output_tokens",
                 "busy_concurrency_threshold", "input_weight", "output_weight", "ranking_direction",
                 "prediction_mode", "target_model", "stream"):
        if getattr(args, name) is not None:
            overrides[name] = getattr(args, name)
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
        base = load_config(args.config)
        slo = {name: getattr(args, "slo_" + name) for name in ("ttft_ms", "tpot_ms", "e2e_ms")
               if getattr(args, "slo_" + name) is not None}
        if slo:
            overrides["slo"] = (base.slo or {}) | slo
        config = replace(base, **overrides)
        with RunLog(args.log_dir, mode="replay", protected_paths=(args.source, args.output)) as log:
            print(f"Request log directory: {log.path}", flush=True)
            result = execute(config, source=args.source, source_format=args.source_format,
                             limit=args.limit, output=args.output, on_request=log.scheduler_request,
                             progress=lambda n: print(f"Profiled {n} original requests", flush=True))
            log.simulation_result(result)
    except (ValueError, TypeError, OSError) as error:
        parser.error(str(error))
    print(f"Recorded {sum(row['endpoint_id'] is not None for row in result.requests)} routes: {args.output.resolve()}")
    if result.summary["rejected_requests"]:
        print(f"Rejected {result.summary['rejected_requests']} requests without route output")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
