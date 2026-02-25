"""Conftest for resolver agent tests."""

import sys
from pathlib import Path

# Add lambda/ so 'shared' and 'resolver' packages are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
# Add lambda/resolver so bare 'schemas' import works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
