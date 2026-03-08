"""Mash API example serving the simple app definition over HTTP."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from mash_api import MashAPIConfig, run_app

from .app_definition import HelloMashDefinition


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run mash-api for the HelloMash definition.")
    parser.add_argument("--root", default=".", help="Working directory for sqlite/log files.")
    parser.add_argument("--host", default="127.0.0.1", help="API bind host")
    parser.add_argument("--port", type=int, default=8000, help="API bind port")
    parser.add_argument("--api-key", default=None, help="Optional API key")
    args = parser.parse_args(argv)

    run_app(
        HelloMashDefinition(Path(args.root).resolve()),
        config=MashAPIConfig(
            bind_host=args.host,
            bind_port=args.port,
            api_key=args.api_key,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
