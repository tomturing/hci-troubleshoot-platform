"""
单元测试：SOP 执行环境数据注入与日志锚定
"""

from app.routes.sop_execution import (
    _extract_environment_fact_sources,
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


def test_resolve_env_variable_explicit_env_source():
    # env:xxx 可显式指定 env_info 中的字段名
    env_context = EnvironmentContextResponse(
        env_info={"cluster": {"version": "6.8.0"}, "hci_version": "6.9.0"}, alert_logs=[], task_logs=[]
    )
    val = _resolve_env_variable("version", {"acquisition_strategy": "env:hci_version"}, env_context)
    assert val == "6.9.0"


def test_resolve_env_variable_does_not_anchor_alert_node_ip():
    # node_ip 是告警锚定问题，不能由 env_injection 从告警列表猜测
    env_context = EnvironmentContextResponse(
        env_info={},
        alert_logs=[
            {"host": "10.0.1.10", "target": "node-1", "description": "disk failed on host 10.0.1.10"},
            {"host": "192.168.1.1", "target": "node-2", "description": "other issue"},
        ],
        task_logs=[],
    )
    val = _resolve_env_variable("node_ip", {}, env_context)
    assert val is None


def test_resolve_env_variable_does_not_parse_disk_sn_from_alert_description():
    # disk_sn 不在环境基础字段中，不能从描述中正则猜测
    env_context = EnvironmentContextResponse(
        env_info={},
        alert_logs=[
            {"host": "10.0.1.10", "target": "node-1", "description": "磁盘寿命异常 [SN: 500605B001234567] 告警"}
        ],
        task_logs=[],
    )
    val = _resolve_env_variable("disk_sn", {}, env_context)
    assert val is None


def test_resolve_env_variable_does_not_read_alert_direct_disk_sn():
    # 即便告警中存在 disk_sn，也应由 hci-alert-parsing 等动态 Skill 解析后写入变量池
    env_context = EnvironmentContextResponse(
        env_info={}, alert_logs=[{"disk_sn": "WN-9876543210", "description": "磁盘异常"}], task_logs=[]
    )
    val = _resolve_env_variable("disk_sn", {}, env_context)
    assert val is None


def test_resolve_env_variable_does_not_anchor_task_log():
    # 任务日志是列表型事实源，不能由 env_injection 隐式选择某条记录
    env_context = EnvironmentContextResponse(
        env_info={}, alert_logs=[], task_logs=[{"status": "失败", "type": "Migration", "request_id": "req-12345"}]
    )
    val = _resolve_env_variable("request_id", {}, env_context)
    assert val is None


def test_resolve_env_variable_semantic_routing_alert():
    # env_injection 不再承担混合告警语义路由，避免错误锚定 node_ip/disk_sn
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

    node_ip = _resolve_env_variable(
        "node_ip", {}, env_context, category_l1="存储", category_l2="磁盘寿命异常", sop_title="磁盘寿命异常排障"
    )
    assert node_ip is None

    disk_sn = _resolve_env_variable(
        "disk_sn", {}, env_context, category_l1="存储", category_l2="磁盘寿命异常", sop_title="磁盘寿命异常排障"
    )
    assert disk_sn is None


def test_resolve_env_variable_semantic_routing_task():
    # env_injection 不再承担混合任务语义路由
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

    req_id = _resolve_env_variable(
        "request_id", {}, env_context, category_l1="计算", category_l2="虚拟机开机失败", sop_title="虚拟机开机排障"
    )
    assert req_id is None


def test_resolve_env_variable_semantic_routing_fallback():
    # 不再 fallback 到首条告警，避免多告警场景错误注入
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

    node_ip = _resolve_env_variable(
        "node_ip", {}, env_context, category_l1="其他", category_l2="未知分类", sop_title="无"
    )
    assert node_ip is None


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

    # 2. alert/task 字段不再由 env_injection 解析
    level = _resolve_env_variable("level", {}, env_context)
    assert level is None

    disk_sn = _resolve_env_variable("disk_sn", {}, env_context)
    assert disk_sn is None

    status = _resolve_env_variable("status", {}, env_context)
    assert status is None

    trace_id = _resolve_env_variable("trace_id", {}, env_context)
    assert trace_id is None


def test_extract_environment_fact_sources_for_dynamic_skills():
    # 原始事实源可以进入变量池供动态 Skill 显式依赖，但不参与 env_injection 语义猜测
    env_context = {
        "is_raw": True,
        "env_info": {"hci_version": "6.8.0"},
        "alert_logs": [{"target": "SVR_aCloud_670", "description": "磁盘寿命异常"}],
        "task_logs": [{"request_id": "req-001", "status": 3}],
    }

    facts = _extract_environment_fact_sources(env_context)

    assert facts["env_info"] == {"hci_version": "6.8.0"}
    assert facts["alert_logs"][0]["target"] == "SVR_aCloud_670"
    assert facts["task_logs"][0]["request_id"] == "req-001"
    assert _resolve_env_variable("node_ip", {}, env_context) is None
