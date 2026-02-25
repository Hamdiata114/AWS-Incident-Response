"""Conftest for mcp/resolver tests — adds parent dir and repo root to sys.path."""

import sys
from pathlib import Path

# Add mcp/resolver to path so we can import tools.*, server, etc.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Add repo root so config.baseline is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
