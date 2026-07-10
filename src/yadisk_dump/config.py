"""Configuration and OAuth-token storage."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

POLYGON_URL = "https://yandex.ru/dev/disk/poligon/"
ENV_TOKEN = "YADISK_TOKEN"


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    """Paths used for yadisk-dump's small global configuration."""

    directory: Path
    token: Path
    settings: Path


def get_config_paths(environ: dict[str, str] | None = None) -> ConfigPaths:
    """Return platform-appropriate config, token, and settings paths."""
    env = os.environ if environ is None else environ
    if os.name == "nt":
        root = Path(env.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(env.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = root.expanduser() / "yadisk-dump"
    return ConfigPaths(directory, directory / "token", directory / "config.json")


def load_token(environ: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """Load the OAuth token and return it with its source (``env`` or ``file``)."""
    env = os.environ if environ is None else environ
    token = env.get(ENV_TOKEN, "").strip()
    if token:
        return token, "env"

    path = get_config_paths(env).token
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, None
    if not token:
        return None, None
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return token, "file"


def save_token(token: str, environ: dict[str, str] | None = None) -> Path:
    """Save a validated OAuth token locally with owner-only permissions."""
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("token must not be empty")
    path = get_config_paths(environ).token
    _atomic_write(path, cleaned, 0o600 if os.name != "nt" else None)
    return path


def delete_token(environ: dict[str, str] | None = None) -> bool:
    """Delete the saved token file and return whether it existed."""
    path = get_config_paths(environ).token
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def load_download_dir(environ: dict[str, str] | None = None) -> Path | None:
    """Load the remembered download directory, if present and valid."""
    path = get_config_paths(environ).settings
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    value = data.get("download_dir") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


def save_download_dir(
    download_dir: Path, environ: dict[str, str] | None = None
) -> Path:
    """Remember the interactive flow's selected download directory."""
    path = get_config_paths(environ).settings
    payload = json.dumps(
        {"download_dir": str(download_dir.expanduser().absolute())},
        ensure_ascii=False,
        indent=2,
    )
    _atomic_write(path, f"{payload}\n", 0o600 if os.name != "nt" else None)
    return path


def mask_token(token: str) -> str:
    """Return a non-sensitive representation of a token for user interfaces."""
    if len(token) <= 10:
        return "•" * min(len(token), 8)
    return f"{token[:6]}…{token[-4:]}"


def token_permissions(path: Path) -> int | None:
    """Return POSIX permission bits for tests and diagnostics."""
    if os.name == "nt":
        return None
    return stat.S_IMODE(path.stat().st_mode)


def _atomic_write(path: Path, value: str, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path = Path(temporary_name)
        if mode is not None:
            temporary_path.chmod(mode)
        os.replace(temporary_path, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass

