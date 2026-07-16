"""
关键信号端到端测试 - 验证命令构建和解析

验证项：
1. BackendSignal 能否构建正确的 acli 命令
2. FrontendSignal 能否正确解析 acli 输出
3. produces 动态提取是否工作
"""

import json
import os
import sys

_backend = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
_agent_service = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent-service"))
if _backend not in sys.path:
    sys.path.insert(0, _backend)
if _agent_service not in sys.path:
    sys.path.insert(0, _agent_service)

from app.tools.qfk.handlers import HandlerRegistry, LogKeywordHandler
from app.tools.qfk.signal import BackendSignal, BackendSignalTarget
from app.tools.qkv.parser import parse_frontend_value
from app.tools.qkv.signal import FrontendQueryType


def test_qfk_log_command_build():
    """测试日志信号命令构建"""
    print("\n=== QFK log 命令构建测试 ===")

    # 案例 41570: qemu 日志检查
    sig = BackendSignal(
        namespace="log",
        target=BackendSignalTarget(
            path="/sf/log/3/",
            resource="sfvt_qemu_{{VM}}.log"
        ),
        keywords=["iotimeout"],
        expected=True
    )

    handler = HandlerRegistry.get("log")
    cmds = handler.build_commands(sig)
    print("案例 41570 - qemu日志检查:")
    print(f"  命令: {cmds[0]}")
    assert "acli log get" in cmds[0]
    assert "-k" in cmds[0] and "iotimeout" in cmds[0]
    assert "sfvt_qemu" in cmds[0]
    assert "/sf/log/3/" in cmds[0]
    print("  ✅ 命令构建正确")

    # 案例 40652: 内核日志检查
    sig2 = BackendSignal(
        namespace="log",
        target=BackendSignalTarget(
            path="/sf/log/today/",
            resource="kernel.log"
        ),
        keywords=["disk", "error"],
        match_mode="and",
        expected=False
    )
    cmds2 = handler.build_commands(sig2)
    print("\n案例 40750 - 内核日志检查:")
    print(f"  命令: {cmds2[0]}")
    assert "/sf/log/today/" in cmds2[0]
    assert "kernel.log" in cmds2[0]
    print("  ✅ 命令构建正确")


def test_qfk_system_command_build():
    """测试系统命令构建"""
    print("\n=== QFK system 命令构建测试 ===")

    handler = HandlerRegistry.get("system")

    # 案例 27123: lsof 检查镜像占用
    sig1 = BackendSignal(
        namespace="system",
        sub_command="lsof",
        keywords=["7436939093432"],
        expected=True
    )
    cmds1 = handler.build_commands(sig1)
    print("案例 27123 - lsof:")
    print(f"  命令: {cmds1[0]}")
    assert cmds1[0] == "acli system lsof"
    print("  ✅ 命令构建正确")

    # 案例 27123: ps 检查进程
    sig2 = BackendSignal(
        namespace="system",
        sub_command="ps auxf",
        keywords=["ClwDRDBClient"],
        expected=True
    )
    cmds2 = handler.build_commands(sig2)
    print("\n案例 27123 - ps:")
    print(f"  命令: {cmds2[0]}")
    assert cmds2[0] == "acli system ps auxf"
    print("  ✅ 命令构建正确")

    # 案例 40652: smartctl
    sig3 = BackendSignal(
        namespace="system",
        sub_command="smartctl -a /dev/sda",
        keywords=["Reallocated_Sector_Ct"],
        expected=True
    )
    cmds3 = handler.build_commands(sig3)
    print("\n案例 40652 - smartctl:")
    print(f"  命令: {cmds3[0]}")
    assert cmds3[0] == "acli system smartctl -a /dev/sda"
    print("  ✅ 命令构建正确")

    # 案例 40680: lsblk
    sig4 = BackendSignal(
        namespace="system",
        sub_command="lsblk",
        keywords=["disk"],
        expected=True
    )
    cmds4 = handler.build_commands(sig4)
    print("\n案例 40680 - lsblk:")
    print(f"  命令: {cmds4[0]}")
    assert cmds4[0] == "acli system lsblk"
    print("  ✅ 命令构建正确")


def test_qkv_produces_extraction():
    """测试 QKV produces 动态提取"""
    print("\n=== QKV produces 动态提取测试 ===")

    # 模拟 acli alert get 返回
    alert_json = json.dumps({
        "data": [
            {
                "alert_type": "磁盘被拔出",
                "host": "SVR_001",
                "vm": "",
                "target": "DISK_SN_12345",
                "end": "2026-07-15 10:00:00",
                "description": "检测到硬盘被拔出"
            }
        ]
    })

    # 案例 40750: 产生 HOST 和 DISK_SN 变量
    produces = [
        {"name": "HOST", "path": "host"},
        {"name": "DISK_SN", "path": "target"},
        {"name": "END", "path": "end"},
    ]

    vals = parse_frontend_value(FrontendQueryType.ALERT, alert_json, produces)
    print("案例 40750 - alert 提取:")
    print(f"  produces: {produces}")
    print(f"  结果: {vals}")
    assert len(vals) == 1
    assert vals[0]["host"] == "SVR_001"
    assert vals[0]["disk_sn"] == "DISK_SN_12345"
    assert vals[0]["end"] == "2026-07-15 10:00:00"
    print("  ✅ produces 动态提取正确")

    # 测试硬编码兜底
    vals_fallback = parse_frontend_value(FrontendQueryType.ALERT, alert_json, produces=None)
    print("\n硬编码兜底测试:")
    print(f"  结果: {vals_fallback[0].keys()}")
    assert "alert_type" in vals_fallback[0]
    print("  ✅ 硬编码兜底正确")


def test_qkv_task_extraction():
    """测试 QKV task 提取"""
    print("\n=== QKV task 提取测试 ===")

    # 模拟 acli task get 返回
    task_json = json.dumps({
        "data": [
            {
                "type": "启动虚拟机",
                "status": "失败",
                "host": "SVR_002",
                "vm": "vm-101",
                "description": "虚拟机镜像忙，正在执行其他操作！",
                "errcode_tracing": "0x0C000005",
                "request_id": "abc123"
            }
        ]
    })

    # 案例 27123: 查看 VM 启动任务
    produces = [
        {"name": "VM", "path": "vm"},
        {"name": "HOST", "path": "host"},
        {"name": "ERRCODE_TRACING", "path": "errcode_tracing"},
    ]

    vals = parse_frontend_value(FrontendQueryType.TASK, task_json, produces)
    print("案例 27123 - task 提取:")
    print(f"  结果: {vals}")
    assert vals[0]["vm"] == "vm-101"
    assert vals[0]["host"] == "SVR_002"
    assert vals[0]["errcode_tracing"] == "0x0C000005"
    print("  ✅ task 提取正确")


def test_evaluator():
    """测试关键字评估"""
    print("\n=== 关键字评估测试 ===")

    handler = LogKeywordHandler()

    # 模拟执行结果
    from app.tools.acli.executor import ExecResult

    # 案例 27123: 进程检查
    res1 = ExecResult(
        stdout="/opt/ClwDRDBClient_2.0.230728/agent_application/application_main",
        stderr="",
        exit_code=0,
        command="acli system ps auxf",
        node="10.0.0.1",
        duration_ms=10,
        truncated=False,
        risk_level=1
    )

    matched, matched_kws, evidence = handler.evaluate([res1], ["ClwDRDBClient"], "or")
    print("案例 27123 - 进程检查:")
    print(f"  matched: {matched}")
    assert matched is True
    print("  ✅ 评估正确")

    # 案例 40750: 内核日志无磁盘离线
    res2 = ExecResult(
        stdout="kernel: normal boot messages...",
        stderr="",
        exit_code=0,
        command="acli log get -k disk -f kernel.log",
        node="10.0.0.1",
        duration_ms=10,
        truncated=False,
        risk_level=1
    )

    matched2, _, _ = handler.evaluate([res2], ["disk", "error"], "and")
    print("\n案例 40750 - 内核日志（期望无错误）:")
    print(f"  matched (期望为 False): {matched2}")
    assert matched2 is False  # 只匹配到 disk，没匹配到 error
    print("  ✅ 评估正确")


def main():
    print("=" * 60)
    print("关键信号端到端测试")
    print("=" * 60)

    test_qfk_log_command_build()
    test_qfk_system_command_build()
    test_qkv_produces_extraction()
    test_qkv_task_extraction()
    test_evaluator()

    print("\n" + "=" * 60)
    print("✅ 全部端到端测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
