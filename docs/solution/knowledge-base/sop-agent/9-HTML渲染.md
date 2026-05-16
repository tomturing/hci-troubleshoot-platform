# SOP Agent HTML渲染分析

> 本文档详细分析 `sop-agent` 项目中 HTML 渲染阶段的实现设计。
>
> 源代码位置：`/mnt/d/aihci/sop-agent/scripts/render_sop_html.py`

---

## 一、概述

HTML 渲染阶段将结构化的 SOP JSON 数据转换为**可交互的纯静态 HTML 页面**，面向一线运维人员提供排障手册的在线浏览体验。

**核心特点**：
- 纯静态 HTML，无服务端依赖，可直接用文件服务或 CDN 托管
- 丰富的交互功能：搜索、引导向导、图片灯箱、步骤导航
- 支持亮/暗主题切换
- 响应式布局，适配桌面和移动端
- 多节点时自动生成索引页

---

## 二、输入输出

| 项目 | 内容 |
|------|------|
| **输入** | `node_sops.jsonl`（多节点）或 单节点 `.json` 文件 |
| **输出** | HTML 文件目录 + `index.html` 索引页（多节点时） |
| **可选输入** | `case_facts.jsonl`（用于渲染分支和步骤级别的截图） |

**命令**：

```bash
# 多节点 JSONL → 目录中的多份 HTML + 索引页
python scripts/render_sop_html.py \
  -i output/node_sops.jsonl \
  -o output/web_sops/

# 单节点 JSON → 单个 HTML
python scripts/render_sop_html.py \
  -i output/node_sop.vm_create_failed.json \
  -o output/node_sop.vm_create_failed.html

# 可选参数
--title-prefix "排障手册"     # 文档标题前缀
--theme dark|light            # 默认主题
--no-wizard                   # 不生成引导模式
--case-facts path             # case_facts.jsonl 路径（不传则自动查找）
--max-images-per-branch 4     # 每个分支最多渲染的案例级图片数
--max-images-per-step 2       # 每个步骤最多渲染的步骤级图片数
```

---

## 三、页面结构

### 3.1 整体布局

```
┌─────────────────────────────────────────────────────────────┐
│ Navbar（固定顶部）                                           │
│ [☰] 标题 | 面包屑导航              [引导] [展开] [折叠] [打印] [主题] [搜索] │
├──────────┬──────────────────────────────────────────────────┤
│ Sidebar  │ Main Content                                     │
│ 分支导航  │                                                   │
│          │ 1. 概览区                                         │
│ - 分支A   │    节点名称 | 覆盖案例 | 分支数 | 生成时间           │
│ - 分支B   │    适用范围 | 前置条件                             │
│ - 分支C   │    典型症状标签 | 关键字标签                        │
│          │                                                   │
│          │ 2. 智能定位（Tab 切换）                              │
│          │    搜索模式 | 引导模式 | 速查表                       │
│          │                                                   │
│          │ 3. 入口决策流程（可折叠）                             │
│          │                                                   │
│          │ 4. 分支详情（可折叠卡片）                             │
│          │    分支A → 检查步骤 → 处理步骤                       │
│          │    分支B → ...                                     │
│          │                                                   │
│          │ 5. 未覆盖案例                                      │
└──────────┴──────────────────────────────────────────────────┘
```

### 3.2 页面组成元素

| 区域 | 内容 | 交互 |
|------|------|------|
| **Navbar** | 标题、面包屑、功能按钮 | 固定顶部，响应式 |
| **Sidebar** | 分支列表 | 点击跳转，高亮当前分支 |
| **概览区** | 元信息网格 + 标签云 | 标签可点击触发搜索 |
| **智能定位** | 搜索/引导/速查三模式 | Tab 切换，实时搜索 |
| **决策流程** | ASCII 决策树 | 可折叠 |
| **分支详情** | 卡片式分支展示 | 折叠/展开，步骤导航 |
| **引导向导** | 逐步引导排障 | 全屏浮层，前进/后退 |

---

## 四、核心交互功能

### 4.1 智能定位（三模式）

```
┌──────────────────────────────────────────┐
│ 搜索模式 | 引导模式 | 速查表                │
├──────────────────────────────────────────┤
│                                          │
│  模式1 - 搜索：输入关键字，自动匹配推荐分支  │
│  模式2 - 引导：按入口步骤逐步判断           │
│  模式3 - 速查：条件→分支的快速对照表        │
│                                          │
└──────────────────────────────────────────┘
```

**搜索算法**（加权匹配）：

```javascript
// 信号匹配（最高权重）
item.signals.forEach(function(sig) {
    if (sigLower.indexOf(q) >= 0) score += 10;    // 完全包含
    else if (q.indexOf(sigLower) >= 0) score += 8; // 反向包含
});
// 文本匹配
if (item.text.indexOf(q) >= 0) score += 5;
// 模糊匹配
fuzzy = q.split('').filter(c => item.text.indexOf(c) >= 0).length;
score += fuzzy / q.length * 2;
```

**全局搜索**：`Ctrl+K` 快捷键打开搜索弹窗。

### 4.2 排障引导向导（Wizard）

向导模式是 HTML 渲染的亮点功能，提供**逐步引导排障**的交互体验：

```
┌─────────────────────────────────────┐
│         排障引导向导                   │
│                                     │
│  入口判断 (1/3)                      │
│  查看虚拟机状态...                    │
│                                     │
│  ① 如果显示HA → 跳转到分支A           │
│  ② 如果显示正常 → 跳转到分支B         │
│  ③ 都不匹配 → 继续                   │
│                                     │
│  排障轨迹：                          │
│  1. 入口步骤1                       │
│                                     │
│  [← 回退]                    [关闭]  │
└─────────────────────────────────────┘
```

**向导流程**：

```
入口步骤(flow) → 选择条件 → 进入分支
     │                         │
     ▼                         ▼
  检查步骤(check) → 命中/不命中 → 下一步
     │                         │
     ▼                         ▼
  处理步骤(solution) → 成功/失败 → [EXIT] 或继续
```

**向导数据**：将 SOP JSON 嵌入 HTML 中的 `<script>` 标签，供 JavaScript 动态渲染：

```python
# render_sop_html.py 第2158行
sop_data_json = json.dumps(wizard_record, ensure_ascii=False)
```

### 4.3 图片灯箱（Lightbox）

点击截图可放大查看，带过渡动画：

- 点击图片 → 全屏浮层展示
- 点击浮层或按 ESC → 关闭
- 显示图片来源和说明

### 4.4 步骤导航

每个步骤底部提供导航按钮：

| 步骤类型 | 命中/成功按钮 | 不命中/失败按钮 |
|---------|-------------|---------------|
| 检查步骤 | ✓ 命中 → 下一步 | ✗ 不命中 → 下一步 |
| 处理步骤 | ✓ 成功 → 下一步 | ✗ 失败 → 下一步 |

按钮点击后自动滚动到目标步骤并高亮。

### 4.5 退出卡片

步骤导航的终点显示退出原因卡片：

| 退出原因 | 样式 | 含义 |
|---------|------|------|
| resolved | 绿色 | 问题已解决 |
| not_match | 灰色 | 当前分支不匹配 |
| fallback_parent | 蓝色 | 回退到上级 SOP |
| manual_review | 黄色 | 需要人工复核 |
| escalate | 红色 | 需要升级支持 |

---

## 五、可视化决策树

HTML 渲染提供**两种决策树可视化**：

### 5.1 视觉决策树（HTML）

由 `render_visual_decision_tree()` 函数从步骤数据动态生成：

```
├─ check-1: 查看虚拟机状态
│  ├─ ✓ 命中
│  │  └─ solution-1: 重启虚拟机
│  │     ├─ ✓ 成功 → [EXIT] 问题已解决
│  │     └─ ✗ 失败 → [EXIT] 需要升级支持
│  └─ ✗ 不命中
│     └─ check-2: 检查网络连通性
│        ...
```

特点：节点可点击跳转，颜色区分检查/处理/退出类型。

### 5.2 文本决策树（ASCII）

直接使用 SOP JSON 中的 `decision_tree_visual` 字段，由事实提取阶段生成。

---

## 六、图片渲染策略

### 6.1 图片来源

从 `case_facts.jsonl` 中加载图片信息，支持三级粒度：

| 粒度 | 来源字段 | 用途 |
|------|---------|------|
| 全局 | `images` / `image_urls` | 兜底图片列表 |
| 案例级 | `case_images` | 分支头部的场景截图 |
| 步骤级 | `checks[].images` / `solution_steps[].images` | 步骤内的操作截图 |

### 6.2 图片匹配

步骤级图片使用**动作文本相似度匹配**：

```python
# render_sop_html.py 第315-338行
def _action_overlap_score(text_a, text_b):
    """计算两个动作文本的词语重叠度（去除停用词）。"""
    words_a = set(text_a.lower().split()) - stop_words
    words_b = set(text_b.lower().split()) - stop_words
    intersection = len(words_a & words_b)
    return intersection * 100 // max(len(words_a), 1)
```

每张步骤图片匹配到相似度最高的 SOP 步骤。

### 6.3 图片数量限制

```bash
--max-images-per-branch 4    # 每分支最多 4 张案例级图片
--max-images-per-step 2      # 每步骤最多 2 张步骤级图片
```

---

## 七、CSS 与主题

### 7.1 CSS 变量体系

使用 CSS 变量实现主题切换：

```css
:root {
  --bg: #f8f9fa; --bg-card: #fff; --text: #212529;
  --primary: #3b82f6; --success: #22c55e; --danger: #ef4444;
  --check-color: #3b82f6; --solution-color: #22c55e;
  --sidebar-width: 280px; --navbar-height: 56px;
}
[data-theme="dark"] {
  --bg: #1a1b26; --bg-card: #24283b; --text: #c0caf5;
}
```

**语义颜色**：
- 检查步骤（check）：蓝色 `--check-color`
- 处理步骤（solution）：绿色 `--solution-color`
- 高风险操作：红色 `--danger`
- 退出卡片：按退出原因着色

### 7.2 响应式设计

```css
@media (max-width: 768px) {
  .sidebar { transform: translateX(-100%); }  /* 侧边栏隐藏 */
  .overview-grid { grid-template-columns: repeat(2, 1fr); }
  .breadcrumb { display: none; }
}
```

### 7.3 打印优化

```css
@media print {
  .navbar, .sidebar, .wizard-overlay, .btn { display: none !important; }
  .card-body { display: block !important; }  /* 强制展开所有折叠 */
  body { font-size: 11px; color: #000; }
}
```

---

## 八、多节点处理

### 8.1 文件命名

```python
# render_sop_html.py 第119-130行
def output_name_for_record(record):
    node_name = meta.get("node_name") or split_path(node_path)[-1]
    short_hash = hashlib.md5(node_path.encode()).hexdigest()[:8]
    return f"{safe_filename(node_name)}.{short_hash}.html"
```

命名格式：`{节点名}.{路径哈希前8位}.html`

### 8.2 索引页

多节点时自动生成 `index.html`，包含所有节点的链接列表。

### 8.3 单节点过滤

```bash
--node-path "云计算-排障-客户端接入使用体验"
```

从 JSONL 中只导出指定节点的 HTML。

---

## 九、与 Word 渲染的对比

| 特性 | HTML 渲染 | Word 渲染 |
|------|----------|----------|
| 交互性 | 搜索、向导、灯箱、导航 | 书签跳转、超链接 |
| 搜索 | 实时搜索 + Ctrl+K 全局搜索 | 无 |
| 引导向导 | 逐步引导排障 | 阅读顺序指引 |
| 主题 | 亮/暗切换 | 固定样式 |
| 响应式 | 移动端适配 | 打印优化 |
| 图片 | 在线加载，灯箱放大 | 下载缓存，内嵌文档 |
| 输出格式 | 纯静态 HTML | .docx 文件 |
| 适用场景 | 在线浏览、快速定位 | 线下阅读、正式归档 |

---

## 十、与其他阶段的关系

```
node_sops.jsonl ───────────→ render_sop_html.py → web_sops/ 目录
     │
     └─ case_facts.jsonl ──→ (可选) 提供分支/步骤级图片
```

- HTML 渲染是全流程的**最终输出阶段**之一
- 输入来自 SOP 聚合阶段（`aggregate_node_sop.py`）的产出
- 与 Word 渲染共享相同的数据加载和图片匹配逻辑

---

*文档创建时间：2026-05-16*
*最后更新：2026-05-16*
