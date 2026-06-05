"""
单元测试：SOP 执行环境数据注入与日志锚定
"""

from app.routes.sop_execution import (
    _filter_logs_by_keywords,
    _get_filter_keywords,
    _resolve_env_variable,
)
from shared.models.schemas import EnvironmentContextResponse


def test_resolve_env_variable_hci_version():
    # 测试从 env_info 提取基础信息
    env_context = EnvironmentContextResponse(
        env_info={"hci_version": "6.8.0", "cluster_name": "prod-cluster"}, alert_logs=[], task_logs=[]
    )
    # 大小写和下划线不敏感匹配
    val = _resolve_env_variable("hci_version", {}, env_context)
    assert val == "6.8.0"

    val_upper = _resolve_env_variable("HCI_VERSION", {}, env_context)
    assert val_upper == "6.8.0"


def test_resolve_env_variable_alert_anchoring_node_ip():
    # 测试从首个告警日志提取 host/target
    env_context = EnvironmentContextResponse(
        env_info={},
        alert_logs=[
            {"host": "10.0.1.10", "target": "node-1", "description": "disk failed on host 10.0.1.10"},
            {"host": "192.168.1.1", "target": "node-2", "description": "other issue"},
        ],
        task_logs=[],
    )
    # node_ip 映射到 alert_logs[0] 中的 host
    val = _resolve_env_variable("node_ip", {}, env_context)
    assert val == "10.0.1.10"


def test_resolve_env_variable_alert_anchoring_disk_sn():
    # 测试从首个告警日志提取 disk_sn (通过 description 中的正则)
    env_context = EnvironmentContextResponse(
        env_info={},
        alert_logs=[
            {"host": "10.0.1.10", "target": "node-1", "description": "磁盘寿命异常 [SN: 500605B001234567] 告警"}
        ],
        task_logs=[],
    )
    val = _resolve_env_variable("disk_sn", {}, env_context)
    assert val == "500605B001234567"


def test_resolve_env_variable_alert_anchoring_disk_sn_direct():
    # 测试首个告警日志直接包含 sn / disk_sn 键
    env_context = EnvironmentContextResponse(
        env_info={}, alert_logs=[{"disk_sn": "WN-9876543210", "description": "磁盘异常"}], task_logs=[]
    )
    val = _resolve_env_variable("disk_sn", {}, env_context)
    assert val == "WN-9876543210"


def test_resolve_env_variable_task_anchoring():
    # 测试从首个任务日志提取相关变量
    env_context = EnvironmentContextResponse(
        env_info={}, alert_logs=[], task_logs=[{"status": "失败", "type": "Migration", "request_id": "req-12345"}]
    )
    val = _resolve_env_variable("request_id", {}, env_context)
    assert val == "req-12345"


def test_get_filter_keywords():
    # 测试从分类和标题中提取关键字
    kws = _get_filter_keywords("磁盘寿命异常", "存储", "磁盘寿命异常排障")
    assert "disk" in kws
    assert "磁盘" in kws
    assert "寿命" in kws
    assert "存储" in kws

    kws_vm = _get_filter_keywords("虚拟机开机失败", "计算", "虚拟机开机排障")
    assert "vm" in kws_vm
    assert "虚拟机" in kws_vm
    assert "power" in kws_vm


def test_filter_logs_by_keywords():
    logs = [
        {"description": "User login failed from host 10.0.0.1"},
        {"description": "Disk SMART warning on node 10.0.0.2, sn: SN-1122"},
    ]
    # 匹配 "disk" 关键字
    filtered = _filter_logs_by_keywords(logs, ["disk"])
    assert len(filtered) == 1
    assert "SN-1122" in filtered[0]["description"]

    # 无匹配
    filtered_empty = _filter_logs_by_keywords(logs, ["nonexistent_keyword"])
    assert len(filtered_empty) == 0


def test_resolve_env_variable_semantic_routing_alert():
    # 模拟混合告警：alert_logs[0] 是无关的备份失败，alert_logs[1] 是与磁盘相关的告警
    env_context = EnvironmentContextResponse(
        env_info={},
        alert_logs=[
            {
                "host": "10.0.0.1",
                "description": "Backup failed on node 10.0.0.1",
            },
            {
                "host": "10.0.0.2",
                "description": "Disk status critical on host 10.0.0.2, sn: SN-123456",
            },
        ],
        task_logs=[],
    )

    # 路由应该能够过滤出磁盘相关的告警，并将其作为锚点提取 node_ip 和 disk_sn
    node_ip = _resolve_env_variable(
        "node_ip", {}, env_context, category_l1="存储", category_l2="磁盘寿命异常", sop_title="磁盘寿命异常排障"
    )
    assert node_ip == "10.0.0.2"

    disk_sn = _resolve_env_variable(
        "disk_sn", {}, env_context, category_l1="存储", category_l2="磁盘寿命异常", sop_title="磁盘寿命异常排障"
    )
    assert disk_sn == "SN-123456"


def test_resolve_env_variable_semantic_routing_task():
    # 模拟混合任务：task_logs[0] 是无关的备份任务，task_logs[1] 是与虚拟机相关的任务
    env_context = EnvironmentContextResponse(
        env_info={},
        alert_logs=[],
        task_logs=[
            {
                "request_id": "req-backup-001",
                "name": "Backup VMs",
            },
            {
                "request_id": "req-vm-start-002",
                "name": "Start VM failed",
            },
        ],
    )

    # 路由应该过滤出虚拟机相关的任务
    req_id = _resolve_env_variable(
        "request_id", {}, env_context, category_l1="计算", category_l2="虚拟机开机失败", sop_title="虚拟机开机排障"
    )
    assert req_id == "req-vm-start-002"


def test_resolve_env_variable_semantic_routing_fallback():
    # 模拟混合告警，但分类不匹配任何告警关键字，应该回退到 alert_logs[0]
    env_context = EnvironmentContextResponse(
        env_info={},
        alert_logs=[
            {
                "host": "10.0.0.1",
                "description": "Backup failed on node 10.0.0.1",
            },
            {
                "host": "10.0.0.2",
                "description": "Disk status critical on host 10.0.0.2, sn: SN-123456",
            },
        ],
        task_logs=[],
    )

    # 传入不相关的分类
    node_ip = _resolve_env_variable(
        "node_ip", {}, env_context, category_l1="其他", category_l2="未知分类", sop_title="无"
    )
    assert node_ip == "10.0.0.1"


def test_resolve_env_variable_raw():
    # 测试原始数据注入模式
    env_context = {
        "is_raw": True,
        "env_info": {
            "hci_version": "6.8.0_R2",
            "name": "raw-cluster-name",
            "mcastaddr": "239.0.0.1",
        },
        "alert_logs": [
            {
                "urgent_type": 1,
                "end": 1780000000,
                "target": "raw-target",
                "type": "disk_failed",
                "description": "磁盘异常 [sn: RAW-SN-123]",
            }
        ],
        "task_logs": [
            {
                "status": 3,
                "end": 1780000100,
                "type": "start_vm",
                "request_id": "req-raw-456",
            }
        ],
    }

    # 1. 验证 env_info 字段映射
    version = _resolve_env_variable("hci_version", {}, env_context)
    assert version == "6.8.0_R2"

    cluster_name = _resolve_env_variable("cluster_name", {}, env_context)
    assert cluster_name == "raw-cluster-name"

    network_config = _resolve_env_variable("network_config", {}, env_context)
    assert network_config == "239.0.0.1"

    # 2. 验证 alert 字段映射
    level = _resolve_env_variable("level", {}, env_context)
    assert level == "CRITICAL"

    disk_sn = _resolve_env_variable("disk_sn", {}, env_context)
    assert disk_sn == "RAW-SN-123"

    # 3. 验证 task 字段映射
    status = _resolve_env_variable("status", {}, env_context)
    assert status == "失败"

    trace_id = _resolve_env_variable("trace_id", {}, env_context)
    assert trace_id == "req-raw-456"
