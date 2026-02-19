"""Conftest for data_processor tests — adds parent dir to sys.path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
