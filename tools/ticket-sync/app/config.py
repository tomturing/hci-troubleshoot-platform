"""配置加载：全部来自环境变量，支持从 env 文件读取。

配置文件只存宿主机本地（设计文档 10.2），不入库、不进集群 Secret。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = Path("/etc/hci-ticket-sync/ticket-sync.env")

# 单工单上限 20 GiB：与设计文档第 3 节的体积分布对齐，超过即判定异常来源。
DEFAULT_MAX_ARCHIVE_BYTES = 20 * 1024**3
# 展开上限 400 GiB：实测最大展开比 20x，20 GiB * 20 即此值。
DEFAULT_MAX_EXTRACT_BYTES = 400 * 1024**3
# 剩余红线 60 GiB：低于此值拒绝新任务，避免把宿主机写满。
DEFAULT_MIN_FREE_BYTES = 60 * 1024**3
# 实测单工单最多 176,562 个文件（Q2026103000409），旧值 5 万会误杀正常包。
# 取 50 万给「节点更多、日志轮转更频繁」的集群留 2.8 倍余量，同时仍是有限值：
# 它防的是恶意归档打爆 inode，不是用来判断包是否正常。
DEFAULT_MAX_FILES = 500_000
# 实测最深 7 层：zip → sp tgz → cmd-output tar.gz → host log_N.tar.gz → 叶子包。
# 旧值 4 层（注释还写「实测最深 3 层」）与生产实际完全脱节。
# 该值是「归档嵌套轮次」，不是文件系统目录深度（实测目录可达 18 层）。
# 超限只停止继续下钻并保留已解产物，不整单失败，故 12 层余量足够。
DEFAULT_MAX_DEPTH = 12
# 单块磁盘上并发更高只会互相拖慢，2 是吞吐与响应性的折中。
DEFAULT_CONCURRENCY = 2
DEFAULT_AUTO_SYNC_INTERVAL_SECONDS = 30
DEFAULT_RETENTION_DAYS = 7
# 产物总量上限 1 TiB：宿主机盘已扩到 2.1T，需给系统、镜像与 .tmp 留余量。
# 旧值 400 GiB 在单机 500+ GiB 产物时就会触发淘汰，与实盘容量不匹配。
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024**3
# 容量淘汰的最短保留天数，避免"刚同步完就被容量逻辑删掉"。
DEFAULT_MIN_KEEP_DAYS = 3


@dataclass(frozen=True)
class Config:
    """运行期配置。所有路径均为宿主机绝对路径。"""

    source_host: str
    source_port: int
    source_user: str
    ssh_key: Path
    archive_password: str
    root: Path
    listen_host: str
    listen_port: int
    max_archive_bytes: int
    max_extract_bytes: int
    min_free_bytes: int
    max_files: int
    max_depth: int
    concurrency: int
    auto_sync_enabled: bool
    auto_sync_interval_seconds: int
    retention_days: int
    # 产物总量上限：超过后按"最旧优先"淘汰，默认 400 GiB。
    # 单一保留天数是不够的：实测客户一天能推来 5 个 50~65 GiB 的工单，
    # 7 天内就能把 1.2T 的盘填满，届时只会拒绝新任务而不会自愈。
    max_total_bytes: int = 400 * 1024**3
    # 容量淘汰的最短保留天数：不足该天数即使超限也不删，避免刚同步就被清掉。
    min_keep_days: int = 3

    @property
    def source(self) -> str:
        """rsync 远端标识，供命令行拼接。"""
        return f"{self.source_user}@{self.source_host}"

    def remote_archive(self, ticket_id: str) -> str:
        """工单归档在 0.2 上的绝对路径。"""
        return f"/sf/log/{ticket_id}/{ticket_id}.zip"

    def tmp_dir(self) -> Path:
        return self.root / ".tmp"

    def trash_dir(self) -> Path:
        return self.root / ".trash"

    def state_db(self) -> Path:
        """任务历史与产物用量的 SQLite 文件。

        放在产物根目录下：与产物同盘便于一起备份/迁移；点开头不会被
        DSH 文件选择器展示，也不匹配 `Q\\d{13}` 因此不会被清理逻辑误删。
        """
        return self.root / ".ticket-sync.db"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Config:
        """先加载 env 文件（若存在），再读环境变量覆盖。

        显式传入的进程环境优先级最高，便于临时覆盖做验证。
        """
        values: dict[str, str] = {}
        target = env_file or DEFAULT_ENV_FILE
        if target.is_file():
            values.update(_parse_env_file(target))
        values.update({k: v for k, v in os.environ.items() if k.startswith("TICKET_")})

        def optional(key: str, default: str) -> str:
            return values.get(key, "").strip() or default

        return cls(
            source_host=optional("TICKET_SOURCE_HOST", "192.168.0.2"),
            source_port=int(optional("TICKET_SOURCE_PORT", "22468")),
            source_user=optional("TICKET_SOURCE_USER", "acli"),
            ssh_key=Path(optional("TICKET_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519"))),
            # 密码允许为空：dry-run 与配置检查不需要它，真正解压前再校验。
            archive_password=optional("TICKET_ARCHIVE_PASSWORD", ""),
            root=Path(optional("TICKET_ROOT", "/sf/tickets")),
            listen_host=optional("TICKET_LISTEN_HOST", "192.168.0.4"),
            listen_port=int(optional("TICKET_LISTEN_PORT", "18080")),
            max_archive_bytes=int(optional("TICKET_MAX_ARCHIVE_BYTES", str(DEFAULT_MAX_ARCHIVE_BYTES))),
            max_extract_bytes=int(optional("TICKET_MAX_EXTRACT_BYTES", str(DEFAULT_MAX_EXTRACT_BYTES))),
            min_free_bytes=int(optional("TICKET_MIN_FREE_BYTES", str(DEFAULT_MIN_FREE_BYTES))),
            max_files=int(optional("TICKET_MAX_FILES", str(DEFAULT_MAX_FILES))),
            max_depth=int(optional("TICKET_MAX_DEPTH", str(DEFAULT_MAX_DEPTH))),
            concurrency=int(optional("TICKET_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
            auto_sync_enabled=_as_bool(optional("TICKET_AUTO_SYNC_ENABLED", "true")),
            auto_sync_interval_seconds=int(
                optional("TICKET_AUTO_SYNC_INTERVAL_SECONDS", str(DEFAULT_AUTO_SYNC_INTERVAL_SECONDS))
            ),
            retention_days=int(optional("TICKET_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))),
            max_total_bytes=int(optional("TICKET_MAX_TOTAL_BYTES", str(DEFAULT_MAX_TOTAL_BYTES))),
            min_keep_days=int(optional("TICKET_MIN_KEEP_DAYS", str(DEFAULT_MIN_KEEP_DAYS))),
        )


def _parse_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 格式，忽略空行与 # 注释。

    不引入 dotenv：格式足够简单，且避免为一个读取动作增加依赖。
    """
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _as_bool(value: str) -> bool:
    """解析布尔配置，非法值按关闭处理并交给启动日志暴露。"""
    return value.strip().lower() in {"1", "true", "yes", "on"}
