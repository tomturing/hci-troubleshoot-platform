"""
KB Service — SOP 变量提取单元测试

测试覆盖：
1. 变量占位符解析（{placeholder} 格式）
2. ## 变量 章节解析
3. 启发式策略推断（node_ip → env_injection, vm_name → tool_call）
4. 双向校验：Undeclared = Error, Orphan = Warning
5. tree_json 变量扫描
6. 本地变量声明表格与层级校验
7. sop_default 策略默认值降级提取说明
"""

from app.schemas.sop_template import (
    DiagnosisDetail,
    PrerequisiteItem,
    SolutionDetail,
    SOPNode,
    VariableDeclaration,
)
from app.services.sop_parser import (
    _extract_vars_from_text,
    _extract_vars_from_tree,
    _infer_strategy,
    _parse_variable_section,
    extract_sop_variables,
)

# ──────────────────────────────────────────────────────────────────────────────
# 测试：变量名提取
# ──────────────────────────────────────────────────────────────────────────────


class TestExtractVarsFromText:
    """测试从文本中提取 {placeholder} 格式变量"""

    def test_single_variable(self):
        """单个变量提取"""
        text = "请检查 {node_ip} 的状态"
        assert _extract_vars_from_text(text) == {"node_ip"}

    def test_multiple_variables(self):
        """多个变量提取"""
        text = "检查 {node_ip} 上的 {vm_name}，磁盘 ID 为 {disk_id}"
        assert _extract_vars_from_text(text) == {"node_ip", "vm_name", "disk_id"}

    def test_nested_braces_not_matched(self):
        """嵌套大括号不应匹配"""
        text = "执行命令 acli {{vm_list}} --filter {vm_name}"
        # 只匹配单层 {var_name} 格式
        assert _extract_vars_from_text(text) == {"vm_name"}

    def test_invalid_var_name_not_matched(self):
        """非法变量名（大写开头）不匹配"""
        text = "变量 {Node_IP} 和 {VM_NAME} 不应被提取"
        assert _extract_vars_from_text(text) == set()

    def test_empty_text(self):
        """空文本返回空集"""
        assert _extract_vars_from_text("") == set()

    def test_no_variables(self):
        """无变量占位符返回空集"""
        text = "这是一段普通文本，没有变量占位符"
        assert _extract_vars_from_text(text) == set()


# ──────────────────────────────────────────────────────────────────────────────
# 测试：启发式策略推断
# ──────────────────────────────────────────────────────────────────────────────


class TestInferStrategy:
    """测试变量名启发式策略推断"""

    def test_ip_suffix_env_context(self):
        """*_ip 后缀推断为 env_injection（规范名，旧名为 env_context）"""
        result = _infer_strategy("node_ip")
        assert result["acquisition_strategy"] == "env_injection"
        assert result["acquisition_tool"] is None

    def test_cluster_ip_env_context(self):
        """cluster_ip 推断为 env_injection（规范名，旧名为 env_context）"""
        result = _infer_strategy("cluster_ip")
        assert result["acquisition_strategy"] == "env_injection"

    def test_vm_name_tool(self):
        """vm_name 推断为 tool_call:get_vm_list（规范名，旧名为 tool）"""
        result = _infer_strategy("vm_name")
        assert result["acquisition_strategy"] == "tool_call"
        assert result["acquisition_tool"] == "get_vm_list"

    def test_disk_id_tool(self):
        """disk_id 推断为 tool_call:acli_storage_disk_list（规范名，旧名为 tool）"""
        result = _infer_strategy("disk_id")
        assert result["acquisition_strategy"] == "tool_call"
        assert result["acquisition_tool"] == "acli_storage_disk_list"

    def test_unknown_var_user_input(self):
        """未知变量名推断为 user_input"""
        result = _infer_strategy("custom_param")
        assert result["acquisition_strategy"] == "user_input"
        assert result["acquisition_tool"] is None


# ──────────────────────────────────────────────────────────────────────────────
# 测试：决策树变量扫描
# ──────────────────────────────────────────────────────────────────────────────


class TestExtractVarsFromTree:
    """测试从 SOPNode 决策树中提取变量"""

    def test_leaf_node_with_solution(self):
        """叶节点 solution 中变量提取"""
        leaf = SOPNode(
            id="n-1-1",
            title="Redis OOM",
            level=4,
            line_number=1,
            diagnosis=DiagnosisDetail(
                acli_methods=["acli check {node_ip}"],
                page_methods=["检查 {node_ip} 上的 Redis 内存使用"],
            ),
            solution=SolutionDetail(
                quick_recovery=["在 {vm_name} 上执行重启"],
                thorough_fix=["调整 {cluster_name} 的内存配置"],
            ),
        )
        vars_set = _extract_vars_from_tree(leaf)
        assert vars_set == {"node_ip", "vm_name", "cluster_name"}

    def test_routing_node_prerequisites(self):
        """中间节点 prerequisites 变量提取"""
        routing = SOPNode(
            id="n-1",
            title="虚拟机故障",
            level=2,
            line_number=1,
            prerequisite_items=[PrerequisiteItem(description="确认 {node_ip} 是否可达", type="filter")],
            children=[
                SOPNode(id="n-1-1", title="VM 启动失败", level=3, line_number=2),
            ],
        )
        vars_set = _extract_vars_from_tree(routing)
        assert vars_set == {"node_ip"}

    def test_nested_tree(self):
        """多层嵌套树变量提取"""
        root = SOPNode(
            id="n-1",
            title="服务组件异常",
            level=1,
            line_number=1,
            prerequisite_items=[PrerequisiteItem(description="检查 {cluster_ip} 的健康状态", type="filter")],
            children=[
                SOPNode(
                    id="n-1-1",
                    title="Redis OOM",
                    level=2,
                    line_number=2,
                    diagnosis=DiagnosisDetail(
                        page_methods=["检查 {node_ip} 的 Redis 内存"],
                        acli_methods=["acli redis info {vm_name}"],
                    ),
                    solution=SolutionDetail(
                        quick_recovery=["重启 {vm_name}"],
                        thorough_fix=["扩容 {disk_id} 存储"],
                    ),
                ),
            ],
        )
        vars_set = _extract_vars_from_tree(root)
        assert vars_set == {"cluster_ip", "node_ip", "vm_name", "disk_id"}


# ──────────────────────────────────────────────────────────────────────────────
# 测试：变量章节解析
# ──────────────────────────────────────────────────────────────────────────────


class TestParseVariableSection:
    """测试 ## 变量 章节解析"""

    def test_basic_variable_section(self):
        """基础变量章节解析"""
        content = """
## 变量

- node_ip：节点 IP 地址，从环境上下文获取
- vm_name：虚拟机名称，需要调用工具获取
"""
        declared = _parse_variable_section(content)
        assert "node_ip" in declared
        assert "vm_name" in declared
        assert declared["node_ip"]["description"] == "节点 IP 地址，从环境上下文获取"
        assert declared["node_ip"]["acquisition_strategy"] == "env_injection"  # 规范名（旧名为 env_context）
        assert declared["vm_name"]["acquisition_strategy"] == "tool_call"  # 规范名（旧名为 tool）

    def test_no_variable_section(self):
        """无变量章节返回空字典"""
        content = """
## 判断方法

检查服务状态是否正常
"""
        declared = _parse_variable_section(content)
        assert declared == {}

    def test_equivalent_keywords(self):
        """等效关键词（参数定义）解析"""
        content = """
## 参数定义

- cluster_name：集群名称
"""
        declared = _parse_variable_section(content)
        assert "cluster_name" in declared

    def test_section_ends_at_next_heading(self):
        """变量章节在下一个标题处结束"""
        content = """
## 变量

- node_ip：节点 IP

## 判断方法

检查 {node_ip} 状态
"""
        declared = _parse_variable_section(content)
        assert "node_ip" in declared
        assert declared["node_ip"]["description"] == "节点 IP"


# ──────────────────────────────────────────────────────────────────────────────
# 测试：完整变量提取 + 双向校验
# ──────────────────────────────────────────────────────────────────────────────


class TestExtractSopVariables:
    """测试完整变量提取 + 双向校验"""

    def test_declared_and_used(self):
        """声明且使用的变量 → 正常提取"""
        content = """
## 变量

- node_ip：节点 IP 地址

## 判断方法

检查 {node_ip} 的状态
"""
        defs, undeclared, orphan = extract_sop_variables(content)
        assert len(defs) == 1
        assert defs[0]["name"] == "node_ip"
        assert defs[0]["auto_generated"] is False
        assert undeclared == []
        assert orphan == []

    def test_undeclared_variable(self):
        """未声明但使用的变量 → undeclared_errors"""
        content = """
## 判断方法

检查 {node_ip} 和 {vm_name} 的状态
"""
        defs, undeclared, orphan = extract_sop_variables(content)
        assert len(defs) == 2  # 两个变量都被提取（自动推断）
        assert defs[0]["auto_generated"] is True
        assert undeclared == ["node_ip", "vm_name"]
        assert orphan == []

    def test_orphan_variable(self):
        """声明但未使用的变量 → orphan_warnings"""
        content = """
## 变量

- node_ip：节点 IP 地址
- unused_var：未使用的变量

## 判断方法

检查 {node_ip} 的状态
"""
        defs, undeclared, orphan = extract_sop_variables(content)
        assert len(defs) == 2  # 提取实际使用的和已声明的变量
        assert defs[0]["name"] == "node_ip"
        assert defs[1]["name"] == "unused_var"
        assert undeclared == []
        assert orphan == ["unused_var"]

    def test_with_tree(self):
        """带决策树的变量提取"""
        content = """
## 变量

- node_ip：节点 IP

# Redis OOM

## 判断方法

检查 {node_ip} 的 Redis 内存

## 解决方案

- 重启 {vm_name}
"""
        tree = SOPNode(
            id="n-1",
            title="Redis OOM",
            level=1,
            line_number=1,
            diagnosis=DiagnosisDetail(
                acli_methods=["acli check {node_ip}"],
                page_methods=["检查 {node_ip} 的 Redis 内存"],
            ),
            solution=SolutionDetail(
                quick_recovery=["重启 {vm_name}"],
                thorough_fix=["调整配置"],
            ),
        )
        defs, undeclared, orphan = extract_sop_variables(content, tree)
        assert len(defs) == 2
        assert "node_ip" in [d["name"] for d in defs]
        assert "vm_name" in [d["name"] for d in defs]
        assert undeclared == ["vm_name"]  # vm_name 未声明
        assert orphan == []

    def test_inferred_strategy(self):
        """自动推断获取策略"""
        content = """
检查 {node_ip} 上的 {vm_name}
"""
        defs, undeclared, orphan = extract_sop_variables(content)
        node_ip_def = next(d for d in defs if d["name"] == "node_ip")
        vm_name_def = next(d for d in defs if d["name"] == "vm_name")

        assert node_ip_def["acquisition_strategy"] == "env_injection"  # 规范名（旧名为 env_context）
        assert vm_name_def["acquisition_strategy"] == "tool_call"  # 规范名（旧名为 tool）
        assert vm_name_def["acquisition_tool"] == "get_vm_list"
        assert node_ip_def["auto_generated"] is True
        assert vm_name_def["auto_generated"] is True

    def test_empty_content(self):
        """空内容返回空"""
        defs, undeclared, orphan = extract_sop_variables("")
        assert defs == []
        assert undeclared == []
        assert orphan == []

    def test_variable_count_matches_used(self):
        """variable_count 应等于实际使用的变量数"""
        content = """
## 变量

- node_ip：节点 IP
- vm_name：虚拟机

## 判断方法

检查 {node_ip} 的状态
"""
        defs, undeclared, orphan = extract_sop_variables(content)
        # 实际使用的与已声明的都应在 defs 中
        assert len(defs) == 2
        assert defs[0]["name"] == "node_ip"
        assert defs[1]["name"] == "vm_name"
        assert orphan == ["vm_name"]

    def test_local_variables_table_and_hierarchical_validation(self):
        """测试本地变量声明表的解析与层级校验"""
        content = """
## 变量声明
| 变量名 | 类型 | 来源 | 说明 |
|---|---|---|---|
| node_ip | ip | env_injection | 节点 IP |

## 系统盘故障
##### 变量声明
| 变量 | 类型 | 来源 | 说明 |
|---|---|---|---|
| disk_dev | string | user_input | 系统盘盘符 |

##### 判断方法
acli判断方法：
检查 {node_ip} 上的 {disk_dev}
"""
        # 模拟解析器生成的树
        root = SOPNode(
            id="n-1",
            title="系统盘故障",
            level=2,
            line_number=7,
            variables=[
                VariableDeclaration(name="disk_dev", type="string", source="user_input", description="系统盘盘符")
            ],
            diagnosis=DiagnosisDetail(
                acli_methods=["检查 {node_ip} 上的 {disk_dev}"],
            ),
        )
        # 双向校验应能提取所有局部和全局变量声明且无 undeclared 错误
        defs, undeclared, orphan = extract_sop_variables(content, root)
        assert "node_ip" in [d["name"] for d in defs]
        assert "disk_dev" in [d["name"] for d in defs]
        assert undeclared == []
        assert orphan == []

    def test_sop_default_fallback_to_description(self):
        """测试 sop_default 策略在缺失默认值列时降级取说明列内容"""
        content = """
## 变量声明
| 变量名 | 类型 | 来源 | 说明 |
|---|---|---|---|
| check_meth | string | sop_default | 硬盘返修判断方法 |
"""
        declared = _parse_variable_section(content)
        assert "check_meth" in declared
        assert declared["check_meth"]["acquisition_strategy"] == "sop_default"
        assert declared["check_meth"]["default_value"] == "硬盘返修判断方法"

    def test_skill_call_strategy(self):
        """测试 skill_call 策略的解析"""
        content = """
## 变量声明
| 变量名 | 类型 | 来源 | 说明 |
|---|---|---|---|
| check_meth | string | skill:disk_vendor_lifetime | 硬盘返修判断方法 |
"""
        declared = _parse_variable_section(content)
        assert "check_meth" in declared
        assert declared["check_meth"]["acquisition_strategy"] == "skill_call"
        assert declared["check_meth"]["acquisition_tool"] == "disk_vendor_lifetime"

    def test_llm_inference_and_agent_pass_strategy(self):
        """测试 llm_inference/agent_pass 策略不会被降级为 user_input"""
        content = """
## 变量声明
| 变量名 | 类型 | 来源 | 说明 |
|---|---|---|---|
| disk_dev | string | llm_inference | 磁盘盘符 |
| check_meth | string | agent_pass | 诊断方法 |
"""
        declared = _parse_variable_section(content)
        assert declared["disk_dev"]["acquisition_strategy"] == "llm_inference"
        assert declared["check_meth"]["acquisition_strategy"] == "agent_pass"

    def test_env_key_strategy_keeps_source_key_as_tool_metadata(self):
        """测试 env:xxx 来源解析为 env_injection，环境变量名存储在 acquisition_tool 字段（language-level parameter）"""
        content = """
## 变量声明
| 变量名 | 类型 | 来源 | 说明 |
|---|---|---|---|
| node_ip | string | env:node_ip | 节点 IP |
"""
        declared = _parse_variable_section(content)
        assert declared["node_ip"]["acquisition_strategy"] == "env_injection"
        # env:xxx 的参数（环境变量名）存储在 acquisition_tool 字段，方便引擎查找具体的环境变量 key
        assert declared["node_ip"]["acquisition_tool"] == "node_ip"

    def test_skill_output_path_and_depends_on_parsing(self):
        """测试动态 Skill 输出绑定和变量依赖解析"""
        content = """
## 变量声明
| 变量名 | 类型 | 来源 | 说明 | 依赖 | 输出路径 | 失败兜底 |
|---|---|---|---|---|---|---|
| node_ip | string | skill:hci-alert-parsing | 告警节点 | alert_logs | values.node_ip | |
| check_meth | string | skill:hci-disk-vendor-lifetime | 寿命判断 | smart_info,node_ip | value | user_input |
"""
        declared = _parse_variable_section(content)

        assert declared["node_ip"]["acquisition_strategy"] == "skill_call"
        assert declared["node_ip"]["acquisition_tool"] == "hci-alert-parsing"
        assert declared["node_ip"]["depends_on"] == ["alert_logs"]
        assert declared["node_ip"]["output_path"] == "values.node_ip"
        assert declared["check_meth"]["depends_on"] == ["smart_info", "node_ip"]
        assert declared["check_meth"]["fallback_strategy"] == "user_input"

    def test_tool_args_template_and_derived_expression_parsing(self):
        """测试工具参数模板和派生表达式解析"""
        content = """
## 变量声明
| 变量名 | 类型 | 来源 | 说明 | 依赖 | 输出路径 | 失败兜底 | 参数模板 | 表达式 |
|---|---|---|---|---|---|---|---|---|
| smart_info | string | tool:bash_exec | SMART原始回显 | disk_dev,node_ip | stdout | | {"container":"vs-cp-manager","command":"smartctl -a /dev/{disk_dev}","node_ip":"{node_ip}","reason":"采集SMART"} | |
| is_sys_disk | boolean | derived | 是否系统盘 | alert_type | | | | contains(alert_type, 'vs') ? false : unknown |
"""
        declared = _parse_variable_section(content)

        assert declared["smart_info"]["acquisition_strategy"] == "tool_call"
        assert declared["smart_info"]["acquisition_tool"] == "bash_exec"
        assert declared["smart_info"]["depends_on"] == ["disk_dev", "node_ip"]
        assert declared["smart_info"]["output_path"] == "stdout"
        assert declared["smart_info"]["acquisition_args_template"] == {
            "container": "vs-cp-manager",
            "command": "smartctl -a /dev/{disk_dev}",
            "node_ip": "{node_ip}",
            "reason": "采集SMART",
        }
        assert declared["is_sys_disk"]["acquisition_strategy"] == "derived"
        assert declared["is_sys_disk"]["expression"] == "contains(alert_type, 'vs') ? false : unknown"
