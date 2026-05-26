"""
KB Service — SOP 变量提取单元测试

测试覆盖：
1. 变量占位符解析（{placeholder} 格式）
2. ## 变量 章节解析
3. 启发式策略推断（node_ip → env_context, vm_name → tool）
4. 双向校验：Undeclared = Error, Orphan = Warning
5. tree_json 变量扫描
"""


from app.schemas.sop_template import DiagnosisDetail, SolutionDetail, SOPNode
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
        """*_ip 后缀推断为 env_context"""
        result = _infer_strategy("node_ip")
        assert result["acquisition_strategy"] == "env_context"
        assert result["acquisition_tool"] is None

    def test_cluster_ip_env_context(self):
        """cluster_ip 推断为 env_context"""
        result = _infer_strategy("cluster_ip")
        assert result["acquisition_strategy"] == "env_context"

    def test_vm_name_tool(self):
        """vm_name 推断为 tool:get_vm_list"""
        result = _infer_strategy("vm_name")
        assert result["acquisition_strategy"] == "tool"
        assert result["acquisition_tool"] == "get_vm_list"

    def test_disk_id_tool(self):
        """disk_id 推断为 tool:acli_storage_disk_list"""
        result = _infer_strategy("disk_id")
        assert result["acquisition_strategy"] == "tool"
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
            name="Redis OOM",
            level=4,
            diagnosis=DiagnosisDetail(
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
            name="虚拟机故障",
            level=2,
            prerequisites=["确认 {node_ip} 是否可达"],
            children=[
                SOPNode(name="VM 启动失败", level=3),
            ],
        )
        vars_set = _extract_vars_from_tree(routing)
        assert vars_set == {"node_ip"}

    def test_nested_tree(self):
        """多层嵌套树变量提取"""
        root = SOPNode(
            name="服务组件异常",
            level=1,
            prerequisites=["检查 {cluster_ip} 的健康状态"],
            children=[
                SOPNode(
                    name="Redis OOM",
                    level=2,
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
        assert declared["node_ip"]["acquisition_strategy"] == "env_context"
        assert declared["vm_name"]["acquisition_strategy"] == "tool"

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
        # 应该只解析到 node_ip，且内容为空字典（无额外属性）
        assert declared == {"node_ip": {}}

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
        assert len(defs) == 1  # 只提取实际使用的变量
        assert defs[0]["name"] == "node_ip"
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
            name="Redis OOM",
            level=1,
            diagnosis=DiagnosisDetail(
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

        assert node_ip_def["acquisition_strategy"] == "env_context"
        assert vm_name_def["acquisition_strategy"] == "tool"
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
        # 只有 node_ip 被使用，所以 defs 只有 1 个
        assert len(defs) == 1
        assert defs[0]["name"] == "node_ip"
        assert orphan == ["vm_name"]
