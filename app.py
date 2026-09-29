"""Vercel entrypoint: Vercel looks for a FastAPI instance named `app` in app.py at the project root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from tpf.api.main import app  # noqa: E402,F401
