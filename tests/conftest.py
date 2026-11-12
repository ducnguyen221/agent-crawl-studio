"""Test setup: make `src/` importable. Tests use the system Python with PYTHONPATH=src semantics,
only the standard library and pytest; they never create a virtual environment in the checkout."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
