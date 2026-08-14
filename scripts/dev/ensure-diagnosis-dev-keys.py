#!/usr/bin/env python3
"""为本地离线诊断生成并持久化证据包加密密钥。"""

import argparse
import base64
import os
import re
import shutil
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PRIVATE_KEY_NAME = "DIAGNOSIS_ENCRYPTION_PRIVATE_KEY_B64"
KEY_ID_NAME = "DIAGNOSIS_ENCRYPTION_KEY_ID"
DEFAULT_KEY_ID = "local-dev-diagnosis-encryption-v1"


def read_value(content: str, name: str) -> str:
    """读取 dotenv 中最后一个有效同名变量。"""

    matches = re.findall(rf"^{re.escape(name)}=(.*)$", content, flags=re.MULTILINE)
    return matches[-1].strip() if matches else ""


def replace_value(content: str, name: str, value: str) -> str:
    """更新 dotenv 变量；不存在时追加。"""

    pattern = re.compile(rf"^{re.escape(name)}=.*$", flags=re.MULTILINE)
    if pattern.search(content):
        return pattern.sub(f"{name}={value}", content)
    separator = "" if not content or content.endswith("\n") else "\n"
    return f"{content}{separator}{name}={value}\n"


def validate_private_key(value: str) -> None:
    """拒绝继续使用损坏或强度不足的本地加密私钥。"""

    try:
        key = serialization.load_pem_private_key(base64.b64decode(value, validate=True), password=None)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{PRIVATE_KEY_NAME} 不是合法的 PEM Base64") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise SystemExit(f"{PRIVATE_KEY_NAME} 必须是至少 3072 位 RSA 私钥")


def atomic_write(path: Path, content: str) -> None:
    """以原权限原子替换 dotenv，避免中途留下半个私钥。"""

    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
        output.write(content)
        temporary = Path(output.name)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def main() -> None:
    """保证本地 dotenv 同时存在匹配的 RSA 私钥和密钥标识。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--example-file", default=".env.example")
    arguments = parser.parse_args()
    env_file = Path(arguments.env_file)
    if not env_file.exists():
        example = Path(arguments.example_file)
        if not example.is_file():
            raise SystemExit(f"环境文件和模板均不存在：{env_file} / {example}")
        shutil.copyfile(example, env_file)
        os.chmod(env_file, 0o600)

    content = env_file.read_text(encoding="utf-8")
    private_key = read_value(content, PRIVATE_KEY_NAME)
    key_id = read_value(content, KEY_ID_NAME)
    if private_key:
        validate_private_key(private_key)
    else:
        generated = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private_pem = generated.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        private_key = base64.b64encode(private_pem).decode("ascii")
    if not key_id:
        key_id = DEFAULT_KEY_ID

    updated = replace_value(content, PRIVATE_KEY_NAME, private_key)
    updated = replace_value(updated, KEY_ID_NAME, key_id)
    if updated != content:
        atomic_write(env_file, updated)
        print(f"已生成并写入本地离线诊断加密密钥：key_id={key_id}")
    else:
        print(f"本地离线诊断加密密钥已就绪：key_id={key_id}")


if __name__ == "__main__":
    main()
