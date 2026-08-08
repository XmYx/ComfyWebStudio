"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="comfywebstudio", description="ComfyWebStudio server")
    parser.add_argument("--host", default=None, help="Bind address (default: from settings)")
    parser.add_argument("--port", type=int, default=None, help="Port (default: from settings)")
    parser.add_argument("--reload", action="store_true", help="Reload on source changes")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    print(f"ComfyWebStudio on http://{host}:{port}  (state: {settings.root})")

    uvicorn.run(
        "comfywebstudio.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
