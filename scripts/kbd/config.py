"""
scripts/kbd/config.py — KBD 知识生产管道配置

所有配置通过环境变量注入（或 .env 文件）。
脚本目录下运行：cp .env.example .env && nano .env
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent.parent.parent


class KbdSettings(BaseSettings):
    """KBD 管道配置"""

    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / "scripts" / "kbd" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 抓取目标 ─────────────────────────────────────────────────────────────
    SANGFOR_API_BASE: str = Field(
        default="https://support.sangfor.com.cn",
        description="深信服技术支持门户 Base URL",
    )
    # Cookie 字符串（从浏览器 DevTools → Network → 任意请求 → Request Headers → Cookie 复制）
    # 包含：PHPSESSID, _pk_id.*, visitor_id, Hm_lvt_*, HMACCOUNT, _pk_ses.*, Hm_lpvt_*
    SANGFOR_COOKIE: str = Field(
        default="",
        description="认证 Cookie 字符串（必填）",
    )
    # 请求间隔（秒）——避免触发限流
    SANGFOR_REQUEST_DELAY: float = Field(default=0.8, ge=0.2, le=10.0)
    # 单条请求超时（秒）
    SANGFOR_TIMEOUT: float = Field(default=30.0)
    # 最大重试次数（指数退避）
    SANGFOR_MAX_RETRIES: int = Field(default=4)

    # ── Excel 输入 ───────────────────────────────────────────────────────────
    EXCEL_FILE: Path = Field(
        default=_PROJECT_ROOT / "案例生产详细数据24-26.xlsx",
        description="包含案例 ID 的 Excel 文件路径（第一列为案例ID，跳过标题行）",
    )

    # ── 本地存储 ─────────────────────────────────────────────────────────────
    # KBD 抓取缓存目录（每个案例一个子目录 cache/{support_id}/）
    KBD_CACHE_DIR: Path = Field(
        default=_PROJECT_ROOT / "scripts" / "kbd" / "cache",
        description="KBD 缓存根目录，结构为 {KBD_CACHE_DIR}/{support_id}/raw.json + img_N.png 等",
    )
    # KBD Pipeline logs 目录（保存 kbd_{run_id}.log 和 progress_{run_id}.json）
    KBD_LOGS_DIR: Path = Field(
        default=_PROJECT_ROOT / "scripts" / "kbd" / "logs",
        description="KBD pipeline logs 目录，保存 kbd_{run_id}.log 和 progress_{run_id}.json",
    )
    # category_baseline.yaml 路径
    CATEGORY_BASELINE: Path = Field(
        default=_PROJECT_ROOT / "backend" / "kb-service" / "config" / "category_baseline.yaml",
    )

    # ── 数据库 ────────────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        default="postgresql://hci_user:hci_pass@localhost:5432/hci_db",
        description="asyncpg 连接字符串（postgresql://...）",
    )
    DB_POOL_MIN: int = Field(default=2)
    DB_POOL_MAX: int = Field(default=10)

    # ── LLM（Vision 兜底 + 分析）───────────────────────────────────────────────
    # 旧字段保留向后兼容（用于 Vision 兜底 OCR，模型换为 DashScope qwen3.5-plus）
    ZAI_API_KEY: str = Field(default="", description="DashScope API Key（必填，替代旧 z.ai key）")
    ZAI_BASE_URL: str = Field(
        default="https://coding.dashscope.aliyuncs.com/v1",
        description="DashScope OpenAI-compatible API Base URL",
    )
    # Vision 兜底模型（支持图片输入，PaddleOCR 失败时启用）
    VISION_MODEL: str = Field(
        default="qwen3.5-plus",
        description="Vision 兜底 OCR 模型（需支持 image_url 输入，DashScope qwen3.5-plus）",
    )
    # 分类模型（保留兼容）
    CLASSIFY_MODEL: str = Field(
        default="qwen3.5-plus",
        description="分类 LLM 模型名称",
    )
    # LLM 请求超时（秒）
    LLM_TIMEOUT: float = Field(default=60.0)
    # Vision 输出最大 token 数（DashScope qwen3.5-plus 支持 2048；BigModel glm-4v-flash 上限 1024）
    VISION_MAX_TOKENS: int = Field(default=2048, ge=128, le=8192)
    # Vision 并发数（控制每个案例的图片并行处理数）
    VISION_CONCURRENCY: int = Field(default=3, ge=1, le=10)

    # ── 管道行为 ──────────────────────────────────────────────────────────────
    # 最低图片文件大小（字节），小于此值视为无效图片（icon/占位图）
    MIN_IMAGE_SIZE: int = Field(default=2048)
    # AI 分类置信度阈值：低于此值时在 draft 标记"需人工重新分类"
    MIN_CLASSIFY_CONFIDENCE: float = Field(default=0.5, ge=0.0, le=1.0)

    # ── kb-service API（数据管道调用）──────────────────────────────────────────
    KB_SERVICE_URL: str = Field(
        default="http://localhost:8004",
        description="kb-service 内部 API 地址",
    )
    INTERNAL_API_TOKEN: str = Field(
        default="hci-dev-internal-token",
        description="内部服务认证 Token（Bearer Token）",
    )
    # API 请求超时（秒）
    API_TIMEOUT: float = Field(default=30.0)
    # API 最大重试次数
    API_MAX_RETRIES: int = Field(default=3)

    @field_validator("KBD_CACHE_DIR", "KBD_LOGS_DIR", "EXCEL_FILE", "CATEGORY_BASELINE", mode="before")
    @classmethod
    def _to_path(cls, v: str | Path) -> Path:
        return Path(v)

    @property
    def sangfor_detail_url(self) -> str:
        return f"{self.SANGFOR_API_BASE}/spt/openapi/case/es/getDetailById"

    @property
    def sangfor_headers(self) -> dict[str, str]:
        """抓取时所需的 HTTP 请求头"""
        return {
            "Accept": "application/vnd.edusoho.v2+json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Cookie": self.SANGFOR_COOKIE,
            "Http_x_requested_with": "xmlhttprequest",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.SANGFOR_API_BASE}/cases/list?product_id=33&type=1",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
            ),
        }


settings = KbdSettings()