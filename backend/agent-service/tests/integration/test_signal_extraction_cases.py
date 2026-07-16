"""
关键信号抽取验收脚本 - 使用新代码测试5个真实KBD案例

验证项：
1. 每个案例的预期信号能否用 BackendSignal/FrontendSignal 正确构造
2. acquirer 是否在 ACQUIRER_CATALOG 中
3. HandlerRegistry 能否正确路由
4. produces/requires/matcher 是否符合约束
"""

import os
import sys

# 添加项目路径
_backend = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
_agent_service = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent-service"))
_kb_service = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "kb-service"))
if _backend not in sys.path:
    sys.path.insert(0, _backend)
if _agent_service not in sys.path:
    sys.path.insert(0, _agent_service)
if _kb_service not in sys.path:
    sys.path.insert(0, _kb_service)

from app.tools.qfk.handlers import HandlerRegistry
from app.tools.qfk.signal import BackendSignal, BackendSignalTarget
from app.tools.qkv.signal import FrontendQueryType, FrontendSignal

# 直接定义 ACQUIRER_CATALOG 和 VALID_MATCHER_TYPES（避免跨服务导入）
# display_name 标准命名（与 extract_signals.py / seed 保持一致）：
#   qkv_alert  - 前端信号-告警查询
#   qkv_task   - 前端信号-任务查询
#   qkv_dialog - 前端信号-弹框查询
#   qfk_log      - 后端信号-日志检查和操作
#   qfk_service  - 后端信号-服务检查和操作
#   qfk_system   - 后端信号-系统检查和操作
#   qfk_vm       - 后端信号-虚拟机相关操作
#   qfk_network  - 后端信号-网络相关操作
#   qfk_storage  - 后端信号-存储相关操作
#   qfk_hardware - 后端信号-硬件相关操作
#   qfk_platform - 后端信号-平台相关操作
ACQUIRER_CATALOG = {
    "qkv_alert": "前端信号-告警查询：acli alert get，产出 host/vm/target/alert_type/end 等",
    "qkv_task": "前端信号-任务查询：acli task get，产出 status/host/vm/errcode_tracing/request_id 等",
    "qkv_dialog": "前端信号-弹框查询：acli dialog/log get",
    "qfk_log": "后端信号-日志检查和操作：acli log get -k <keyword>",
    "qfk_service": "后端信号-服务检查和操作：acli service {asv|anet|host} <name> status",
    "qfk_system": "后端信号-系统检查和操作：acli system <sub_command>",
    "qfk_vm": "后端信号-虚拟机相关操作：acli vm <sub_command>",
    "qfk_network": "后端信号-网络相关操作：acli network <sub_command>",
    "qfk_storage": "后端信号-存储相关操作：acli storage <sub_command>",
    "qfk_hardware": "后端信号-硬件相关操作：acli hardware <sub_command>",
    "qfk_platform": "后端信号-平台相关操作：acli platform <sub_command>",
}
VALID_MATCHER_TYPES = {"keyword", "regex", "state", "threshold", "json_path", "exists"}


def test_case_27123():
    """
    案例 27123: 【HCI-VT】虚拟机开机失败,报错虚拟机镜像忙，正在进行其他操作

    排查步骤：
    1. 查看虚拟机任务详情 - QKV task
    2. lsof | grep <vmid> 检查镜像占用 - QFK system (lsof)
    3. ps auxf | grep <PID> 查看进程详情 - QFK system (ps)

    解决：kill -9 <PID> - QFK system (kill) [写操作，不在信号中]
    """
    case_id = "27123"
    print(f"\n{'='*60}")
    print(f"案例 {case_id}: 虚拟机开机失败，镜像忙")
    print(f"{'='*60}")

    # 定义预期抽取的信号
    expected_signals = [
        # 生产者：查看任务详情
        {
            "id": "s1",
            "signal_category": "frontend",
            "keyword": "启动虚拟机",
            "description": "查看虚拟机启动任务详情，确认失败报错",
            "acquirer": "qkv_task",
            "acquirer_args": {"keyword": "启动虚拟机", "is_failed": True, "limit": 100},
            "produces": [{"name": "VM", "path": "vm"}, {"name": "HOST", "path": "host"}, {"name": "ERRCODE_TRACING", "path": "errcode_tracing"}],
            "requires": [],
            "matcher": None,
        },
        # 消费者：lsof 检查镜像占用
        {
            "id": "s2",
            "signal_category": "backend",
            "keyword": "镜像占用",
            "description": "检查虚拟机镜像文件是否被其他进程占用",
            "acquirer": "qfk_system",
            "acquirer_args": {"sub_command": "lsof", "target": {"scope": "{{HOST}}"}},
            "produces": [],
            "requires": ["VM"],
            "matcher": {"type": "keyword", "pattern": "{{VM}}", "mode": "any", "expected": True},
        },
        # 消费者：ps 检查进程详情
        {
            "id": "s3",
            "signal_category": "backend",
            "keyword": "进程详情",
            "description": "查询占用镜像文件的进程详情",
            "acquirer": "qfk_system",
            "acquirer_args": {"sub_command": "ps auxf"},
            "produces": [],
            "requires": [],
            "matcher": {"type": "keyword", "pattern": "ClwDRDBClient", "mode": "any", "expected": True},
        },
    ]

    return _validate_signals(case_id, expected_signals)


def test_case_41570():
    """
    案例 41570: 因外置iSCSI存储scrub服务导致存储时延高，虚拟机IO卡顿挂起

    排查步骤：
    1. 查看虚拟机qemu日志 - QFK log (iotimeout)
    2. 查看 iostat 日志 - QFK log (LOG_iostat.txt)

    注意：scrub服务检查是第三方操作，不在 HCI 后台范围内
    """
    case_id = "41570"
    print(f"\n{'='*60}")
    print(f"案例 {case_id}: iSCSI存储scrub导致时延高")
    print(f"{'='*60}")

    expected_signals = [
        # 消费者：qemu日志检查 iotimeout
        {
            "id": "s1",
            "signal_category": "backend",
            "keyword": "iotimeout",
            "description": "查看虚拟机qemu日志，确认是否存在iotimeout报错",
            "acquirer": "qfk_log",
            "acquirer_args": {
                "target": {"path": "/sf/log/3/", "resource": "sfvt_qemu_{{VM}}.log"},
            },
            "produces": [],
            "requires": ["VM"],
            "matcher": {"type": "keyword", "pattern": "iotimeout", "mode": "any", "expected": True},
        },
        # 消费者：iostat 日志检查时延
        {
            "id": "s2",
            "signal_category": "backend",
            "keyword": "时延",
            "description": "查看主机iostat日志，分析存储IO读写速率和时延",
            "acquirer": "qfk_log",
            "acquirer_args": {
                "target": {"path": "/sf/log/blackbox/today/", "resource": "LOG_iostat.txt"},
            },
            "produces": [],
            "requires": [],
            "matcher": {"type": "keyword", "pattern": "await", "mode": "any", "expected": True},
        },
    ]

    return _validate_signals(case_id, expected_signals)


def test_case_40652():
    """
    案例 40652: 硬盘物理坏道导致SMART 5值持续增长

    排查步骤：
    1. diskchecker.py 检查 SMART 信息 - 容器内脚本（非标准 acli）
    2. 管理平台查看硬盘状态 - UI 操作（非后台命令）
    3. grep 定位硬盘设备文件 - QFK log (grep 配置文件)

    注意：smartctl 是 acli system 命令的子集
    """
    case_id = "40652"
    print(f"\n{'='*60}")
    print(f"案例 {case_id}: SMART 5值增长，硬盘物理坏道")
    print(f"{'='*60}")

    expected_signals = [
        # 消费者：smartctl 检查 SMART 信息（通过 acli system smartctl）
        {
            "id": "s1",
            "signal_category": "backend",
            "keyword": "SMART 5",
            "description": "检查磁盘SMART信息及坏道计数",
            "acquirer": "qfk_system",
            "acquirer_args": {"sub_command": "smartctl -a /dev/sdX"},
            "produces": [],
            "requires": [],
            "matcher": {"type": "keyword", "pattern": "Reallocated_Sector_Ct", "mode": "any", "expected": True},
        },
        # 消费者：grep 硬盘配置文件
        {
            "id": "s2",
            "signal_category": "backend",
            "keyword": "硬盘SN",
            "description": "定位故障硬盘对应的物理设备文件",
            "acquirer": "qfk_log",
            "acquirer_args": {
                "target": {"path": "/sf/cfg/vs/disk/", "resource": "{{DISK_SN}}.json"},
            },
            "produces": [],
            "requires": ["DISK_SN"],
            "matcher": {"type": "keyword", "pattern": "/dev/", "mode": "any", "expected": True},
        },
    ]

    return _validate_signals(case_id, expected_signals)


def test_case_40680():
    """
    案例 40680: Dell HBA355i RAID卡部分磁盘无法识别

    排查步骤：
    1. lsblk 检查磁盘数量 - QFK system (lsblk)
    2. lspci 检查 RAID 卡型号 - QFK system (lspci)
    3. modinfo 检查驱动版本 - QFK system (modinfo)
    4. BMC 查看（第三方操作）
    """
    case_id = "40680"
    print(f"\n{'='*60}")
    print(f"案例 {case_id}: Dell RAID卡磁盘无法识别")
    print(f"{'='*60}")

    expected_signals = [
        # 消费者：lsblk 检查磁盘数量
        {
            "id": "s1",
            "signal_category": "backend",
            "keyword": "磁盘数量",
            "description": "检查操作系统已识别磁盘数量",
            "acquirer": "qfk_system",
            "acquirer_args": {"sub_command": "lsblk"},
            "produces": [],
            "requires": [],
            "matcher": {"type": "keyword", "pattern": "disk", "mode": "any", "expected": True},
        },
        # 消费者：lspci 检查 RAID 卡型号
        {
            "id": "s2",
            "signal_category": "backend",
            "keyword": "RAID卡型号",
            "description": "确认RAID卡型号及驱动版本",
            "acquirer": "qfk_system",
            "acquirer_args": {"sub_command": "lspci"},
            "produces": [],
            "requires": [],
            "matcher": {"type": "keyword", "pattern": "SAS38xx", "mode": "any", "expected": True},
        },
        # 消费者：modinfo 检查驱动版本
        {
            "id": "s3",
            "signal_category": "backend",
            "keyword": "驱动版本",
            "description": "检查mpt3sas驱动版本",
            "acquirer": "qfk_system",
            "acquirer_args": {"sub_command": "modinfo mpt3sas"},
            "produces": [],
            "requires": [],
            "matcher": {"type": "keyword", "pattern": "version:", "mode": "any", "expected": True},
        },
    ]

    return _validate_signals(case_id, expected_signals)


def test_case_40750():
    """
    案例 40750: 磁盘组配比不一致导致数据同步频繁，误报磁盘被拔出

    排查步骤：
    1. 查看告警日志 - QKV alert (磁盘被拔出)
    2. 磁盘管理界面查看状态 - UI 操作
    3. 内核日志检查 - QFK log (kernel.log)
    4. 任务列表检查 - QKV task (数据同步)
    5. 磁盘组配比对比 - UI 操作
    """
    case_id = "40750"
    print(f"\n{'='*60}")
    print(f"案例 {case_id}: 磁盘组配比不一致误报")
    print(f"{'='*60}")

    expected_signals = [
        # 生产者：查看告警
        {
            "id": "s1",
            "signal_category": "frontend",
            "keyword": "磁盘被拔出",
            "description": "查看告警日志，确认磁盘被拔出告警",
            "acquirer": "qkv_alert",
            "acquirer_args": {"keyword": "磁盘被拔出", "limit": 100},
            "produces": [{"name": "HOST", "path": "host"}, {"name": "DISK_SN", "path": "target"}],
            "requires": [],
            "matcher": None,
        },
        # 消费者：内核日志检查
        {
            "id": "s2",
            "signal_category": "backend",
            "keyword": "磁盘离线",
            "description": "检查内核日志中是否有磁盘离线记录",
            "acquirer": "qfk_log",
            "acquirer_args": {
                "target": {"path": "/sf/log/today/", "resource": "kernel.log"},
            },
            "produces": [],
            "requires": ["HOST"],
            "matcher": {"type": "keyword", "pattern": "disk", "mode": "any", "expected": False},
        },
        # 生产者：查看数据同步任务
        {
            "id": "s3",
            "signal_category": "frontend",
            "keyword": "数据同步",
            "description": "检查虚拟存储任务列表中的数据同步任务",
            "acquirer": "qkv_task",
            "acquirer_args": {"keyword": "数据同步", "is_failed": False, "limit": 100},
            "produces": [{"name": "TASK_TYPE", "path": "type"}],
            "requires": [],
            "matcher": None,
        },
    ]

    return _validate_signals(case_id, expected_signals)


def _validate_signals(case_id: str, signals: list) -> dict:
    """验证信号列表的合法性"""
    results = {
        "case_id": case_id,
        "total": len(signals),
        "valid": 0,
        "invalid": 0,
        "errors": [],
        "acquirer_coverage": set(),
    }

    for sig in signals:
        try:
            sig_id = sig.get("id", "?")
            category = sig.get("signal_category")
            acquirer = sig.get("acquirer", "")

            # 1. 验证 acquirer 在 ACQUIRER_CATALOG 中
            if acquirer not in ACQUIRER_CATALOG:
                raise ValueError(f"acquirer '{acquirer}' 不在 ACQUIRER_CATALOG 中")

            results["acquirer_coverage"].add(acquirer)

            # 2. 根据类别构造信号对象
            if category == "frontend":
                query_type = sig["acquirer_args"].get("query", "alert")
                # acquirer 已统一为 snake_case（如 qkv_alert）：按 _ 拆分取 query 类型
                if acquirer.startswith("qkv_"):
                    query_type = acquirer.split("_", 1)[1]
                try:
                    query = FrontendQueryType(query_type)
                except ValueError:
                    raise ValueError(f"无效的 query 类型: {query_type}") from None

                fs = FrontendSignal(
                    query=query,
                    keyword=sig["acquirer_args"].get("keyword", ""),
                    is_failed=sig["acquirer_args"].get("is_failed", False),
                    limit=sig["acquirer_args"].get("limit", 100),
                    produces=sig.get("produces", []),
                )
                print(f"  ✅ [{sig_id}] FrontendSignal(query={query.value}, keyword='{fs.keyword}', produces={fs.produces})")

            elif category == "backend":
                # 从 acquirer 解析 namespace（snake_case：qfk_log -> log）
                namespace = acquirer.split("_", 1)[1] if "_" in acquirer else acquirer

                # 构造 BackendSignal
                args = sig.get("acquirer_args", {})
                target_data = args.get("target", {})
                target = BackendSignalTarget(**{k: v for k, v in target_data.items() if k in ("scope", "resource", "path", "time_window")}) if target_data else None

                matcher = sig.get("matcher", {})
                keywords = []
                if matcher.get("type") == "keyword":
                    p = matcher.get("pattern", "")
                    keywords = [p] if isinstance(p, str) else list(p)

                bs = BackendSignal(
                    namespace=namespace,
                    target=target,
                    keywords=keywords,
                    match_mode=matcher.get("mode", "any"),
                    expected=matcher.get("expected", True),
                    sub_command=args.get("sub_command"),
                )

                # 3. 验证 HandlerRegistry 能找到 handler
                handler = HandlerRegistry.get(bs.namespace)
                handler_name = handler.__class__.__name__

                # 4. 验证 matcher 类型
                matcher_type = matcher.get("type") if matcher else None
                if matcher_type and matcher_type not in VALID_MATCHER_TYPES:
                    raise ValueError(f"无效的 matcher 类型: {matcher_type}")

                print(f"  ✅ [{sig_id}] BackendSignal(namespace='{namespace}', handler={handler_name}, sub_cmd={bs.sub_command})")
            else:
                raise ValueError(f"未知的 signal_category: {category}")

            results["valid"] += 1

        except Exception as e:
            results["invalid"] += 1
            results["errors"].append(f"信号 {sig.get('id', '?')}: {e}")
            print(f"  ❌ [{sig.get('id', '?')}] 错误: {e}")

    return results


def main():
    print("=" * 60)
    print("关键信号抽取验收 - 5个真实KBD案例")
    print("=" * 60)

    print(f"\n支持的 acquirer 目录 ({len(ACQUIRER_CATALOG)} 个):")
    for k, v in ACQUIRER_CATALOG.items():
        print(f"  - {k}: {v[:50]}...")

    print(f"\n支持的 matcher 类型 ({len(VALID_MATCHER_TYPES)} 种): {VALID_MATCHER_TYPES}")

    print("\nHandlerRegistry 支持的 namespace:")
    for ns in HandlerRegistry.supported_namespaces():
        print(f"  - {ns}")

    # 运行所有案例测试
    all_results = []
    all_results.append(test_case_27123())
    all_results.append(test_case_41570())
    all_results.append(test_case_40652())
    all_results.append(test_case_40680())
    all_results.append(test_case_40750())

    # 汇总
    print("\n" + "=" * 60)
    print("验收汇总")
    print("=" * 60)

    total_signals = sum(r["total"] for r in all_results)
    total_valid = sum(r["valid"] for r in all_results)
    total_invalid = sum(r["invalid"] for r in all_results)
    all_acquirers = set().union(*[r["acquirer_coverage"] for r in all_results])

    print(f"\n总计信号数: {total_signals}")
    print(f"有效信号: {total_valid} ({total_valid/total_signals*100:.1f}%)")
    print(f"无效信号: {total_invalid}")

    print(f"\n覆盖的 acquirer ({len(all_acquirers)} 个):")
    for acq in sorted(all_acquirers):
        print(f"  - {acq}")

    if total_invalid > 0:
        print("\n错误详情:")
        for r in all_results:
            for err in r["errors"]:
                print(f"  案例 {r['case_id']}: {err}")

    # 覆盖率分析
    print("\n" + "=" * 60)
    print("ACQUIRER_CATALOG 覆盖率分析")
    print("=" * 60)

    used_acquirers = all_acquirers
    unused_acquirers = set(ACQUIRER_CATALOG.keys()) - used_acquirers

    print(f"\n已使用的 acquirer: {len(used_acquirers)}/{len(ACQUIRER_CATALOG)}")
    if unused_acquirers:
        print(f"未使用的 acquirer ({len(unused_acquirers)} 个):")
        for acq in sorted(unused_acquirers):
            print(f"  - {acq}")

    print(f"\n{'='*60}")
    print(f"✅ 验收完成: {total_valid}/{total_signals} 信号有效")
    print(f"{'='*60}")

    return total_invalid == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
