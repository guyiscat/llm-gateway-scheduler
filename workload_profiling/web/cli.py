"""Start the local adaptive scheduling console."""
import argparse
from .simulation_console import serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        serve(args.port, open_browser=args.open)
    except OSError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
