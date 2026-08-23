"""Command-line entry point for Daily Chip News."""

from __future__ import annotations

import sys
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from daily_chip_news.app import main  # noqa: E402


if __name__ == "__main__":
    main()
