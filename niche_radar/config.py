from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "radar.toml"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def load_config(path: Path | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, "rb") as f:
        return tomllib.load(f)


def load_api_key(env_file: Path | None = None) -> str:
    """Environment variable wins; otherwise YOUTUBE_API_KEY=... from the project .env file."""
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    env_file = env_file or PROJECT_ROOT / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "YOUTUBE_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        raise SystemExit(
            "YOUTUBE_API_KEY не найден. Создайте youtube-project/.env со строкой "
            "YOUTUBE_API_KEY=... (образец — .env.example)."
        )
    return key
