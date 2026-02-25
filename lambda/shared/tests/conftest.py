"""Conftest for shared tests — adds lambda/ to sys.path."""

import sys
from pathlib import Path

# Add lambda/ so 'shared' package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
