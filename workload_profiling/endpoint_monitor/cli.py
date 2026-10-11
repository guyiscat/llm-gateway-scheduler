"""Log in once and continuously refresh endpoint metadata and local state."""
import argparse
from http.client import HTTPException
from pathlib import Path

from ..common.paths import PACKAGE, RESULTS
from ..common.validation import finite_number, positive_integer
from ..core.config import load_scheduler_config
from ..integrations.litellm_gateway import LiteLLMSession
from .service import EndpointMonitor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--config", type=Path, default=PACKAGE / "config/scheduler_config.json")
    parser.add_argument("--endpoint-state-dir", type=Path, default=RESULTS / "endpoints")
    parser.add_argument("--endpoint-update-interval", type=float, default=30, help="Refresh interval in seconds (default 30); wait for in-flight requests to finish")
    parser.add_argument("--proxy-port", type=int, default=4000)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--once", action="store_true", help="Fetch one metadata snapshot, then close; no model calls")
    for name in ("rpm", "tpm", "concurrency"):
        parser.add_argument("--endpoint-" + name + "-limit", type=int)
    args = parser.parse_args()
    try:
        finite_number(args.endpoint_update_interval, "endpoint_update_interval")
        for name in ("rpm", "tpm", "concurrency"):
            if getattr(args, "endpoint_" + name + "_limit") is not None:
                positive_integer(getattr(args, "endpoint_" + name + "_limit"), name + "_limit")
        if args.config.resolve() in {args.endpoint_state_dir.resolve() / name for name in ("endpoint_catalog_raw.json", "endpoint_state.json")}:
            raise ValueError("Endpoint files must not overwrite the scheduler configuration")
        with LiteLLMSession(args.ssh_target, proxy_port=args.proxy_port, timeout=args.timeout) as session:
            settings = load_scheduler_config(args.config)
            with EndpointMonitor(session, args.endpoint_state_dir, settings,
                    update_interval_seconds=args.endpoint_update_interval, periodic=not args.once,
                    limit_overrides={name + "_limit": getattr(args, "endpoint_" + name + "_limit")
                                     for name in ("rpm", "tpm", "concurrency") if getattr(args, "endpoint_" + name + "_limit") is not None}) as monitor:
                print(f"Raw endpoint catalog: {monitor.raw_path}")
                print(f"Maintained endpoint state: {monitor.state_path}")
                if not args.once:
                    print(f"Updating every {monitor.interval} seconds; Ctrl+C to close.", flush=True)
                    monitor.stop.wait()
    except KeyboardInterrupt:
        print("\nEndpoint maintenance stopped; SSH session closed.")
    except (ValueError, TypeError, OSError, HTTPException) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
