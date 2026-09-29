"""Encrypted vault: everything (servers, passwords, keys, snippets) lives in one
file encrypted with AES-256-GCM. The key is derived from the master password
with scrypt, so the file is useless without it.

File layout:  b"SDV1" | salt(16) | n_log2(1) | nonce(12) | ciphertext+tag
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"SDV1"
DEFAULT_N_LOG2 = 17  # 2**17 -> ~128 MB, ~0.3 s: slow for brute force, fine for humans


class VaultError(Exception):
    pass


class WrongPassword(VaultError):
    pass


def _derive(password: str, salt: bytes, n_log2: int) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2 ** n_log2, r=8, p=1)
    return kdf.derive(password.encode("utf-8"))


class Vault:
    def __init__(self, path: Path, key: bytes, salt: bytes, n_log2: int):
        self.path = Path(path)
        self._key = key
        self._salt = salt
        self._n_log2 = n_log2

    # ---- lifecycle -------------------------------------------------------
    @staticmethod
    def exists(path: Path) -> bool:
        return Path(path).is_file()

    @classmethod
    def create(cls, path: Path, password: str, data: dict | None = None,
               n_log2: int = DEFAULT_N_LOG2) -> "Vault":
        if not password:
            raise VaultError("Master password cannot be empty")
        salt = os.urandom(16)
        v = cls(path, _derive(password, salt, n_log2), salt, n_log2)
        v.save(data or {})
        return v

    @classmethod
    def open(cls, path: Path, password: str) -> tuple["Vault", dict]:
        raw = Path(path).read_bytes()
        if len(raw) < 4 + 16 + 1 + 12 + 16 or raw[:4] != MAGIC:
            raise VaultError("Not a ShellDeck vault (or the file is corrupted)")
        salt = raw[4:20]
        n_log2 = raw[20]
        nonce = raw[21:33]
        ct = raw[33:]
        key = _derive(password, salt, n_log2)
        try:
            plain = AESGCM(key).decrypt(nonce, ct, MAGIC)
        except InvalidTag:
            raise WrongPassword("Wrong master password") from None
        return cls(path, key, salt, n_log2), json.loads(plain.decode("utf-8"))

    # ---- io -------------------------------------------------------------
    def save(self, data: dict) -> None:
        nonce = os.urandom(12)
        plain = json.dumps(data, separators=(",", ":")).encode("utf-8")
        ct = AESGCM(self._key).encrypt(nonce, plain, MAGIC)
        blob = MAGIC + self._salt + bytes([self._n_log2]) + nonce + ct
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)  # atomic: never leaves a half-written vault

    def change_password(self, new_password: str, data: dict) -> None:
        if not new_password:
            raise VaultError("Master password cannot be empty")
        self._salt = os.urandom(16)
        self._key = _derive(new_password, self._salt, self._n_log2)
        self.save(data)
