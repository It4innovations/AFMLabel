# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Argon2id password hashing."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 8

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


class WeakPasswordError(ValueError):
    pass


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"password must be at least {MIN_PASSWORD_LENGTH} characters long")
    if password.strip() != password:
        raise WeakPasswordError("password must not start or end with whitespace")


def hash_password(password: str) -> str:
    validate_password_strength(password)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


#: A valid hash of a random string, verified against wrong passwords when no
#: account exists or the username does not match, so response timing does
#: not reveal whether the username is right.
DUMMY_HASH = _hasher.hash("afmlabel-dummy-password-for-constant-time-verify")
