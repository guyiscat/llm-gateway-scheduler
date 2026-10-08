"""Unified project entry point; subcommands keep their own argument parsers."""
import argparse
import importlib
import sys


COMMANDS = {
    "replay": ("workload_profiling.simulation.cli", "回放请求并导出调度结果"),
    "web": ("workload_profiling.web.cli", "启动自适应调度控制台"),
}


def main():
    parser = argparse.ArgumentParser(description="大模型请求调度与离线分析")
    parser.add_argument("command", choices=COMMANDS,
                        help="；".join(f"{name}: {description}" for name, (_, description) in COMMANDS.items()))
    args = parser.parse_args(sys.argv[1:2])
    module = importlib.import_module(COMMANDS[args.command][0])
    original_argv = sys.argv
    sys.argv = [f"{parser.prog} {args.command}", *original_argv[2:]]
    try:
        module.main()
    finally:
        sys.argv = original_argv


if __name__ == "__main__":
    main()
