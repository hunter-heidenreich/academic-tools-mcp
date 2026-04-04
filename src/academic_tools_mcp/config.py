import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_env_path)


def get(key: str) -> str | None:
    """Get a config value from environment."""
    return os.environ.get(key) or None
