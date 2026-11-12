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

# ─────────────────────────── 磁盘水位双阈值 ───────────────────────────
# 两个阈值都表达"剩余空间"，且**软阈值必须大于硬阈值**，否则淘汰还没来得及
# 腾出空间，任务就已经撞上熔断——这正是 2026-11-12 全栈中断的形态：
# 那一次没有任何一个"提前腾空间"的动作，只有写满之后的失败。
#
# 反推依据（每一项都是可复算的事实，不是拍脑袋的经验值）：
#   1. kubelet 驱逐线：nodefs.available 阈值实测 114,853,190,729 B ≈ 107 GiB。
#      跌破它 Pod 就被驱逐，全栈一起挂——这是绝对不能碰的地板。
#   2. 单任务峰值写入：归档上限 20 GiB（实测最大 27 GiB，按 30 计）
#      + 单层解压增量 ≤ 60 GiB ⇒ 约 90 GiB。
#   3. 并发峰值：自动任务 2 并发 ⇒ 180 GiB，但双阈值是按"1 个任务还能安全起跑"
#      设计的，取 90 GiB 即可，余下的差额由软阈值兜住。
#
# 硬阈值（熔断）= 107（驱逐线）+ 90（单任务峰值）+ 53（安全边际）≈ 250 GiB。
# 低于它直接拒绝新任务，一个字节都不写。宁可失败也不把整栈拖下水。
DEFAULT_MIN_FREE_BYTES = 250 * 1024**3
# 软阈值（主动淘汰）= 硬阈值 250 + 150（一轮淘汰要能腾出 1.5 个任务的空间）≈ 400 GiB。
# 低于它每轮维护立刻按"非日志优先、最旧优先"删产物，把余量拉回阈值之上。
# 差额 150 GiB 就是用户要的"降低失败概率"的物理来源：
# 淘汰先动手，熔断就只在极端突发（半天涌入数百 GiB）时才会被触发。
DEFAULT_EVICT_FREE_BYTES = 400 * 1024**3
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
# 保留期 3 天。判据是"同步完成时刻"，不是工单号里的日期——客户可能把历史工单
# 隔几天再上传，按工单号日期算会把它判成早就过期。
# 取 3 天而不是 7 天的依据：保留期的目的不是"存数据"（远端 /sf/log 留有原件、
# 有独立备份，可随时重拉），而是"避免重复拉取"。绝大多数排障在 48 小时内完成，
# 3 天覆盖它且把稳态在途量从 7 天压到 3 天，直接对应磁盘占用减半。
DEFAULT_RETENTION_DAYS = 3
# 产物总量上限 1 TiB：宿主机盘已扩到 2.1T，需给系统、镜像与 .tmp 留余量。
# 旧值 400 GiB 在单机 500+ GiB 产物时就会触发淘汰，与实盘容量不匹配。
# 注意这是**第三重**保险（已用量上限），主控是上面两个剩余空间阈值：
# 已用量是代理指标，磁盘剩余才是唯一真值，两者冲突时以剩余空间为准。
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024**3
# 容量淘汰的最短保留天数。必须**小于**保留期，否则保留期永远轮不到执行：
# 淘汰在 3 天就允许动手，而保留期要 7 天才动手，结果保留期一个都删不掉
# （2026-11-12 实测 expired_removed_total 恒为 0，365 GiB 全是容量淘汰干的）。
# 取 1 天：刚同步完的产物当天不动，隔天起可被水位压力淘汰。
DEFAULT_MIN_KEEP_DAYS = 1

# 失败任务补扫窗口默认与保留期一致：扫得比保留期还宽没有意义（产物早被删了），
# 扫得更窄则会漏掉前几天的失败任务。
DEFAULT_AUTO_SCAN_DAYS = 7


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
    # 硬阈值：剩余空间低于它直接拒绝新任务，一个字节都不写。
    min_free_bytes: int
    max_files: int
    max_depth: int
    concurrency: int
    auto_sync_enabled: bool
    auto_sync_interval_seconds: int
    retention_days: int
    # 软阈值：剩余空间低于它就主动淘汰最旧产物。必须大于 min_free_bytes，
    # 否则"淘汰"来不及在"熔断"之前生效。构造时校验，见 _validate_waterline。
    evict_free_bytes: int = DEFAULT_EVICT_FREE_BYTES
    # 产物总量上限：超过后按"最旧优先"淘汰，默认 400 GiB。
    # 单一保留天数是不够的：实测客户一天能推来 5 个 50~65 GiB 的工单，
    # 7 天内就能把 1.2T 的盘填满，届时只会拒绝新任务而不会自愈。
    max_total_bytes: int = 400 * 1024**3
    # 容量淘汰的最短保留天数：不足该天数即使超限也不删，避免刚同步就被清掉。
    min_keep_days: int = 3
    # 失败任务补扫的回看窗口（天）。常规扫描永远只看当天；这个窗口只用来找回
    # "曾经尝试过、失败过"的工单（判据是 retries 表），**不是**把窗口内所有历史
    # 归档都补拉一遍——2026-11-01 正是那样一次性入队 57 个工单、吃掉 766 GiB。
    auto_scan_days: int = DEFAULT_AUTO_SCAN_DAYS

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

        cfg = cls(
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
            evict_free_bytes=int(optional("TICKET_EVICT_FREE_BYTES", str(DEFAULT_EVICT_FREE_BYTES))),
            max_files=int(optional("TICKET_MAX_FILES", str(DEFAULT_MAX_FILES))),
            max_depth=int(optional("TICKET_MAX_DEPTH", str(DEFAULT_MAX_DEPTH))),
            concurrency=int(optional("TICKET_CONCURRENCY", str(DEFAULT_CONCURRENCY))),
            auto_sync_enabled=_as_bool(optional("TICKET_AUTO_SYNC_ENABLED", "true")),
            auto_sync_interval_seconds=int(
                optional("TICKET_AUTO_SYNC_INTERVAL_SECONDS", str(DEFAULT_AUTO_SYNC_INTERVAL_SECONDS))
            ),
            retention_days=int(optional("TICKET_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))),
            auto_scan_days=int(optional("TICKET_AUTO_SCAN_DAYS", str(DEFAULT_AUTO_SCAN_DAYS))),
            max_total_bytes=int(optional("TICKET_MAX_TOTAL_BYTES", str(DEFAULT_MAX_TOTAL_BYTES))),
            min_keep_days=int(optional("TICKET_MIN_KEEP_DAYS", str(DEFAULT_MIN_KEEP_DAYS))),
        )
        _validate_waterline(cfg)
        return cfg


def _validate_waterline(cfg: Config) -> None:
    """启动即校验两个阈值的相对关系，配错了不许起服务。

    为什么必须在这里拦截而不是等到运行时：两者反了不会报错，只会表现成
    "每次都失败、清理永远来不及"——2026-11-12 的事故就是这么发生的，
    现场只能靠人工读 journal 才发现。宁可起不来，也不要带着错误的水位跑。

    两条硬约束：
    1. 软阈值（淘汰）必须严格大于硬阈值（熔断），否则淘汰永远在熔断之后；
    2. 保留期必须大于最短保留天数，否则保留期被容量淘汰抢在前面，永远删 0 个。
    """
    if cfg.evict_free_bytes <= cfg.min_free_bytes:
        raise SystemExit(
            "TICKET_EVICT_FREE_BYTES 必须大于 TICKET_MIN_FREE_BYTES："
            f"淘汰阈值={cfg.evict_free_bytes} 熔断阈值={cfg.min_free_bytes}"
            "（淘汰必须先于熔断发生，否则空间不足导致的失败无法避免）"
        )
    if cfg.retention_days <= cfg.min_keep_days:
        raise SystemExit(
            "TICKET_RETENTION_DAYS 必须大于 TICKET_MIN_KEEP_DAYS："
            f"保留期={cfg.retention_days}天 最短保留={cfg.min_keep_days}天"
            "（两者相等时容量淘汰会抢在保留期之前删完，保留期形同虚设）"
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
