#!/usr/bin/env python3
"""Entry point for the app-demo-recorder CLI (see adrd/cli.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from adrd.cli import main  # noqa: E402
except ModuleNotFoundError as exc:
    if exc.name in ("PIL", "imageio_ffmpeg"):
        raise SystemExit(f"missing dependency {exc.name!r} for {sys.executable}\n"
                         f"use the project wrapper: ../adrd   (or run ./setup.sh)")
    raise

if __name__ == "__main__":
    raise SystemExit(main())
