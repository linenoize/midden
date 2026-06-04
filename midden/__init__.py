"""Midden — read-only archivist for inherited digital chaos."""
from .env import load_dotenv as _load_dotenv

__version__ = "0.1.0"

# Load .env from the repo root on import so the LLM API key (and any overrides)
# are available to every entry point without an explicit call. Real environment
# variables take precedence; a missing .env is a no-op.
_load_dotenv()
