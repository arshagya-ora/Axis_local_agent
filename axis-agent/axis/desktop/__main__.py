"""``python -m axis.desktop`` entry point."""
from __future__ import annotations

import sys

from axis.desktop.app import run

if __name__ == "__main__":
    sys.exit(run())
