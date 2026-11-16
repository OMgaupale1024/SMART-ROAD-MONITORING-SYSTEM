"""The archived Arduino stack is not installed: put its src on the path for these tests only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
