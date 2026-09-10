"""Makes `pytest` work from a clean checkout with no PYTHONPATH or install."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
