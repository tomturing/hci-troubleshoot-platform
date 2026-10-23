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
DEFAULT_MAX_FILES = 50_000
# 实测最深 3 层，留一层余量。
DEFAULT_MAX_DEPTH = 4


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
