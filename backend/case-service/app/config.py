"""
Case Service Configuration
"""

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """配置类"""

    # 服务配置
    SERVICE_NAME: str = "case-service"
    SERVICE_PORT: int = 8001
    LOG_LEVEL: str = "INFO"

    # 数据库配置
    DATABASE_URL: str = "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot"

    # Redis配置 (可选，用于缓存)
    REDIS_URL: str = "redis://redis:6379/0"

    # KB Service 配置（Case 关闭时推送摘要，异步 fire-and-forget）
    KB_SERVICE_URL: str = "http://kb-service:8004"
    INTERNAL_API_TOKEN: str = "hci-dev-internal-token"
    KB_PUSH_ENABLED: bool = True  # 环境变量 KB_PUSH_ENABLED=false 可禁用

    # 日志收集制品存储（P2 段②：客户 PC → 平台 分块上传）
    # 单任务 2–20 GiB，使用本地共享卷；平台不做自动清理，容量告警由前端展示。
    LOG_COLLECT_STORAGE_ROOT: str = "/var/lib/log-collect"
    # 分片大小必须按「单请求耗时」反推，而不是按内存效率反推。
    #
    # 第一性原理：一个分片 = 一次 PUT = 一次完整的网关读窗口。网关（Traefik）
    # 读完整个请求（含 body）的时限默认只有 60s，客户侧上行常见 0.3–4 MB/s，
    # 于是 32 MiB 分片在 0.5 MB/s 链路上需要 64s —— 请求必然被网关掐断，
    # 表现为「平台返回 HTTP 502」，且重试永远停在同一片（Q2026102801062：
    # 72/103 片后再无进展，每轮 attempt 精确耗时 3×60s）。8 MiB 把单请求
    # 窗口压到 16–32s，即使网关超时未放宽也不会必然失败。
    # 代价是分片数 ×4（3.4 GB 包 103 → 409 片），只增加 HTTP 往返次数；
    # 服务端按文件逐片落盘、合并仍是流式，不产生额外内存或磁盘峰值。
    LOG_COLLECT_CHUNK_SIZE_BYTES: int = 8 * 1024 * 1024
    LOG_COLLECT_MAX_ARCHIVE_BYTES: int = 25 * 1024 * 1024 * 1024  # 25 GiB
    LOG_COLLECT_UPLOAD_TOKEN_TTL_HOURS: int = 24
    # 数据面 token 签名密钥；留空时由 INTERNAL_API_TOKEN 派生，避免新增必填配置。
    LOG_COLLECT_UPLOAD_SECRET: str = ""

    # P2 段③：平台代传日志服务器（与 acli 插件一致，插件只认官方域名白名单）。
    LOG_COLLECT_RELAY_URL: str = "http://acli.sangfor.com.cn:5888"
    LOG_COLLECT_RELAY_ENABLED: bool = True
    # 5888 的 ACLI_UPLOAD_TOKEN。留空 = 对方未启用鉴权（当前现网），此时不发认证头。
    # 切换顺序（不可颠倒）：先给本项配置真实值并部署平台侧 → 再在 5888 上设置
    # ACLI_UPLOAD_TOKEN 并重启。反过来会让所有代传立刻 401。
    LOG_COLLECT_RELAY_TOKEN: str = ""
    # 代传租约：running 状态必须靠心跳续租，超时即视为「执行者已死」可被抢占。
    # 心跳由独立协程发送，所以单个 300s 分片超时期间也在续租；本值只需覆盖
    # 「进程已死但没人写终态」的判定窗口。默认 120s 对应 30s 心跳。
    LOG_COLLECT_RELAY_STALE_SECONDS: int = 120

    model_config = ConfigDict(env_file=".env", case_sensitive=True, extra="ignore")


settings = Settings()
