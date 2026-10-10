"""Log in first, replay N local requests, then send their routed handoffs.

The virtual-clock replay and its simulated feedback remain unchanged. Real
calls run afterwards, sequentially, in one SSH session without retries.
"""
import argparse
from contextlib import nullcontext
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
import sys

from ..common.paths import DEFAULT_SOURCE
from ..common.run_log import RunLog
from ..simulation.cli import DEFAULT_OUTPUT, execute
from ..simulation.config import CONFIG_PATH, load_config, positive_integer
from ..simulation.engine import RunResult
from .litellm_gateway import (
    LiteLLMSession, build_payload, display_response_chunk, display_result,
    iter_routes, validate_timeout, validate_payload_options,
)

@dataclass(frozen=True)
class ReplayDeliveryResult:
    simulation: RunResult
    prepared_count: int
    sent_count: int
    dry_run: bool
    log_path: Path | None = None


class DeliveryError(RuntimeError):
    """Stop after a failed or ambiguous call, preserving the generated routes."""
    def __init__(self, request_id, *, successful_count, pending_count, detail):
        self.request_id = request_id
        self.successful_count = successful_count
        self.pending_count = pending_count
        super().__init__(f"Delivery stopped at request_id={request_id}: {detail}. "
                         f"Successful={successful_count}; remaining unsent={pending_count}. No automatic retry.")


def replay_and_send(*, ssh_target=None, config_path=CONFIG_PATH,
                    source=DEFAULT_SOURCE, source_format="prompt", limit=3,
                    output=DEFAULT_OUTPUT, endpoint_groups=None, max_tokens=None,
                    no_max_tokens=False, proxy_port=4000, timeout=60, dry_run=False,
                    tokenizer=None, progress=None, on_response=None, log_dir=None, log_file=None):
    """Open the session before config/data/tokenizer loading, then run replay.

    on_response(route, response) receives real responses after they are logged.
    dry_run generates and validates the same handoffs without SSH or model calls.
    limit is an input-record upper bound; rejected requests have no handoff.
    """
    positive_integer(limit, "limit")
    validate_timeout(timeout)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    if on_response is not None and not callable(on_response):
        raise TypeError("on_response must be callable")
    # Validate delivery controls before authenticating, without reading a dataset.
    validate_payload_options(endpoint_groups=endpoint_groups, max_tokens=max_tokens, no_max_tokens=no_max_tokens)
    if not dry_run and not ssh_target:
        raise ValueError("--ssh-target is required unless --dry-run is used")
    if log_dir is not None and log_file is not None:
        raise ValueError("Use log_dir or the legacy log_file, not both")
    log = RunLog(log_dir if log_dir is not None else log_file,
                 mode="replay-send-dry-run" if dry_run else "replay-send", protected_paths=(source, output))
    context = nullcontext(None) if dry_run else LiteLLMSession(
        ssh_target, proxy_port=proxy_port, timeout=timeout, run_log=log)
    if not dry_run:
        print("Logging in to LiteLLM before loading data or starting replay...", flush=True)
    with log, context as session:
        print(f"Request log directory: {log.path}", flush=True)
        if not dry_run:
            print("Login ready. Starting local replay...", flush=True)
        config = load_config(config_path)
        simulation = execute(config, source=source, source_format=source_format, limit=limit,
                             output=output, tokenizer=tokenizer, progress=progress, on_request=log.scheduler_request)
        log.simulation_result(simulation)
        routes = list(iter_routes(output))
        if len(routes) != simulation.summary["endpoint_executed_requests"] or len(routes) > limit:
            raise ValueError("Route output count does not match the current replay")
        # Validate every payload before the first paid POST, including later rows.
        payloads = [build_payload(route, endpoint_groups=endpoint_groups,
                                  max_tokens=max_tokens, no_max_tokens=no_max_tokens) for route in routes]
        print(f"Replayed {simulation.summary['total_requests']} input records; prepared {len(routes)} routes: "
              f"{Path(output).resolve()}", flush=True)
        if simulation.summary["rejected_requests"]:
            print(f"Rejected {simulation.summary['rejected_requests']} requests; those will not be sent.", flush=True)
        if dry_run:
            print("Dry run complete. No SSH connection or model requests.", flush=True)
            return ReplayDeliveryResult(simulation, len(routes), 0, True, log.path)
        for index, (route, payload) in enumerate(zip(routes, payloads)):
            request_id = route["request_id"]
            print(f"Sending {index + 1}/{len(routes)}: request_id={request_id}, "
                  f"selected_endpoint_id={route['selected_endpoint_id']}, model={payload['model']}", flush=True)
            try:
                response = session.send(payload, on_response_chunk=display_response_chunk,
                                        request_id=request_id, selected_endpoint_id=route["selected_endpoint_id"])
            except (OSError, HTTPException) as error:
                raise DeliveryError(request_id, successful_count=index,
                                    pending_count=len(routes) - index - 1, detail=str(error)) from error
            if on_response is None:
                display_result(response)
            else:
                on_response(route, response)
            if not 200 <= response["http_status"] < 300:
                raise DeliveryError(request_id, successful_count=index,
                                    pending_count=len(routes) - index - 1,
                                    detail=f"HTTP {response['http_status']}")
        return ReplayDeliveryResult(simulation, len(routes), len(routes), False, log.path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--source-format", choices=["prompt", "lengths"], default="prompt")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    logs = parser.add_mutually_exclusive_group()
    logs.add_argument("--log-dir", type=Path, help="New run directory with one JSONL file per request")
    logs.add_argument("--log-file", dest="log_dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=3, help="Replay up to N input records; default 3")
    parser.add_argument("--ssh-target", help="SSH user@host; required unless --dry-run")
    parser.add_argument("--endpoint-group", action="append", help="Optional remote group; no default")
    limits = parser.add_mutually_exclusive_group()
    limits.add_argument("--max-tokens", type=int, help="Override the real request's output limit")
    limits.add_argument("--no-max-tokens", action="store_true", help="Omit any recorded output limit")
    parser.add_argument("--proxy-port", type=int, default=4000)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true", help="Replay and validate locally, without SSH or real calls")
    args = parser.parse_args()
    try:
        result = replay_and_send(ssh_target=args.ssh_target,
            config_path=args.config, source=args.source, source_format=args.source_format,
            output=args.output, log_dir=args.log_dir, limit=args.limit, endpoint_groups=args.endpoint_group,
            max_tokens=args.max_tokens, no_max_tokens=args.no_max_tokens,
            proxy_port=args.proxy_port, timeout=args.timeout, dry_run=args.dry_run,
            progress=lambda n: print(f"Profiled {n} original requests", flush=True))
    except DeliveryError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted. SSH session closed; requests are not automatically resent.", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, TypeError, OSError) as error:
        parser.error(str(error))
    if not result.dry_run:
        print(f"Sent {result.sent_count} requests. SSH session closed.")
    if result.simulation.summary["rejected_requests"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
