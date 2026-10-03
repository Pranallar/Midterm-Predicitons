"""Settings for the bot, read from environment variables and an optional `.env` file.

The API key is never hard-coded: set ``SUPERMARKET_API_KEY`` in your shell or in a
git-ignored ``.env`` file next to where you run the bot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional

DEFAULT_BASE_URL = "https://www.thesuper.market/api/v1"
PLACEHOLDER_KEY = "ace_your_key_here"

# Standard accounts get 100 reads and 30 writes per minute, shared by every key on
# the account. Stay a little under so other tools (or the website) keep working.
DEFAULT_READS_PER_MIN = 90
DEFAULT_WRITES_PER_MIN = 25


class ConfigError(RuntimeError):
    """Raised when required configuration (such as the API key) is missing."""


def parse_env_file(text: str) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Supports comments, blank lines, ``export`` and quotes."""
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            if end != -1:
                value = value[1:end]  # anything after the closing quote is a comment
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_env_file(path: Path) -> Dict[str, str]:
    """Read a ``.env`` file. Handles UTF-8 with or without BOM and UTF-16 (Windows Notepad)."""
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return {}
    except PermissionError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not UTF-8 text; re-save it as UTF-8") from exc
    return parse_env_file(text)


def _int(value: Optional[str], default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if parsed < 1:
        raise ConfigError(f"{name} must be at least 1, got {parsed}")
    return parsed


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    tournament: Optional[str] = None
    reads_per_min: int = DEFAULT_READS_PER_MIN
    writes_per_min: int = DEFAULT_WRITES_PER_MIN
    data_dir: Path = Path("data")

    def __repr__(self) -> str:  # never print the key
        return (
            f"Settings(api_key={mask_key(self.api_key)!r}, base_url={self.base_url!r}, "
            f"tournament={self.tournament!r}, reads_per_min={self.reads_per_min}, "
            f"writes_per_min={self.writes_per_min}, data_dir={str(self.data_dir)!r})"
        )

    __str__ = __repr__

    @classmethod
    def load(
        cls,
        env: Optional[Mapping[str, str]] = None,
        env_file: Optional[Path] = Path(".env"),
        **overrides: object,
    ) -> "Settings":
        """Build settings. Precedence: explicit overrides > environment > ``.env`` file."""
        merged: Dict[str, str] = {}
        if env_file is not None:
            merged.update(load_env_file(env_file))
        merged.update(os.environ if env is None else env)

        def get(name: str) -> Optional[str]:
            value = merged.get(name)
            return value.strip() if isinstance(value, str) and value.strip() else None

        api_key = overrides.get("api_key") or get("SUPERMARKET_API_KEY")
        if not api_key:
            hint = ""
            example = (env_file.parent if env_file is not None else Path(".")) / ".env.example"
            leaked = load_env_file(example).get("SUPERMARKET_API_KEY", "")
            if leaked and leaked != PLACEHOLDER_KEY:
                hint = (
                    " Your key is in .env.example, which is committed to git and is not read by the bot."
                    " Put it in a file named .env instead, and if .env.example was pushed to GitHub,"
                    " revoke that key and create a new one."
                )
            raise ConfigError(
                "No API key found. Set SUPERMARKET_API_KEY in your environment or in a "
                ".env file (see .env.example)." + hint
            )
        base_url = str(overrides.get("base_url") or get("SUPERMARKET_BASE_URL") or DEFAULT_BASE_URL)
        tournament = overrides.get("tournament") or get("SUPERMARKET_TOURNAMENT")
        data_dir = overrides.get("data_dir") or get("SUPERMARKET_DATA_DIR") or "data"
        reads = overrides.get("reads_per_min") or _int(
            get("SUPERMARKET_READS_PER_MIN"), DEFAULT_READS_PER_MIN, "SUPERMARKET_READS_PER_MIN"
        )
        writes = overrides.get("writes_per_min") or _int(
            get("SUPERMARKET_WRITES_PER_MIN"), DEFAULT_WRITES_PER_MIN, "SUPERMARKET_WRITES_PER_MIN"
        )
        return cls(
            api_key=str(api_key),
            base_url=base_url.rstrip("/"),
            tournament=str(tournament) if tournament else None,
            reads_per_min=int(reads),  # type: ignore[arg-type]
            writes_per_min=int(writes),  # type: ignore[arg-type]
            data_dir=Path(str(data_dir)),
        )


def mask_key(key: str) -> str:
    """Show only enough of a key to recognise it in logs."""
    if len(key) <= 10:
        return "***"
    return f"{key[:6]}…{key[-4:]}"
