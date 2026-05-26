"""
SOP 解析器单元测试

测试 sop_parser 模块：关键词分类、Markdown 解析、树构建、叶节点校验。
测试不依赖数据库或外部服务，全部为纯函数单元测试。
"""

from __future__ import annotations

from app.config.template_config_loader import get_keywords, reload_config
from app.schemas.sop_template import SOPValidationResult
from app.services.sop_parser import classify_heading, parse_sop_markdown

# ──────────────────────────────────────────────────────────────────────────────
# 测试用 Markdown 片段
# ──────────────────────────────────────────────────────────────────────────────

# 标准两层 SOP（H1→H2 叶节点，标准话术，带字段标签）
_SIMPLE_SOP = """\
# 虚拟机启动失败

## CPU 资源不足

### 判断方法

页面判断方法：
- 查看 vCenter 中 CPU 剩余可配置量
- 登录管理台 → 资源池 → 剩余 CPU 核数

acli命令行：
- acli vm.get <vm_name>

### 解决方案

快速恢复：
- 迁移其他虚拟机腾出资源

彻底解决方案：
- 向集群扩容 CPU 资源
"""

# 深层 SOP（H1→H2→H3→H4 叶节点，带 acli 字段）
_DEEP_SOP = """\
# 存储服务异常

## NFS 挂载失败

### 客户端权限不足

#### 判断方法

页面判断方法：
- 查看挂载状态页面

acli命令行：
- showmount -e <nfs-server>

#### 解决方案

快速恢复：
- 在 NFS 服务端临时放开 exports

彻底解决方案：
- 正式申请挂载权限并重新配置 exports
"""

# 非标准话术（"处理方法" 作为 solution，"排查方法" 作为 diagnosis）
_NONSTANDARD_SOP = """\
# Redis 故障

## Redis OOM

### 处理方法

- 清理过期 key

### 排查方法

acli命令行：
- 执行 INFO memory 查看内存占用
"""

# 缺少 solution 的 SOP（叶节点无解决方案）
_MISSING_SOLUTION_SOP = """\
# 网络故障

## DNS 解析失败

### 判断方法

acli命令行：
- 执行 nslookup 确认 DNS 不通
"""

# 缺少 diagnosis 的 SOP（叶节点无判断方法）
_MISSING_DIAGNOSIS_SOP = """\
# 磁盘故障

## 磁盘 I/O 过高

### 解决方案

快速恢复：
- 重启 IO 密集进程

彻底解决方案：
- 更换高速磁盘
"""


# ──────────────────────────────────────────────────────────────────────────────
# classify_heading 测试
# ──────────────────────────────────────────────────────────────────────────────


class TestClassifyHeading:
    """测试 classify_heading 函数"""

    def test_standard_diagnosis_keyword(self):
        """标准关键词「判断方法」→ diagnosis"""
        assert classify_heading("判断方法") == "diagnosis"

    def test_all_diagnosis_variants(self):
        """配置中的所有 diagnosis 关键词均识别为 diagnosis"""
        for kw in get_keywords("diagnosis"):
            assert classify_heading(kw) == "diagnosis", f"「{kw}」应识别为 diagnosis"

    def test_diagnosis_in_longer_heading(self):
        """标题包含关键词即可（不要求完全匹配）"""
        assert classify_heading("Redis OOM 判断方法详解") == "diagnosis"

    def test_standard_solution_keyword(self):
        """标准关键词「解决方案」→ solution"""
        assert classify_heading("解决方案") == "solution"

    def test_all_solution_variants(self):
        """配置中的所有 solution 关键词均识别为 solution"""
        for kw in get_keywords("solution"):
            assert classify_heading(kw) == "solution", f"「{kw}」应识别为 solution"

    def test_solution_in_longer_heading(self):
        """标题包含解决方案关键词即可"""
        assert classify_heading("Redis OOM 处理方法说明") == "solution"

    def test_plain_node_heading(self):
        """普通标题 → node"""
        assert classify_heading("Redis 内存不足") == "node"

    def test_service_name_heading(self):
        """服务名称 → node"""
        assert classify_heading("服务组件异常") == "node"

    def test_empty_string(self):
        """空字符串 → node（无关键词）"""
        assert classify_heading("") == "node"

    def test_structural_suffix_not_classified(self):
        """包含关键词但以结构性后缀（概述/汇总）结尾的标题 → node（中间节点）"""
        assert classify_heading("判断方法概述") == "node", "章节汇总标题不应识别为 diagnosis"
        assert classify_heading("解决方案汇总") == "node", "章节汇总标题不应识别为 solution"
        assert classify_heading("排查方法总览") == "node", "目录型标题不应识别为 diagnosis"


# ──────────────────────────────────────────────────────────────────────────────
# 空文档测试
# ──────────────────────────────────────────────────────────────────────────────


class TestParseEmptyDocument:
    """测试空文档/无标题文档"""

    def test_empty_string(self):
        """空字符串 → has_error=True"""
        result = parse_sop_markdown("")
        assert isinstance(result, SOPValidationResult)
        assert result.has_error is True
        assert len(result.root_nodes) == 0
        assert len(result.issues) > 0

    def test_whitespace_only(self):
        """纯空白 → has_error=True"""
        result = parse_sop_markdown("   \n\n   ")
        assert result.has_error is True

    def test_no_headings(self):
        """无标题的纯文本 → has_error=True"""
        result = parse_sop_markdown("这是一些没有标题的文本\n- 列表项")
        assert result.has_error is True


# ──────────────────────────────────────────────────────────────────────────────
# 标准 SOP 解析测试
# ──────────────────────────────────────────────────────────────────────────────


class TestParseSimpleSop:
    """测试标准 H1→H2 SOP 结构"""

    def test_no_errors(self):
        """标准 SOP → has_error=False"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.has_error is False
        assert len(result.root_nodes) > 0

    def test_root_title(self):
        """根节点标题正确"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert len(result.root_nodes) > 0
        assert result.root_nodes[0].title == "虚拟机启动失败"

    def test_tree_structure(self):
        """树结构：H1 → H2（叶节点）"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        root = result.root_nodes[0]
        assert len(root.children) == 1
        leaf = root.children[0]
        assert leaf.title == "CPU 资源不足"
        assert len(leaf.children) == 0

    def test_leaf_diagnosis_acli_methods(self):
        """叶节点 diagnosis.acli_methods 正确解析"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        root = result.root_nodes[0]
        leaf = root.children[0]
        assert leaf.diagnosis is not None
        assert len(leaf.diagnosis.acli_methods) > 0
        assert "acli vm.get <vm_name>" in leaf.diagnosis.acli_methods

    def test_leaf_diagnosis_page_methods(self):
        """叶节点 diagnosis.page_methods 正确解析"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        root = result.root_nodes[0]
        leaf = root.children[0]
        assert leaf.diagnosis is not None
        assert "查看 vCenter 中 CPU 剩余可配置量" in leaf.diagnosis.page_methods

    def test_leaf_solution_quick_recovery(self):
        """叶节点 solution.quick_recovery 正确解析"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        root = result.root_nodes[0]
        leaf = root.children[0]
        assert leaf.solution is not None
        assert "迁移其他虚拟机腾出资源" in leaf.solution.quick_recovery

    def test_leaf_solution_thorough_fix(self):
        """叶节点 solution.thorough_fix 正确解析"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        root = result.root_nodes[0]
        leaf = root.children[0]
        assert leaf.solution is not None
        assert "向集群扩容 CPU 资源" in leaf.solution.thorough_fix


# ──────────────────────────────────────────────────────────────────────────────
# 深层 SOP 测试
# ──────────────────────────────────────────────────────────────────────────────


class TestParseDeepSop:
    """测试 H1→H2→H3→H4 SOP 结构"""

    def test_no_errors(self):
        """深层 SOP → has_error=False"""
        result = parse_sop_markdown(_DEEP_SOP)
        assert result.has_error is False
        assert len(result.root_nodes) > 0

    def test_tree_depth(self):
        """树深度正确（H3 为叶节点）"""
        result = parse_sop_markdown(_DEEP_SOP)
        root = result.root_nodes[0]
        h2 = root.children[0]
        h3 = h2.children[0]
        assert len(h3.children) == 0

    def test_acli_methods_parsed(self):
        """acli_methods 字段正确解析"""
        result = parse_sop_markdown(_DEEP_SOP)
        root = result.root_nodes[0]
        leaf = root.children[0].children[0]
        assert leaf.diagnosis is not None
        assert len(leaf.diagnosis.acli_methods) > 0
        assert "showmount -e <nfs-server>" in leaf.diagnosis.acli_methods

    def test_page_methods_parsed(self):
        """page_methods 字段正确解析"""
        result = parse_sop_markdown(_DEEP_SOP)
        root = result.root_nodes[0]
        leaf = root.children[0].children[0]
        assert leaf.diagnosis is not None
        assert "查看挂载状态页面" in leaf.diagnosis.page_methods


# ──────────────────────────────────────────────────────────────────────────────
# 非标准话术测试
# ──────────────────────────────────────────────────────────────────────────────


class TestNonstandardHeading:
    """测试非标准话术（等效关键词）"""

    def test_has_errors_due_to_nonstandard(self):
        """非标准话术 → has_error=True（W-9/W-10 升级为 error）"""
        result = parse_sop_markdown(_NONSTANDARD_SOP)
        assert result.has_error is True
        assert len(result.issues) > 0

    def test_error_message_mentions_nonstandard_wording(self):
        """error 消息包含「话术不规范」"""
        result = parse_sop_markdown(_NONSTANDARD_SOP)
        msgs = [i.message for i in result.issues]
        assert any("话术不规范" in m for m in msgs)


# ──────────────────────────────────────────────────────────────────────────────
# 叶节点缺失测试
# ──────────────────────────────────────────────────────────────────────────────


class TestMissingLeafContent:
    """测试叶节点缺少 diagnosis 或 solution"""

    def test_missing_solution_has_error(self):
        """缺少 solution → has_error=True"""
        result = parse_sop_markdown(_MISSING_SOLUTION_SOP)
        assert result.has_error is True

    def test_missing_solution_error_message(self):
        """issue 消息包含「解决方案」"""
        result = parse_sop_markdown(_MISSING_SOLUTION_SOP)
        msgs = [i.message for i in result.issues]
        assert any("解决方案" in m for m in msgs)

    def test_missing_diagnosis_has_error(self):
        """缺少 diagnosis → has_error=True"""
        result = parse_sop_markdown(_MISSING_DIAGNOSIS_SOP)
        assert result.has_error is True

    def test_missing_diagnosis_error_message(self):
        """issue 消息包含「判断方法」或「acli」"""
        result = parse_sop_markdown(_MISSING_DIAGNOSIS_SOP)
        msgs = [i.message for i in result.issues]
        assert any("判断方法" in m or "acli" in m for m in msgs)


# ──────────────────────────────────────────────────────────────────────────────
# node_id 分配测试
# ──────────────────────────────────────────────────────────────────────────────


class TestNodeIdAssignment:
    """测试节点 ID 分配"""

    def _collect_node_ids(self, node, ids=None):
        """递归收集所有节点 ID"""
        if ids is None:
            ids = []
        ids.append(node.id)
        for child in node.children:
            self._collect_node_ids(child, ids)
        return ids

    def test_root_id_is_n1(self):
        """根节点 id == 'n-1'"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert len(result.root_nodes) > 0
        assert result.root_nodes[0].id == "n-1"

    def test_first_child_id(self):
        """第一个子节点 id == 'n-1-1'"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        root = result.root_nodes[0]
        assert root.children[0].id == "n-1-1"

    def test_all_nodes_have_non_empty_id(self):
        """所有节点均分配了非空 id"""
        result = parse_sop_markdown(_DEEP_SOP)
        ids = self._collect_node_ids(result.root_nodes[0])
        for nid in ids:
            assert nid, "存在未分配 id 的节点"
            assert nid.startswith("n-")

    def test_no_duplicate_ids(self):
        """id 无重复"""
        result = parse_sop_markdown(_DEEP_SOP)
        ids = self._collect_node_ids(result.root_nodes[0])
        assert len(ids) == len(set(ids)), "存在重复的 id"


# ──────────────────────────────────────────────────────────────────────────────
# 行号追踪测试
# ──────────────────────────────────────────────────────────────────────────────


class TestLineNumberTracking:
    """测试 ValidationIssue.line_number 追踪"""

    def test_root_node_line_number(self):
        """根节点 line_number 为标题所在行"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        root = result.root_nodes[0]
        assert root.line_number == 1

    def test_child_node_line_number(self):
        """子节点 line_number 为其标题所在行"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        root = result.root_nodes[0]
        child = root.children[0]
        assert child.line_number == 3

    def test_issue_line_number_present(self):
        """校验问题携带 line_number"""
        result = parse_sop_markdown(_MISSING_SOLUTION_SOP)
        assert result.has_error
        for issue in result.issues:
            if issue.line_number is not None:
                assert issue.line_number > 0


# ──────────────────────────────────────────────────────────────────────────────
# 配置热重载测试
# ──────────────────────────────────────────────────────────────────────────────


class TestConfigReload:
    """测试配置热重载"""

    def test_reload_config_clears_cache(self):
        """reload_config 清除缓存"""
        reload_config()
        keywords = get_keywords("diagnosis")
        assert len(keywords) > 0
        reload_config()
        keywords2 = get_keywords("diagnosis")
        assert keywords == keywords2
