"""Encrypted storage of the Telethon StringSession (HISTORY_INGESTION §5.3).

The session grants FULL access to the Telegram account. It is Fernet-encrypted at rest with a key
that lives only in the environment, and the file is created with mode 0600.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SessionStoreError(RuntimeError):
    pass


def default_session_path(data_dir: Path) -> Path:
    return data_dir / "secrets" / "mtproto.session.enc"


def save_session(path: Path, session_string: str, key: str) -> None:
    token = Fernet(key.encode()).encrypt(session_string.encode())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_session(path: Path, key: str) -> str:
    if not path.exists():
        raise SessionStoreError(f"no MTProto session at {path}; run `elysium-bot mtproto login`")
    if path.stat().st_mode & 0o077:
        raise SessionStoreError(f"{path} is readable by other users; run chmod 600 on it")
    try:
        return Fernet(key.encode()).decrypt(path.read_bytes()).decode()
    except InvalidToken as exc:
        raise SessionStoreError(
            "cannot decrypt MTProto session (wrong ELYSIUM_MTPROTO_SESSION_KEY?)"
        ) from exc


def delete_session(path: Path) -> None:
    path.unlink(missing_ok=True)
