# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""The single application account.

Stored as ``workspace/auth/account.json`` containing only the username and
the Argon2id hash. Created and changed exclusively through the server-side
CLI (``afmlabel auth set-password``); there is no web registration.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from afmlabel.auth.passwords import DUMMY_HASH, hash_password, verify_password
from afmlabel.domain.common import utc_now_iso
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import load_model, save_model

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


class Account(BaseModel):
    schema_version: int = 1
    username: str
    password_hash: str
    algorithm: str = "argon2id"
    created_at: str
    updated_at: str


class AccountStore:
    def __init__(self, workspace_root: Path) -> None:
        self._path = paths.account_file(workspace_root)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Account | None:
        if not self._path.exists():
            return None
        return load_model(self._path, Account)

    def is_configured(self) -> bool:
        return self._path.exists()

    def set_password(self, username: str, password: str) -> Account:
        username = username.strip()
        if not _USERNAME_PATTERN.match(username):
            raise ValueError("username must be 1-64 characters of letters, digits, . _ @ -")
        existing = self.load()
        now = utc_now_iso()
        account = Account(
            username=username,
            password_hash=hash_password(password),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        save_model(self._path, account)
        try:
            self._path.chmod(0o600)
        except OSError:  # pragma: no cover - Windows ignores POSIX modes
            pass
        return account

    def verify(self, username: str, password: str) -> bool:
        account = self.load()
        if account is None or account.username != username.strip():
            verify_password(DUMMY_HASH, password)
            return False
        return verify_password(account.password_hash, password)
