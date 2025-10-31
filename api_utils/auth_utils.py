from __future__ import annotations

import os
from pathlib import Path
from typing import Set

API_KEYS: Set[str] = set()

_DEFAULT_KEY_FILE_PATH = Path(__file__).resolve().parent.parent / "auth_profiles" / "key.txt"
_AUTH_KEY_ENV = "AUTH_KEY_FILE_PATH"


def _resolve_key_file_path() -> Path:
    override = os.environ.get(_AUTH_KEY_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_KEY_FILE_PATH


def load_api_keys() -> None:
    """Load API keys from disk into the in-memory cache."""
    global API_KEYS
    API_KEYS.clear()

    key_file_path = _resolve_key_file_path()
    if not key_file_path.exists():
        return

    with key_file_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            key = line.strip()
            if key:
                API_KEYS.add(key)


def initialize_keys() -> None:
    """Initialise API keys, respecting environment overrides."""
    key_file_path = _resolve_key_file_path()
    if not key_file_path.exists():
        if os.environ.get(_AUTH_KEY_ENV):
            raise FileNotFoundError(
                f"AUTH_KEY_FILE_PATH points to '{key_file_path}', but the file does not exist."
            )
        key_file_path.parent.mkdir(parents=True, exist_ok=True)
        key_file_path.touch()

    load_api_keys()


def verify_api_key(api_key_from_header: str) -> bool:
    """
    Verify the supplied API key.

    Returns True if API_KEYS is empty (validation disabled) or if the key is valid.
    """
    if not API_KEYS:
        return True
    return api_key_from_header in API_KEYS
