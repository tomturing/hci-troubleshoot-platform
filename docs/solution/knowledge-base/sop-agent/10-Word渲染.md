# SOP Agent Word渲染分析

> 本文档详细分析 `sop-agent` 项目中 Word 渲染阶段的实现设计。
>
> 源代码位置：`/mnt/d/aihci/sop-agent/scripts/render_sop_docx.py`

---

## 核心特点

- **完整文档结构**：封面、目录、正文、附件，完整的 Word 排障手册格式
- **丰富排版元素**：书签、超链接、表格、色块、徽章等视觉元素
- **图片内嵌**：图片下载缓存并内嵌到文档中，离线可用
- **书签跳转**：面向运维场景，支持书签跳转、风险提示、命令高亮
- **python-docx 驱动**：使用 `python-docx` 库生成 `.docx` 文件

## 核心问题

如何将结构化的 SOP JSON 转换为格式规范、图片完整、可离线使用的 Word 文档？

## 核心思路

使用 `python-docx` 库提供的 API 构建文档。通过遍历 SOP JSON 的各字段（入口、步骤、决策树等），依次调用 `python-docx` API 添加段落、表格、图片等元素，并设置样式。

## 核心目标

1. 将 SOP JSON 转换为格式规范的 Word 排障手册
2. 所有图片内嵌到文档中，确保离线可用
3. 完整的文档结构：封面、目录、正文、附件
4. 支持书签跳转和风险标识

## 核心约束

1. **图片内嵌**：所有图片必须内嵌到 docx 中，不使用外部引用
2. **文档结构对齐**：Word 文档结构应与 HTML 渲染保持一致
3. **超链接有效**：文档内超链接必须指向有效书签
4. **风险标识可见**：高危操作标识必须以视觉色块呈现

## LLM 使用情况

**使用**：否

此阶段为纯文档对象模型（DOM）操作，依赖 `python-docx` 库，不调用任何 LLM API。

---

## 一、概述

Word 渲染阶段将结构化的 SOP JSON 数据转换为**可离线阅读的 Word 排障手册**，面向一线运维人员提供正式归档和线下查阅的文档格式。

---

## 二、输入输出

| 项目 | 内容 |
|------|------|
| **输入** | `node_sops.jsonl`（多节点）或 单节点 `.json` 文件 |
| **输出** | `.docx` 文件（单节点）或 目录中的多个 `.docx`（多节点） |
| **可选输入** | `case_facts.jsonl`（用于渲染分支和步骤级别的截图） |

**命令**：

```bash
# 多节点 JSONL → 目录中的多份 Word
python scripts/render_sop_docx.py \
  -i output/node_sops.jsonl \
  -o output/docx_sops/

# 单节点 JSON → 单个 Word
python scripts/render_sop_docx.py \
  -i output/node_sop.client_access.json \
  -o output/node_sop.client_access.docx

# 从 JSONL 中只导出一个节点
python scripts/render_sop_docx.py \
  -i output/node_sops.jsonl \
  -o output/client_access.docx \
  --node-path "云计算-排障-客户端接入使用体验"

# 可选参数
--title-prefix "排障手册"         # 文档标题前缀
--case-facts path                # case_facts.jsonl 路径
--max-images-per-branch 3       # 每分支最多渲染的图片数
--image-cache-dir output/.image_cache  # 图片缓存目录
--image-download-delay 0.45     # 图片下载最小间隔（秒）
--image-download-retries 4      # 图片下载最大重试次数
```

---

## 三、文档结构

### 3.1 完整章节

```
封面页
  ├── 标题（节点名 + 排障手册）
  ├── 副标题（分类路径）
  ├── 说明文字（面向一线运维人员）
  ├── 元信息表格
  │    ├── 节点名称
  │    ├── 适用范围
  │    ├── 覆盖案例数
  │    ├── 分支数
  │    ├── 适用环境
  │    ├── 典型症状
  │    ├── 常见关键字
  │    └── 生成时间
  └── 阅读顺序说明框

1. 如何使用这份手册
  ├── 6 步阅读指引
  └── 高风险提醒框（如有高风险步骤）

2. 场景概览
  ├── 适用节点 / 范围 / 案例数 / 环境
  ├── 典型症状（列表）
  ├── 常见告警或关键字（列表）
  └── 操作前置条件（列表）

3. 快速定位速查
  ├── 使用说明
  ├── 条件-分支对照表（3列：情况 / 建议分支 / 处理方向）
  └── 不匹配提示框

4. 入口判断步骤
  └── 每个步骤：动作 → 命令 → 预期结果 → 分支选择 → 不匹配处理

5. 分支导航
  └── 分支索引表（4列：分支 / 何时进入 / 核心问题 / 备注）

6.1 ~ 6.N 各分支详情
  ├── 本分支解决什么问题（摘要框）
  ├── 风险提醒框（如有）
  ├── 元信息表（类型 / 根因 / 案例数 / 环境 / 场景）
  ├── 何时进入本分支
  ├── 处置要点（临时措施 / 长期修复 / 风险影响 / 回滚方案 / 升级条件）
  ├── 相关截图
  ├── 分支内决策流程（ASCII 图）
  ├── 推荐执行路径
  ├── 检查步骤
  ├── 处理步骤
  ├── 日志或报错示例
  └── 分支结束导航

7. 本手册未覆盖的情况
  └── 排除案例表（案例ID / 原因）
```

### 3.2 封面页

```
┌─────────────────────────────────────┐
│                                     │
│        虚拟机开机失败排障手册         │
│     云计算-排障-虚拟机-开机失败       │
│                                     │
│     面向一线运维人员的排障执行手册     │
│                                     │
│  ┌──────────┬──────────────────┐    │
│  │ 节点名称 │ 虚拟机开机失败     │    │
│  │ 适用范围 │ ...              │    │
│  │ 覆盖案例 │ 50               │    │
│  │ 分支数   │ 5                │    │
│  │ ...      │ ...              │    │
│  └──────────┴──────────────────┘    │
│                                     │
│  ┌──────────────────────────────┐   │
│  │ 阅读顺序：先看快速定位...     │   │
│  └──────────────────────────────┘   │
│                                     │
└─────────────────────────────────────┘
```

---

## 四、排版元素

### 4.1 书签与超链接

Word 文档中使用**书签（Bookmark）+ 超链接（Hyperlink）** 实现文档内导航：

```python
# render_sop_docx.py 第683-691行
def add_bookmark(paragraph, anchor, bookmark_counter):
    bookmark_id = str(next(bookmark_counter))
    start = parse_xml(rf'<w:bookmarkStart ... w:name="{anchor}"/>')
    end = parse_xml(rf'<w:bookmarkEnd ... w:id="{bookmark_id}"/>')
    paragraph._p.insert(0, start)
    paragraph._p.append(end)

# render_sop_docx.py 第693-713行
def add_internal_hyperlink(paragraph, text, anchor, *, bold=False):
    hyperlink = parse_xml(rf'<w:hyperlink ... w:anchor="{anchor}"/>')
    # 蓝色下划线文字，点击跳转到书签
```

**锚点命名**：使用 MD5 哈希确保唯一性：

```python
def make_anchor_name(*parts):
    key = "|".join(normalize_cell(part) for part in parts)
    digest = hashlib.md5(key.encode()).hexdigest()[:16]
    return f"a{digest}"
```

**导航场景**：
- 快速定位表 → 分支章节
- 入口步骤 → 分支章节
- 分支内步骤 → 下一步骤
- 分支底部 → 返回导航

### 4.2 提示框（Notice Box）

使用单列表格 + 背景色实现：

```python
# render_sop_docx.py 第799-813行
def add_notice_box(doc, title, text, *, fill, color):
    table = doc.add_table(rows=1, cols=1)
    cell = table.rows[0].cells[0]
    shade_cell(cell, fill)           # 背景色
    # 标题加粗 + 正文
```

| 类型 | 背景色 | 文字色 | 用途 |
|------|--------|--------|------|
| 阅读顺序 | `FFF2CC` 黄色 | `7F6000` | 阅读指引 |
| 高风险提醒 | `FDE9D9` 橙色 | `C00000` 红色 | 高风险操作警告 |
| 点击提示 | `EAF2F8` 蓝色 | `445C6E` | 超链接导航提示 |
| 不匹配提示 | `EDEDED` 灰色 | `666666` | 退出说明 |
| 摘要框 | `D9EAF7` 蓝色 | `1F1F1F` | 分支摘要 |

### 4.3 命令块（Command Block）

区分三种命令类型，视觉差异明显：

| 类型 | 标题 | 背景色 | 特点 |
|------|------|--------|------|
| 执行命令 | "执行命令" | `EBF5EB` 绿色 | 常规可执行命令 |
| 高风险命令 | "⚠ 高风险命令" | `FDE9D9` 橙色 | 醒目警告 |
| 参考路径 | "参考路径 / 界面操作" | `F3F4F6` 灰色 | 非命令行操作 |

```python
# render_sop_docx.py 第829-847行
def add_command_block(doc, command, *, executable, high_risk=False, example=""):
    if executable:
        title = "⚠ 高风险命令" if high_risk else "执行命令"
    else:
        title = "参考路径 / 界面操作"
    add_code_block(doc, command, title=title, accent_fill=fill)
    if example and example != command:
        add_code_block(doc, example, title="示例", accent_fill="F0F4FF")
```

### 4.4 徽章（Badge）

步骤级别的视觉标记：

| 徽章 | 颜色 | 含义 |
|------|------|------|
| `[⚠ 高风险]` | 红色 `C00000` | 高风险操作 |
| `[命令行]` | 绿色 `2E7D32` | 可执行命令 |
| `[界面操作]` | 蓝色 `5C6BC0` | 界面路径 |

### 4.5 步骤导航表

每个步骤底部显示导航表：

| 检查步骤 | 处理步骤 |
|---------|---------|
| ✓ 命中 → 下一步 | ✓ 成功 → 下一步 |
| ✗ 不命中 → 下一步 | ✗ 失败 → 下一步 |

导航表中的步骤名称是**可点击的超链接**，直接跳转到对应步骤。

### 4.6 执行路径可视化

Word 渲染通过 DFS 遍历步骤图，生成从入口到退出的所有执行路径：

```python
# render_sop_docx.py 第1300-1404行
def build_branch_paths(branch, display_map):
    """通过 DFS 构建分支内的所有执行路径（最多 8 条）。"""
```

渲染效果：

```
• 检查1（命中）→ 处理1（成功）→ [问题已解决]
• 检查1（不命中）→ 检查2（命中）→ 处理2（成功）→ [问题已解决]
• 检查1（不命中）→ 检查2（不命中）→ [当前分支不匹配]
```

步骤名称可点击跳转。

---

## 五、图片处理

### 5.1 图片下载与缓存

Word 渲染需要**将图片下载到本地并内嵌文档**：

```python
# render_sop_docx.py 第78-86行
@dataclass(frozen=True)
class ImageFetchConfig:
    cache_dir: Path
    min_interval_sec: float = 0.45    # 全局下载限速
    max_retries: int = 4              # 最大重试
    request_timeout_sec: float = 45.0 # 单次超时
```

**全局限速**：避免对源站高频请求：

```python
# render_sop_docx.py 第92-101行
def _rate_limit_before_fetch(min_interval_sec):
    """全局限速：两次下载之间至少间隔 min_interval_sec 秒。"""
    with _image_fetch_lock:
        wait = min_interval_sec - (now - _image_fetch_last_mono)
        if wait > 0:
            time.sleep(wait)
```

**缓存策略**：以 URL 的 MD5 哈希为文件名，下载后缓存到本地：

```python
# render_sop_docx.py 第185-196行
def fetch_image_to_cache(url, config):
    base_name = hashlib.md5(url.encode()).hexdigest()
    for existing in cache_dir.glob(f"{base_name}.*"):
        if existing.is_file() and existing.stat().st_size > 0:
            return existing  # 命中缓存
```

### 5.2 图片格式兼容

Word 对图片格式有要求，自动处理 WebP 等不兼容格式：

```python
# render_sop_docx.py 第145-182行
def _normalize_cached_image_for_docx(data, base_cache_path):
    ext = _guess_image_extension(data)   # PNG/JPG/GIF/WebP/BMP
    if not ext:
        # 使用 PIL 转换任意格式为 PNG
        im = PILImage.open(io.BytesIO(data))
        im.save(str(out), "PNG")
    if ext == ".webp":
        # WebP → PNG（Word 不原生支持 WebP）
        im = PILImage.open(io.BytesIO(data))
        im.save(str(out), "PNG")
```

### 5.3 图片插入

```python
# render_sop_docx.py 第1429-1457行
def _render_single_image(doc, image, image_config):
    cached = fetch_image_to_cache(image["url"], image_config)
    if cached is None:
        # 下载失败：显示文字说明和原始链接
        add_labeled_paragraph(doc, "截图", "图片下载失败，请参考原始链接：...")
        return
    doc.add_picture(str(cached), width=Inches(5.6))
    # 添加居中图注
    caption_text = f"图：来源案例 {image.get('case_id', '')}"
```

**图片宽度**：固定 5.6 英寸（A4 页面留白后约 7 英寸可用宽度）。

---

## 六、字体与样式

### 6.1 字体设置

```python
# render_sop_docx.py 第743-760行
def set_document_styles(doc):
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(10.5)
    # 标题字号：Title=22pt, H1=16pt, H2=13pt, H3=11.5pt
```

| 元素 | 字体 | 字号 |
|------|------|------|
| 正文 | Microsoft YaHei | 10.5pt |
| 代码/命令 | Consolas | 10.5pt |
| 标题1 | Microsoft YaHei | 16pt |
| 标题2 | Microsoft YaHei | 13pt |
| 标题3 | Microsoft YaHei | 11.5pt |

### 6.2 页面边距

```python
# render_sop_docx.py 第1901-1905行
section.top_margin = Pt(48)
section.bottom_margin = Pt(42)
section.left_margin = Pt(54)
section.right_margin = Pt(54)
```

### 6.3 文档元信息

```python
# render_sop_docx.py 第1907-1911行
core = doc.core_properties
core.title = f"{node_name}{title_prefix}"
core.subject = "运维排障手册"
core.author = "aiops-sop-agent"
```

---

## 七、与 HTML 渲染的对比

| 特性 | Word 渲染 | HTML 渲染 |
|------|----------|----------|
| 导航机制 | 书签 + 超链接 | 锚点 + JS 滚动 |
| 搜索 | 无 | 实时搜索 + Ctrl+K |
| 引导向导 | 阅读顺序文字指引 | 逐步交互式向导 |
| 图片 | 下载缓存 → 内嵌文档 | 在线加载 → 灯箱放大 |
| 主题 | 固定样式 | 亮/暗切换 |
| 响应式 | 打印优化 | 移动端适配 |
| 决策树 | ASCII 文本 | HTML 可视化 + ASCII |
| 执行路径 | DFS 路径列表 | 步骤导航按钮 |
| 输出格式 | .docx | .html |
| 适用场景 | 离线阅读、正式归档 | 在线浏览、快速定位 |

**共享逻辑**：两者共享数据加载（`load_records`）、图片匹配（`collect_step_images_for_branch`、`_action_overlap_score`）等核心函数。

---

## 八、与其他阶段的关系

```
node_sops.jsonl ───────────→ render_sop_docx.py → docx_sops/ 目录
     │
     └─ case_facts.jsonl ──→ (可选) 提供分支/步骤级图片
```

- Word 渲染是全流程的**最终输出阶段**之一
- 与 HTML 渲染并行，使用相同的输入数据
- 图片缓存目录默认为 `output/.image_cache`，可跨次运行复用

---

*文档创建时间：2026-05-16*
*最后更新：2026-05-16*
