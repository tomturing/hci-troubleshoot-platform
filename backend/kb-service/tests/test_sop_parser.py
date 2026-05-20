"""
SOP 解析器单元测试

测试 sop_parser 模块：关键词分类、Markdown 解析、树构建、叶节点校验。
测试不依赖数据库或外部服务，全部为纯函数单元测试。
"""


from app.schemas.sop_template import SOPValidationResult
from app.services.sop_parser import (
    DIAGNOSIS_KEYWORDS,
    SOLUTION_KEYWORDS,
    STANDARD_DIAGNOSIS_HEADING,
    STANDARD_SOLUTION_HEADING,
    classify_heading,
    parse_sop_markdown,
)

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

- 执行 INFO memory 查看内存占用
"""

# 缺少 solution 的 SOP（叶节点无解决方案）
_MISSING_SOLUTION_SOP = """\
# 网络故障

## DNS 解析失败

### 判断方法

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
        """DIAGNOSIS_KEYWORDS 中所有等效词均识别为 diagnosis"""
        for kw in DIAGNOSIS_KEYWORDS:
            assert classify_heading(kw) == "diagnosis", f"「{kw}」应识别为 diagnosis"

    def test_diagnosis_in_longer_heading(self):
        """标题包含关键词即可（不要求完全匹配）"""
        assert classify_heading("Redis OOM 判断方法详解") == "diagnosis"

    def test_standard_solution_keyword(self):
        """标准关键词「解决方案」→ solution"""
        assert classify_heading("解决方案") == "solution"

    def test_all_solution_variants(self):
        """SOLUTION_KEYWORDS 中所有等效词均识别为 solution"""
        for kw in SOLUTION_KEYWORDS:
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
        """空字符串 → is_valid=False"""
        result = parse_sop_markdown("")
        assert isinstance(result, SOPValidationResult)
        assert result.is_valid is False
        assert result.tree is None
        assert len(result.errors) > 0

    def test_whitespace_only(self):
        """纯空白 → is_valid=False"""
        result = parse_sop_markdown("   \n\n   ")
        assert result.is_valid is False

    def test_no_headings(self):
        """无标题的纯文本 → is_valid=False"""
        result = parse_sop_markdown("这是一些没有标题的文本\n- 列表项")
        assert result.is_valid is False


# ──────────────────────────────────────────────────────────────────────────────
# 标准 SOP 解析测试
# ──────────────────────────────────────────────────────────────────────────────


class TestParseSimpleSop:
    """测试标准 H1→H2 SOP 结构"""

    def test_is_valid(self):
        """标准 SOP → is_valid=True，无 error"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.is_valid is True
        assert result.tree is not None
        assert result.errors == []

    def test_root_name(self):
        """根节点名称正确"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.tree is not None
        assert result.tree.name == "虚拟机启动失败"

    def test_tree_structure(self):
        """树结构：H1 → H2（叶节点）"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        tree = result.tree
        assert tree is not None
        assert len(tree.children) == 1
        leaf = tree.children[0]
        assert leaf.name == "CPU 资源不足"
        assert leaf.is_leaf

    def test_leaf_diagnosis_page_methods(self):
        """叶节点 diagnosis.page_methods 正确解析"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.tree is not None
        leaf = result.tree.children[0]
        assert leaf.diagnosis is not None
        assert "查看 vCenter 中 CPU 剩余可配置量" in leaf.diagnosis.page_methods
        assert "登录管理台 → 资源池 → 剩余 CPU 核数" in leaf.diagnosis.page_methods

    def test_leaf_solution_quick_recovery(self):
        """叶节点 solution.quick_recovery 正确解析"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.tree is not None
        leaf = result.tree.children[0]
        assert leaf.solution is not None
        assert "迁移其他虚拟机腾出资源" in leaf.solution.quick_recovery

    def test_leaf_solution_thorough_fix(self):
        """叶节点 solution.thorough_fix 正确解析"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.tree is not None
        leaf = result.tree.children[0]
        assert leaf.solution is not None
        assert "向集群扩容 CPU 资源" in leaf.solution.thorough_fix

    def test_no_warnings_with_standard_headings(self):
        """标准话术：无 warning"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.warnings == []

    def test_source_heading_standard(self):
        """标准话术 source_heading 等于常量值"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.tree is not None
        leaf = result.tree.children[0]
        assert leaf.diagnosis is not None
        assert leaf.diagnosis.source_heading == STANDARD_DIAGNOSIS_HEADING
        assert leaf.solution is not None
        assert leaf.solution.source_heading == STANDARD_SOLUTION_HEADING


# ──────────────────────────────────────────────────────────────────────────────
# 深层 SOP 测试
# ──────────────────────────────────────────────────────────────────────────────


class TestParseDeepSop:
    """测试 H1→H2→H3→H4 SOP 结构"""

    def test_is_valid(self):
        """深层 SOP → is_valid=True"""
        result = parse_sop_markdown(_DEEP_SOP)
        assert result.is_valid is True
        assert result.tree is not None

    def test_tree_depth(self):
        """树深度正确（H3 为叶节点）"""
        result = parse_sop_markdown(_DEEP_SOP)
        tree = result.tree
        assert tree is not None
        h2 = tree.children[0]
        h3 = h2.children[0]
        assert h3.is_leaf

    def test_single_leaf(self):
        """collect_leaves 返回唯一叶节点"""
        result = parse_sop_markdown(_DEEP_SOP)
        assert result.tree is not None
        leaves = result.tree.collect_leaves()
        assert len(leaves) == 1

    def test_acli_methods_parsed(self):
        """acli_methods 字段正确解析"""
        result = parse_sop_markdown(_DEEP_SOP)
        assert result.tree is not None
        leaf = result.tree.collect_leaves()[0]
        assert leaf.diagnosis is not None
        assert len(leaf.diagnosis.acli_methods) > 0
        assert "showmount -e <nfs-server>" in leaf.diagnosis.acli_methods

    def test_page_methods_parsed(self):
        """page_methods 字段正确解析"""
        result = parse_sop_markdown(_DEEP_SOP)
        assert result.tree is not None
        leaf = result.tree.collect_leaves()[0]
        assert leaf.diagnosis is not None
        assert "查看挂载状态页面" in leaf.diagnosis.page_methods


# ──────────────────────────────────────────────────────────────────────────────
# 非标准话术测试
# ──────────────────────────────────────────────────────────────────────────────


class TestNonstandardHeading:
    """测试非标准话术（等效关键词）"""

    def test_is_valid_with_warnings(self):
        """非标准话术 → is_valid=True，有 warning"""
        result = parse_sop_markdown(_NONSTANDARD_SOP)
        assert result.is_valid is True
        assert len(result.warnings) > 0

    def test_warning_message_mentions_nonstandard_wording(self):
        """warning 消息包含「话术不规范」"""
        result = parse_sop_markdown(_NONSTANDARD_SOP)
        msgs = [w.message for w in result.warnings]
        assert any("话术不规范" in m for m in msgs)

    def test_solution_source_heading_recorded(self):
        """solution.source_heading 记录原始文本「处理方法」"""
        result = parse_sop_markdown(_NONSTANDARD_SOP)
        assert result.tree is not None
        leaf = result.tree.collect_leaves()[0]
        assert leaf.solution is not None
        assert leaf.solution.source_heading == "处理方法"

    def test_diagnosis_source_heading_recorded(self):
        """diagnosis.source_heading 记录原始文本「排查方法」"""
        result = parse_sop_markdown(_NONSTANDARD_SOP)
        assert result.tree is not None
        leaf = result.tree.collect_leaves()[0]
        assert leaf.diagnosis is not None
        assert leaf.diagnosis.source_heading == "排查方法"

    def test_tree_not_none_when_only_warnings(self):
        """仅有 warning 时 tree 仍非 None"""
        result = parse_sop_markdown(_NONSTANDARD_SOP)
        assert result.tree is not None


# ──────────────────────────────────────────────────────────────────────────────
# 叶节点缺失测试
# ──────────────────────────────────────────────────────────────────────────────


class TestMissingLeafContent:
    """测试叶节点缺少 diagnosis 或 solution"""

    def test_missing_solution_is_invalid(self):
        """缺少 solution → is_valid=False"""
        result = parse_sop_markdown(_MISSING_SOLUTION_SOP)
        assert result.is_valid is False
        assert result.tree is None

    def test_missing_solution_error_message(self):
        """error 消息包含「解决方案」"""
        result = parse_sop_markdown(_MISSING_SOLUTION_SOP)
        msgs = [e.message for e in result.errors]
        assert any("解决方案" in m for m in msgs)

    def test_missing_diagnosis_is_invalid(self):
        """缺少 diagnosis → is_valid=False"""
        result = parse_sop_markdown(_MISSING_DIAGNOSIS_SOP)
        assert result.is_valid is False
        assert result.tree is None

    def test_missing_diagnosis_error_message(self):
        """error 消息包含「判断方法」"""
        result = parse_sop_markdown(_MISSING_DIAGNOSIS_SOP)
        msgs = [e.message for e in result.errors]
        assert any("判断方法" in m for m in msgs)


# ──────────────────────────────────────────────────────────────────────────────
# node_id 分配测试
# ──────────────────────────────────────────────────────────────────────────────


class TestNodeIdAssignment:
    """测试 node_id 分配"""

    def _collect_node_ids(self, node, ids=None):
        """递归收集所有节点 ID"""
        if ids is None:
            ids = []
        ids.append(node.node_id)
        for child in node.children:
            self._collect_node_ids(child, ids)
        return ids

    def test_root_id_is_n1(self):
        """根节点 node_id == 'n-1'"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.tree is not None
        assert result.tree.node_id == "n-1"

    def test_first_child_id(self):
        """第一个子节点 node_id == 'n-1-1'"""
        result = parse_sop_markdown(_SIMPLE_SOP)
        assert result.tree is not None
        assert result.tree.children[0].node_id == "n-1-1"

    def test_all_nodes_have_non_empty_id(self):
        """所有节点均分配了非空 node_id"""
        result = parse_sop_markdown(_DEEP_SOP)
        assert result.tree is not None
        ids = self._collect_node_ids(result.tree)
        for nid in ids:
            assert nid, "存在未分配 node_id 的节点"
            assert nid.startswith("n-")

    def test_no_duplicate_ids(self):
        """node_id 无重复"""
        result = parse_sop_markdown(_DEEP_SOP)
        assert result.tree is not None
        ids = self._collect_node_ids(result.tree)
        assert len(ids) == len(set(ids)), "存在重复的 node_id"
