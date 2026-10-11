"""Replay N local requests and send handoffs through an existing login session.

The virtual-clock replay and its simulated feedback remain unchanged. Real
calls run afterwards, sequentially, in one SSH session without retries.
"""
import argparse
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
import sys

from ..common.paths import DEFAULT_SOURCE
from ..common.run_log import RunLog
from ..simulation.replay import DEFAULT_OUTPUT, execute
from ..simulation.config import CONFIG_PATH, load_config, load_scheduler_settings, positive_integer
from ..simulation.engine import RunResult
from .litellm_gateway import (
    build_payload, display_response_chunk, display_result,
    iter_routes, validate_payload_options,
)

@dataclass(frozen=True)
class ReplayDeliveryResult:
    simulation: RunResult
    prepared_count: int
    sent_count: int
    log_path: Path | None = None


class DeliveryError(RuntimeError):
    """Stop after a failed or ambiguous call, preserving the generated routes."""
    def __init__(self, request_id, *, successful_count, pending_count, detail):
        self.request_id = request_id
        self.successful_count = successful_count
        self.pending_count = pending_count
        super().__init__(f"Delivery stopped at request_id={request_id}: {detail}. "
                         f"Successful={successful_count}; remaining unsent={pending_count}. No automatic retry.")


def replay_and_send(*, connected_session, connected_monitor, config_path=CONFIG_PATH,
                    source=DEFAULT_SOURCE, source_format="prompt", limit=3,
                    output=DEFAULT_OUTPUT, endpoint_groups=None, max_tokens=None,
                    no_max_tokens=False, tokenizer=None, progress=None,
                    on_response=None, log_dir=None, cancelled=None):
    """Replay using live discovered endpoints and an already authenticated session.

    on_response(route, response) receives real responses after they are logged.
    limit is an input-record upper bound; rejected requests have no handoff.
    """
    positive_integer(limit, "limit")
    if on_response is not None and not callable(on_response):
        raise TypeError("on_response must be callable")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable")
    if connected_session is None or connected_monitor is None or connected_monitor.session is not connected_session:
        raise ValueError("An authenticated session and its endpoint monitor are required")
    validate_payload_options(endpoint_groups=endpoint_groups, max_tokens=max_tokens, no_max_tokens=no_max_tokens)
    log = RunLog(log_dir, mode="replay-send", protected_paths=(source, output))
    state_dir = connected_monitor.directory
    for filename in ("endpoint_catalog_raw.json", "endpoint_state.json"):
        path = state_dir / filename
        if any(path == Path(protected).resolve() for protected in (source, output, config_path)):
            raise ValueError("Endpoint state must not overwrite source, route output or configuration")
    with log, connected_session.recording(log):
        print(f"Request log directory: {log.path}", flush=True)
        print("Using existing login. Starting local replay...", flush=True)
        monitor = connected_monitor
        if load_scheduler_settings(config_path) != monitor.settings:
            raise ValueError("Scheduling settings changed; close and log in again with the new --config")
        config = load_config(config_path, endpoint_configs=monitor.endpoint_configs(endpoint_groups=endpoint_groups))
        print(f"LiteLLM endpoint state: {monitor.state_path}", flush=True)
        def request_received(request):
            log.scheduler_request(request)
            monitor.capture_request(request)
        simulation = execute(config, source=source, source_format=source_format, limit=limit,
                             output=output, tokenizer=tokenizer, progress=progress, on_request=request_received,
                             route_adapter=monitor.route_adapter())
        log.simulation_result(simulation)
        routes = list(iter_routes(output))
        if len(routes) != simulation.summary["endpoint_executed_requests"] or len(routes) > limit:
            raise ValueError("Route output count does not match the current replay")
        # Validate every payload before the first paid POST, including later rows.
        payloads = [build_payload(route, endpoint_groups=endpoint_groups,
                                  max_tokens=max_tokens, no_max_tokens=no_max_tokens) for route in routes]
        payloads = [monitor.bind_payload(route, payload) for route, payload in zip(routes, payloads)]
        print(f"Replayed {simulation.summary['total_requests']} input records; prepared {len(routes)} routes: "
              f"{Path(output).resolve()}", flush=True)
        if simulation.summary["rejected_requests"]:
            print(f"Rejected {simulation.summary['rejected_requests']} requests; those will not be sent.", flush=True)
        for index, (route, payload) in enumerate(zip(routes, payloads)):
            request_id = route["request_id"]
            if cancelled is not None and cancelled():
                raise DeliveryError(request_id, successful_count=index, pending_count=len(routes) - index,
                                    detail="Session closing; request was not sent")
            print(f"Sending {index + 1}/{len(routes)}: request_id={request_id}, "
                  f"selected_endpoint_id={route['selected_endpoint_id']}, model={payload['model']}", flush=True)
            try:
                response = monitor.send(route, payload, on_response_chunk=display_response_chunk, cancelled=cancelled)
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
        return ReplayDeliveryResult(simulation, len(routes), len(routes), log.path)


def main():
    from .session_control import SESSION_DIR, send_batch
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Default: logged-in session configuration")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--source-format", choices=["prompt", "lengths"], default="prompt")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--log-dir", type=Path, help="New run directory with one JSONL file per request")
    parser.add_argument("--limit", type=int, default=3, help="Replay up to N input records; default 3")
    parser.add_argument("--session-dir", type=Path, default=SESSION_DIR,
                        help="Session created by litellm-login; no login is performed by this command")
    parser.add_argument("--endpoint-group", action="append", help="Optional remote group; no default")
    limits = parser.add_mutually_exclusive_group()
    limits.add_argument("--max-tokens", type=int, help="Override the real request's output limit")
    limits.add_argument("--no-max-tokens", action="store_true", help="Omit any recorded output limit")
    args = parser.parse_args()
    try:
        options = dict(source=str(args.source.resolve()), source_format=args.source_format,
                       output=str(args.output.resolve()), limit=args.limit, endpoint_groups=args.endpoint_group,
                       max_tokens=args.max_tokens, no_max_tokens=args.no_max_tokens)
        if args.config is not None:
            options["config_path"] = str(args.config.resolve())
        if args.log_dir is not None:
            options["log_dir"] = str(args.log_dir.resolve())
        result = send_batch(args.session_dir, options)
        print(f"Sent {result['sent_count']} requests. Login session remains open.")
        if result["rejected_count"]:
            raise SystemExit(2)
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted. Requests are not automatically resent. Use litellm-status or litellm-close to inspect/end the session.", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, TypeError, OSError, HTTPException, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
