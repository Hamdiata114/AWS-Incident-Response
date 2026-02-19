"""Conftest for mcp/supervisor tests — adds parent dir to sys.path."""

import sys
from pathlib import Path

# Add mcp/supervisor to path so we can import tools.*, server, etc.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
