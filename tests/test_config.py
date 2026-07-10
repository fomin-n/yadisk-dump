from __future__ import annotations

import json
import os
from pathlib import Path

from yadisk_dump.config import (
    delete_token,
    get_config_paths,
    load_download_dir,
    load_token,
    mask_token,
    save_download_dir,
    save_token,
    token_permissions,
)


def test_environment_credential_precedes_saved_file(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path), "YADISK_TOKEN": "environment-secret"}
    paths = get_config_paths(env)
    paths.directory.mkdir(parents=True)
    paths.token.write_text("file-secret", encoding="utf-8")
    assert load_token(env) == ("environment-secret", "env")


def test_save_load_and_delete_credential_securely(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    path = save_token("secret-value", env)
    assert load_token(env) == ("secret-value", "file")
    if os.name != "nt":
        assert token_permissions(path) == 0o600
    assert delete_token(env)
    assert not delete_token(env)


def test_download_directory_round_trip(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    destination = tmp_path / "данные"
    path = save_download_dir(destination, env)
    assert json.loads(path.read_text(encoding="utf-8"))["download_dir"] == str(
        destination.absolute()
    )
    assert load_download_dir(env) == destination.absolute()


def test_mask_never_returns_full_secret() -> None:
    secret = "y0_AgAlongCredential1234"
    masked = mask_token(secret)
    assert masked == "y0_AgA…1234"
    assert secret not in masked

