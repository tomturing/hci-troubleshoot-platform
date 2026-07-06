"""
data-pipeline/raw_to_sop/config.py — 独立配置

与 kbd/config.py 完全独立，不 import 任何 kbd 模块。
仅需两个配置项：KB_SERVICE_URL 和 INTERNAL_API_TOKEN。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 优先加载同目录下的 .env（其次项目根目录）
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
else:
    load_dotenv(Path(__file__).parent.parent / ".env")


class Settings:
    """raw_to_sop 工具配置（从环境变量读取）。"""

    @property
    def KB_SERVICE_URL(self) -> str:
        return os.environ.get("KB_SERVICE_URL", "http://localhost:8004").rstrip("/")

    @property
    def INTERNAL_API_TOKEN(self) -> str:
        return os.environ.get("INTERNAL_API_TOKEN", "")

    @property
    def API_TIMEOUT(self) -> float:
        return float(os.environ.get("API_TIMEOUT", "120"))


settings = Settings()
