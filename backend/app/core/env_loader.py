import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_ROOT / "backend"
DEFAULT_ENV_FILE = BACKEND_DIR / ".env"


def resolve_backend_env_file() -> Path:
    env_file_value = os.getenv("BACKEND_ENV_FILE", "").strip()
    if env_file_value:
        env_file = Path(env_file_value)
        if not env_file.is_absolute():
            env_file = PROJECT_ROOT / env_file
        return env_file
    return DEFAULT_ENV_FILE


def load_backend_env() -> Path:
    env_file = resolve_backend_env_file()
    load_dotenv(env_file, override=False)
    return env_file
