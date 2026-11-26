"""Entry point:  python -m nuclear_agent  [--no-open] [--port 8765]"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import logging.handlers
import sys
import threading
import webbrowser

from .config import Config


def _setup_logging(config: Config) -> None:
    log_dir = config.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_dir / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nuclear_agent", description="Nuclear Agent - local browser agent")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-open", action="store_true", help="don't open the control panel automatically")
    args = parser.parse_args(argv)

    config = Config()
    _setup_logging(config)
    host = config.get("server", "host")
    port = args.port or config.get("server", "port")
    if not _is_loopback(host):
        print(f"Refusing to bind to '{host}'. Nuclear Agent only listens on localhost.\n"
              "See docs/REMOTE_ACCESS.md for how to add a secure, authenticated remote bridge.")
        return 2

    import uvicorn

    from .server import create_app

    app = create_app(config)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Nuclear Agent control panel: {url}\n  Press Ctrl+C to quit.\n")
    if not args.no_open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except OSError as exc:
        print(f"Could not start server on port {port}: {exc}\nIs Nuclear Agent already running? Try --port 8766")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
