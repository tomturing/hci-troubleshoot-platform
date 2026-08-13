"""Diagnostic Evidence Bundle（诊断证据包）信封加密。"""

import base64
import json
import os
import struct
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.errors import DiagnosisError

MAGIC_V1 = b"HCIEB1\n"
MAGIC_V2 = b"HCIEB2\n"
MAGIC = MAGIC_V2
ENCRYPTED_MAGICS = frozenset({MAGIC_V1, MAGIC_V2})
MAX_HEADER_BYTES = 64 * 1024
CHUNK_BYTES = 1024 * 1024


class EnvelopeEncryptionService:
    """使用 RSA-OAEP 封装 AES-256-GCM 数据密钥。"""

    def __init__(self, *, private_key_base64: str, key_id: str):
        try:
            key_bytes = base64.b64decode(private_key_base64, validate=True)
            private_key = serialization.load_pem_private_key(key_bytes, password=None)
        except (ValueError, TypeError) as exc:
            raise ValueError("DIAGNOSIS_ENCRYPTION_PRIVATE_KEY_B64 不是合法 PEM Base64") from exc
        if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 3072:
            raise ValueError("诊断包加密私钥必须是至少 3072 位 RSA 私钥")
        if not key_id.strip():
            raise ValueError("DIAGNOSIS_ENCRYPTION_KEY_ID 不能为空")
        self._private_key = private_key
        self.key_id = key_id.strip()

    def public_metadata(self) -> dict[str, str]:
        """返回采集端加密所需公钥和算法。"""

        public_pem = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        fingerprint = hashes.Hash(hashes.SHA256())
        fingerprint.update(public_pem)
        return {
            "algorithm": "AES-256-GCM",
            "key_wrap_algorithm": "RSA-OAEP-SHA256",
            "key_id": self.key_id,
            "public_key_pem_base64": base64.b64encode(public_pem).decode("ascii"),
            "public_key_fingerprint": fingerprint.finalize().hex(),
            "format": "HCIEB2",
        }

    def decrypt_file(self, source: Path, target: Path) -> dict[str, str]:
        """流式解密 HCIEB2，并兼容历史 HCIEB1 文件。"""

        try:
            with source.open("rb") as encrypted:
                magic = encrypted.read(len(MAGIC_V2))
                encrypted.seek(0)
                header = self._read_header(encrypted, expected_magic=magic)
                if header["key_id"] != self.key_id:
                    raise DiagnosisError(
                        code="ENCRYPTION_KEY_UNAVAILABLE",
                        message="证据包使用了未知或已轮换的加密密钥",
                        http_status=422,
                        details={"key_id": header["key_id"]},
                    )
                data_key = self._private_key.decrypt(
                    base64.b64decode(header["encrypted_data_key"], validate=True),
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None,
                    ),
                )
                if len(data_key) != 32:
                    raise ValueError("AES-256 数据密钥长度不合法")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if magic == MAGIC_V2:
                    self._decrypt_v2(encrypted, target, data_key, header)
                elif magic == MAGIC_V1:
                    decryptor = Cipher(
                        algorithms.AES(data_key),
                        modes.GCM(
                            base64.b64decode(header["nonce"], validate=True),
                            base64.b64decode(header["tag"], validate=True),
                        ),
                    ).decryptor()
                    with target.open("wb") as plaintext:
                        while chunk := encrypted.read(1024 * 1024):
                            plaintext.write(decryptor.update(chunk))
                        plaintext.write(decryptor.finalize())
                else:
                    raise ValueError("证据包加密 magic 不合法")
        except DiagnosisError:
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise DiagnosisError(
                code="BUNDLE_DECRYPTION_FAILED",
                message="证据包解密或完整性认证失败",
                http_status=422,
            ) from exc
        finally:
            if "data_key" in locals():
                data_key = b"\x00" * len(data_key)
        return {
            "algorithm": header["algorithm"],
            "key_wrap_algorithm": header["key_wrap_algorithm"],
            "key_id": header["key_id"],
            "format": "HCIEB2" if magic == MAGIC_V2 else "HCIEB1",
        }

    @staticmethod
    def encrypt_file(*, source: Path, target: Path, public_key_pem: bytes, key_id: str) -> dict[str, str]:
        """供测试及平台工具调用的 HCIEB2 分块流式加密实现。"""

        public_key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 3072:
            raise ValueError("加密公钥必须是至少 3072 位 RSA 公钥")
        data_key = os.urandom(32)
        nonce_prefix = os.urandom(4)
        encrypted_data_key = public_key.encrypt(
            data_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        try:
            header = {
                "algorithm": "AES-256-GCM",
                "key_wrap_algorithm": "RSA-OAEP-SHA256",
                "key_id": key_id,
                "encrypted_data_key": base64.b64encode(encrypted_data_key).decode("ascii"),
                "nonce_prefix": base64.b64encode(nonce_prefix).decode("ascii"),
                "chunk_size": str(CHUNK_BYTES),
                "plaintext_size": str(source.stat().st_size),
            }
            header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
            aesgcm = AESGCM(data_key)
            with source.open("rb") as plaintext, target.open("wb") as output:
                output.write(MAGIC_V2)
                output.write(struct.pack(">I", len(header_bytes)))
                output.write(header_bytes)
                counter = 0
                while chunk := plaintext.read(CHUNK_BYTES):
                    nonce = nonce_prefix + counter.to_bytes(8, "big")
                    aad = header_bytes + counter.to_bytes(8, "big")
                    sealed = aesgcm.encrypt(nonce, chunk, aad)
                    output.write(struct.pack(">I", len(sealed)))
                    output.write(sealed)
                    counter += 1
            return header
        finally:
            data_key = b"\x00" * len(data_key)

    @staticmethod
    def _read_header(source: BinaryIO, *, expected_magic: bytes) -> dict[str, str]:
        if expected_magic not in ENCRYPTED_MAGICS or source.read(len(expected_magic)) != expected_magic:
            raise ValueError("证据包加密 magic 不合法")
        length_bytes = source.read(4)
        if len(length_bytes) != 4:
            raise ValueError("证据包加密头缺失")
        header_length = struct.unpack(">I", length_bytes)[0]
        if not 1 <= header_length <= MAX_HEADER_BYTES:
            raise ValueError("证据包加密头长度不合法")
        header = json.loads(source.read(header_length))
        required = {
            "algorithm": "AES-256-GCM",
            "key_wrap_algorithm": "RSA-OAEP-SHA256",
        }
        if any(header.get(key) != value for key, value in required.items()):
            raise ValueError("证据包加密算法不受支持")
        format_keys = ("nonce_prefix", "chunk_size", "plaintext_size") if expected_magic == MAGIC_V2 else ("nonce", "tag")
        for key in ("key_id", "encrypted_data_key", *format_keys):
            if not isinstance(header.get(key), str) or not header[key]:
                raise ValueError(f"证据包加密头缺少 {key}")
        return header

    @staticmethod
    def _decrypt_v2(source: BinaryIO, target: Path, data_key: bytes, header: dict[str, str]) -> None:
        """逐块认证后写入明文，任一块损坏即拒绝整个证据包。"""

        nonce_prefix = base64.b64decode(header["nonce_prefix"], validate=True)
        chunk_size = int(header["chunk_size"])
        expected_size = int(header["plaintext_size"])
        if len(nonce_prefix) != 4 or chunk_size != CHUNK_BYTES or not 0 <= expected_size <= 512 * 1024 * 1024:
            raise ValueError("HCIEB2 分块参数不合法")
        header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        aesgcm = AESGCM(data_key)
        written = 0
        counter = 0
        with target.open("wb") as plaintext:
            while True:
                length_bytes = source.read(4)
                if not length_bytes:
                    break
                if len(length_bytes) != 4:
                    raise ValueError("HCIEB2 分块长度缺失")
                sealed_length = struct.unpack(">I", length_bytes)[0]
                if not 16 <= sealed_length <= chunk_size + 16:
                    raise ValueError("HCIEB2 分块长度不合法")
                sealed = source.read(sealed_length)
                if len(sealed) != sealed_length:
                    raise ValueError("HCIEB2 分块内容被截断")
                nonce = nonce_prefix + counter.to_bytes(8, "big")
                aad = header_bytes + counter.to_bytes(8, "big")
                chunk = aesgcm.decrypt(nonce, sealed, aad)
                plaintext.write(chunk)
                written += len(chunk)
                counter += 1
        if written != expected_size:
            raise ValueError("HCIEB2 明文大小与加密头不一致")
