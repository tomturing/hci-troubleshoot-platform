"""密码哈希校验（argon2id）。

仅本服务提供 verify；首个管理员密码由 seed 脚本用 `hash_password` 预置。
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """生成 argon2id 哈希（用于 seed 首个管理员）。"""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """校验密码；任何异常（不匹配/哈希损坏）均返回 False。"""
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
