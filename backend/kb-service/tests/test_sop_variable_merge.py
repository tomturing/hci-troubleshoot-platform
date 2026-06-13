"""
KB Service — SOP 变量三路合并单元测试（T-AGT-26）

测试 merge_variable_schema() 函数的三路合并逻辑：
1. 两版都有 → 保留人工编辑字段
2. 仅新版有 → 自动添加，auto_generated=True
3. 仅旧版有 → 标记 deprecated=True，不删除
"""

from app.services.sop_parser import merge_variable_schema


class TestMergeVariableSchema:
    """三路合并测试类"""

    def test_empty_old_schema(self):
        """测试旧版为空的情况：直接使用新版"""
        old_schema = []
        new_schema = [
            {"name": "node_ip", "acquisition_strategy": "env_context"},
            {"name": "vm_name", "acquisition_strategy": "tool"},
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert len(merged) == 2
        assert deprecated == []
        # 新变量标记 auto_generated=True
        assert merged[0]["auto_generated"] is True
        assert merged[1]["auto_generated"] is True

    def test_empty_new_schema(self):
        """测试新版为空的情况：所有旧变量标记 deprecated"""
        old_schema = [
            {"name": "node_ip", "description": "节点IP地址"},
            {"name": "vm_name", "description": "虚拟机名称"},
        ]
        new_schema = []

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert len(merged) == 2
        assert deprecated == ["node_ip", "vm_name"]
        # 所有变量标记 deprecated
        assert merged[0]["deprecated"] is True
        assert merged[1]["deprecated"] is True

    def test_both_versions_same_vars(self):
        """测试两版变量完全相同：保留人工编辑字段"""
        old_schema = [
            {
                "name": "node_ip",
                "description": "节点 IP 地址（人工编辑）",
                "acquisition_strategy": "env_context",
                "validation_pattern": "^\\d{1,3}(\\.\\d{1,3}){3}$",
                "display_name": "节点IP",
            },
            {
                "name": "vm_name",
                "description": "虚拟机名称（人工编辑）",
                "acquisition_strategy": "user_input",  # 人工修改
                "acquisition_tool": None,
            },
        ]
        new_schema = [
            {
                "name": "node_ip",
                "description": "",  # 新版自动解析无描述
                "acquisition_strategy": "env_context",  # 自动推断
                "validation_pattern": None,  # 新版无校验规则
                "auto_generated": True,
            },
            {
                "name": "vm_name",
                "description": "",
                "acquisition_strategy": "tool",  # 自动推断为 tool
                "acquisition_tool": "get_vm_list",
                "auto_generated": True,
            },
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert len(merged) == 2
        assert deprecated == []

        # node_ip：保留人工编辑的 description、validation_pattern
        node_ip_var = next(v for v in merged if v["name"] == "node_ip")
        assert node_ip_var["description"] == "节点 IP 地址（人工编辑）"
        assert node_ip_var["validation_pattern"] == "^\\d{1,3}(\\.\\d{1,3}){3}$"
        assert node_ip_var["display_name"] == "节点IP"
        assert node_ip_var["auto_generated"] is False

        # vm_name：保留人工修改的 acquisition_strategy
        vm_name_var = next(v for v in merged if v["name"] == "vm_name")
        assert vm_name_var["description"] == "虚拟机名称（人工编辑）"
        assert vm_name_var["acquisition_strategy"] == "user_input"  # 保留人工修改
        assert vm_name_var["acquisition_tool"] is None
        assert vm_name_var["auto_generated"] is False

    def test_new_vars_added(self):
        """测试新版新增变量：标记 auto_generated=True"""
        old_schema = [
            {"name": "node_ip", "description": "节点IP"},
        ]
        new_schema = [
            {"name": "node_ip", "description": ""},
            {"name": "disk_id", "acquisition_strategy": "tool"},  # 新增变量
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert len(merged) == 2
        assert deprecated == []

        node_ip_var = next(v for v in merged if v["name"] == "node_ip")
        assert node_ip_var["description"] == "节点IP"  # 保留人工编辑
        assert node_ip_var["auto_generated"] is False

        disk_id_var = next(v for v in merged if v["name"] == "disk_id")
        assert disk_id_var["auto_generated"] is True  # 新增变量标记

    def test_old_vars_removed(self):
        """测试旧版变量消失：标记 deprecated=True"""
        old_schema = [
            {"name": "node_ip", "description": "节点IP"},
            {"name": "vm_name", "description": "虚拟机"},
            {"name": "old_var", "description": "已废弃变量"},  # 将消失
        ]
        new_schema = [
            {"name": "node_ip", "description": ""},
            {"name": "vm_name", "description": ""},
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert len(merged) == 3
        assert deprecated == ["old_var"]

        # old_var 标记 deprecated
        old_var_var = next(v for v in merged if v["name"] == "old_var")
        assert old_var_var["deprecated"] is True
        assert old_var_var["description"] == "已废弃变量"  # 保留描述
        assert "auto_generated" not in old_var_var  # deprecated 变量无 auto_generated

    def test_deprecated_var_reappears(self):
        """测试 deprecated 变量重新出现：清除 deprecated 标记"""
        old_schema = [
            {"name": "node_ip", "description": "节点IP"},
            {"name": "vm_name", "deprecated": True},  # 旧版标记 deprecated
        ]
        new_schema = [
            {"name": "node_ip", "description": ""},
            {"name": "vm_name", "acquisition_strategy": "tool"},  # 重新出现
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert len(merged) == 2
        assert deprecated == []

        # vm_name 清除 deprecated 标记
        vm_name_var = next(v for v in merged if v["name"] == "vm_name")
        assert "deprecated" not in vm_name_var
        assert vm_name_var["auto_generated"] is False

    def test_preserve_all_human_fields(self):
        """测试保留所有人工编辑字段"""
        old_schema = [
            {
                "name": "cluster_name",
                "display_name": "集群名称",
                "description": "目标集群名称（人工编辑）",
                "acquisition_strategy": "user_confirm",  # 人工修改策略
                "acquisition_prompt": "请确认即将操作的集群名称",
                "validation_pattern": "^[a-zA-Z0-9_-]+$",
                "default_value": "default-cluster",
            },
        ]
        new_schema = [
            {
                "name": "cluster_name",
                "display_name": "cluster_name",  # 自动生成
                "description": "",  # 无描述
                "acquisition_strategy": "env_context",  # 自动推断
                "acquisition_prompt": None,
                "validation_pattern": None,
                "default_value": None,
            },
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert len(merged) == 1
        assert deprecated == []

        cluster_var = merged[0]
        # 所有人工编辑字段保留
        assert cluster_var["display_name"] == "集群名称"
        assert cluster_var["description"] == "目标集群名称（人工编辑）"
        assert cluster_var["acquisition_strategy"] == "user_confirm"
        assert cluster_var["acquisition_prompt"] == "请确认即将操作的集群名称"
        assert cluster_var["validation_pattern"] == "^[a-zA-Z0-9_-]+$"
        assert cluster_var["default_value"] == "default-cluster"
        assert cluster_var["auto_generated"] is False

    def test_complex_merge_scenario(self):
        """测试复杂场景：部分保留、部分新增、部分 deprecated"""
        old_schema = [
            {"name": "node_ip", "description": "节点IP（人工）"},
            {"name": "vm_name", "description": "虚拟机（人工）"},
            {"name": "disk_id", "description": "磁盘ID"},  # 将消失
            {"name": "nic_name", "deprecated": True},  # 已废弃但重新出现
        ]
        new_schema = [
            {"name": "node_ip", "description": ""},
            {"name": "vm_name", "description": ""},
            {"name": "nic_name", "acquisition_strategy": "tool"},  # 重新出现
            {"name": "volume_id", "acquisition_strategy": "tool"},  # 新增
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert len(merged) == 5  # node_ip, vm_name, nic_name, volume_id, disk_id
        assert deprecated == ["disk_id"]

        # node_ip、vm_name：保留人工描述
        node_ip_var = next(v for v in merged if v["name"] == "node_ip")
        assert node_ip_var["description"] == "节点IP（人工）"
        assert node_ip_var["auto_generated"] is False

        vm_name_var = next(v for v in merged if v["name"] == "vm_name")
        assert vm_name_var["description"] == "虚拟机（人工）"
        assert vm_name_var["auto_generated"] is False

        # nic_name：清除 deprecated，标记为已确认
        nic_name_var = next(v for v in merged if v["name"] == "nic_name")
        assert "deprecated" not in nic_name_var
        assert nic_name_var["auto_generated"] is False

        # volume_id：新增，标记 auto_generated
        volume_id_var = next(v for v in merged if v["name"] == "volume_id")
        assert volume_id_var["auto_generated"] is True

        # disk_id：deprecated
        disk_id_var = next(v for v in merged if v["name"] == "disk_id")
        assert disk_id_var["deprecated"] is True
        assert disk_id_var["description"] == "磁盘ID"

    def test_markdown_specific_strategy_overrides_old_generic_user_input(self):
        """测试 Markdown 新声明的具体来源能修正历史误落的 user_input"""
        old_schema = [
            {
                "name": "disk_dev",
                "description": "磁盘盘符",
                "acquisition_strategy": "user_input",
                "acquisition_tool": None,
            }
        ]
        new_schema = [
            {
                "name": "disk_dev",
                "description": "磁盘盘符",
                "acquisition_strategy": "llm_inference",
                "acquisition_tool": None,
                "auto_generated": False,
            }
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert deprecated == []
        assert merged[0]["acquisition_strategy"] == "llm_inference"

    def test_markdown_specific_strategy_overrides_old_specific_strategy(self):
        """测试 Markdown 明确声明的新来源能修正旧的具体来源"""
        old_schema = [
            {
                "name": "node_ip",
                "description": "旧描述",
                "acquisition_strategy": "env_injection",
                "acquisition_tool": "env:node_ip",
            }
        ]
        new_schema = [
            {
                "name": "node_ip",
                "description": "告警硬盘所在主机",
                "acquisition_strategy": "skill_call",
                "acquisition_tool": "hci-alert-parsing",
                "output_path": "node_ip",
                "depends_on": ["alert_logs"],
                "auto_generated": False,
            }
        ]

        merged, deprecated = merge_variable_schema(old_schema, new_schema)

        assert deprecated == []
        assert merged[0]["acquisition_strategy"] == "skill_call"
        assert merged[0]["acquisition_tool"] == "hci-alert-parsing"
        assert merged[0]["output_path"] == "node_ip"
        assert merged[0]["depends_on"] == ["alert_logs"]
