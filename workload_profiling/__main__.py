"""Unified project entry point; subcommands keep their own argument parsers."""
import argparse
import importlib
import sys


COMMANDS = {
    "replay-send": ("workload_profiling.integrations.replay_litellm", "复用登录会话，模拟 N 条请求并发送到 LiteLLM"),
    "litellm-login": ("workload_profiling.integrations.session_cli", "登录并启动可复用的后台会话"),
    "litellm-status": ("workload_profiling.integrations.session_cli", "查看会话与空闲超时状态"),
    "litellm-close": ("workload_profiling.integrations.session_cli", "停止后续发送并关闭登录会话"),
}


def main():
    parser = argparse.ArgumentParser(description="Endpoint 调度与本地路由交付记录")
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
