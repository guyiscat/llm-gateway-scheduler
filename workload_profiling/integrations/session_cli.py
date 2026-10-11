"""Login, inspect and close a reusable local LiteLLM session."""
import argparse
import json
from pathlib import Path
import sys

from ..common.paths import RESULTS
from ..simulation.config import CONFIG_PATH
from .session_control import SESSION_DIR, control
from .session_service import login


def main():
    actions = ("litellm-login", "litellm-status", "litellm-close")
    action = sys.argv[0].rsplit(" ", 1)[-1]
    parser = argparse.ArgumentParser(description=__doc__)
    if action not in actions:
        parser.add_argument("action", choices=actions)
        known, _ = parser.parse_known_args()
        action = known.action
    parser.add_argument("--session-dir", type=Path, default=SESSION_DIR,
                        help="Private local session control directory")
    if action == "litellm-login":
        parser.add_argument("--ssh-target", required=True, help="SSH user@host")
        parser.add_argument("--config", type=Path, default=CONFIG_PATH)
        parser.add_argument("--idle-timeout", type=float, default=600,
                            help="Close after N idle seconds; default 600; active batches do not expire")
        parser.add_argument("--proxy-port", type=int, default=4000)
        parser.add_argument("--timeout", type=float, default=60, help="HTTP/login timeout in seconds")
        parser.add_argument("--endpoint-update-interval", type=float, default=30)
        parser.add_argument("--endpoint-state-dir", type=Path, default=RESULTS / "endpoints")
        for name in ("rpm", "tpm", "concurrency"):
            parser.add_argument("--endpoint-" + name + "-limit", type=int,
                                help="Local capacity override for each discovered endpoint")
    args = parser.parse_args()
    try:
        if action == "litellm-login":
            print("Logging in and initializing endpoint state before accepting requests...", flush=True)
            result = login(directory=args.session_dir, ssh_target=args.ssh_target, config_path=args.config,
                idle_timeout=args.idle_timeout, timeout=args.timeout, proxy_port=args.proxy_port,
                endpoint_state_dir=args.endpoint_state_dir, endpoint_update_interval=args.endpoint_update_interval,
                endpoint_limit_overrides={name + "_limit": getattr(args, "endpoint_" + name + "_limit")
                    for name in ("rpm", "tpm", "concurrency") if getattr(args, "endpoint_" + name + "_limit") is not None})
            print("Login ready. Run replay-send repeatedly; litellm-close ends this session.")
        else:
            result = control(args.session_dir, "status" if action == "litellm-status" else "close")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TypeError, OSError) as error:
        parser.error(str(error))
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted. No automatic retry.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
