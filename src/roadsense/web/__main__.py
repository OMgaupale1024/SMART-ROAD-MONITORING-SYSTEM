"""Web Server entry point: python -m roadsense.web [--host HOST] [--port PORT] [--no-browser]."""
import argparse

from roadsense.web.server import run_web


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m roadsense.web", description="RoadSense web dashboard and API server.")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="address to listen on (default: 127.0.0.1, this computer only). 0.0.0.0 lets every "
             "device on the network open the dashboard; there is no login.")
    parser.add_argument("--port", type=int, default=8000, help="TCP port (default: 8000)")
    parser.add_argument(
        "--no-browser", action="store_true",
        help="don't open a web browser (headless Raspberry Pi, systemd service)")
    args = parser.parse_args(argv)
    run_web(host=args.host, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
