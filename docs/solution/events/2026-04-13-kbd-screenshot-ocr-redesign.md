---
status: active
category: solution
audience: developer
last_updated: 2026-04-13
changelog:
  - 2026-04-13: 初版（PaddleOCR + LLM 双引擎设计）
  - 2026-04-13: 修订——架构调整为 Vision LLM（qwen3.5-plus）主路径，发现 PaddleOCR 在 wsl2/无 GPU 环境初始化极慢，改用 Vision 完全替代
  - 2026-04-13: Bugfix——修复日志截图识别：改进 OCR Prompt 照录黑色背景文字；增加 full_text 为空时黑色背景兜底分类
owner: team
---

# KBD 截图识别与说明展示优化方案

## 背景与需求

参见：[docs/requirement/events/2026-04-13-kbd-screenshot-ocr-redesign.md](../requirement/events/2026-04-13-kbd-screenshot-ocr-redesign.md)

## 方案概述

采用 **Vision LLM + 分析 LLM 双引擎**替代现有单次 Vision 调用：

```
图片
  ↓ [1] qwen3.5-plus Vision（OCR 照录全文，黑色背景/英文日志须明确照录）
  ↓ [2] Pillow（背景色采样，独立于 LLM）
  ↓ [3] qwen3-max 分析 LLM（类型判断 + KEY 提取 + TIPS 生成）
  → desc.txt（新格式 BACKGROUND/TYPE/FULL_TEXT/KEY/TIPS）
  ↓ [converter.py]
  → content_md（每行加 "> " 前缀）
  ↓ [KbdReviewView.vue]
  → 按 TYPE 差异化展示三个字段
```

> **架构演进说明**：初版设计使用 PaddleOCR 为主路径（Vision LLM 为兜底），实际测试中发现 PaddleOCR 在 WSL2/无 GPU 环境初始化极慢（约 30s+），维护成本高。最终改为 Vision LLM（qwen3.5-plus）作为唯一 OCR 路径，ocr.py 模块保留但不再调用。

## 详细设计

### 1. 新增 scripts/kbd/ocr.py（PaddleOCR 封装，已保留但不调用）

- 接口：`extract_text(image_path: Path) -> list[str]`
- **当前状态**：模块存在但 image_proc.py 不再调用，Vision LLM 已完全替代
- 保留原因：作为备用方案，若未来需要高精度 GPU OCR 可重新启用

### 2. 新增 scripts/kbd/analyzer.py（LLM 分析层）

- 接口：`analyze_screenshot(background: str, full_text: list[str]) -> AnalysisResult`
- 调用 DashScope qwen3-max-2026-01-23（文本模式，无图片）
- Prompt 驱动：提供背景色 + 完整文字，要求输出 TYPE/KEY/TIPS
- 解析 LLM 输出为 `AnalysisResult(type, key, tips)`

### 3. 重构 scripts/kbd/image_proc.py

**实际流程**（Vision LLM 为主路径）：
```python
async def _process_image(img_path) -> str:
    # Step 1: Vision LLM OCR（qwen3.5-plus，照录全文）
    full_text = await _vision_ocr(img_path)   # 包含黑色背景/英文日志特殊指令
    # Step 2: 背景色检测（Pillow，独立于 LLM）
    background = detect_background(img_path)
    # Step 3: LLM 分析（qwen3-max，文本模式）
    result = await analyze_screenshot(background, full_text)
    # Step 4: 组装 desc.txt
    return _format_desc(background, result.type, full_text, result.key, result.tips)
```

**OCR Prompt 关键要求**（针对日志截图识别）：
- 英文日志、命令行输出、错误堆栈等原文照录，每行单独输出
- 黑色背景的终端/日志截图同样需要照录所有文字
- 时间戳、进程 ID、路径等技术内容原样保留，不省略
- 只有截图中完全没有任何文字时，才输出：（无文字）

**分析器兜底逻辑**（analyzer.py）：
- 当 `full_text` 为空（OCR 超时/失败）且 `background == "黑色"` 时，自动分类为 `TYPE=日志截图`
- 其他情况下返回默认 `TYPE=其他截图`

### 4. 新 desc.txt 格式（v2）

```
BACKGROUND: 黑色
TYPE: 日志截图
FULL_TEXT:
- 第1行
- 第2行
KEY:
- ERROR行1
TIPS:
- 建议1
```

字段说明：
- `BACKGROUND`：Pillow 采样决定，非 LLM 推断
- `TYPE`：LLM 决定，权威值，前端只读
- `FULL_TEXT`：PaddleOCR 完整输出，不截断，全量存储
- `KEY`：LLM 基于 TYPE 提取的类型相关关键内容
- `TIPS`：2-3条排障建议

### 5. 更新 converter.py（新块格式）

旧格式（行内拼接）：
```
> **【截图说明】**：0. **截图背景颜色**：黑色\n1. **截图界面类型**：...
```

新格式（每行独立 `>` 行）：
```markdown
> **【截图说明】**
> BACKGROUND: 黑色
> TYPE: 日志截图
> FULL_TEXT:
> - 第1行
> KEY:
> - ERROR行1
> TIPS:
> - 建议1
```

### 6. 更新 frontend parseContentMd

新增 `isFieldLine` 兼容 v2 section headers：
```javascript
// 同时兼容 v1（数字字段）和 v2（关键词 section）
const isFieldLine = /^\d+[.、]\s+\*\*/.test(trimmed)
  || /^(BACKGROUND|TYPE|FULL_TEXT|KEY|TIPS):/.test(trimmed)
```

### 7. 更新 frontend parseScreenshotBlock

双格式检测：
- v2（有 `> BACKGROUND:` 或 `> FULL_TEXT:`）→ 新解析器
- v1（有 `0. **截图背景颜色**`）→ 保留旧解析器

v2 解析器：
- 剥离每行 `> ` 前缀
- FULL_TEXT 按 TYPE 决定展示方向（终端/日志取后N行，告警/任务取前N行）
- KEY 直接展示（字段2，标签跟随 TYPE）
- TIPS 展示（字段3）

## 决策依据

### 方案选择：最终选 Vision LLM + 分析 LLM 双引擎

| 方案 | 文字精度 | 智能分析 | 部署代价 | 选中 |
|---|---|---|---|---|
| glm-4v-flash 单次（现状） | 低（字符混淆）| 弱 | 0 | ❌ |
| PaddleOCR + LLM 双引擎（初版设计） | 极高（专业 OCR） | 强 | 高（WSL2 无 GPU 30s+ 初始化）| ❌ 实测放弃 |
| Vision OCR（qwen3.5-plus）+ 分析 LLM | 中高 | 强（LLM 分析纯文本） | 0（无额外依赖） | ✅ 最终选中 |

**Vision LLM 替代 PaddleOCR 的原因**：实测 WSL2 环境下 PaddleOCR 初始化约 30 秒（CPU 模式），且第一次调用需下载约 300MB 模型，operator 体验极差。qwen3.5-plus Vision 模型在加强 OCR Prompt 后，对终端/日志截图的文字识别精度可接受，且无需额外依赖。

PaddleOCR 的核心优势：专为字符识别设计，对等宽字体终端/日志截图极为擅长；LLM 拿到准确文字后，分析质量也显著提升。

### DashScope 模型选型

| 用途 | 模型 | 理由 |
|---|---|---|
| Vision 兜底 OCR | qwen3.5-plus | DashScope 上视觉理解最强，替代 glm-4v-flash |
| 文本分析 LLM | qwen3-max-2026-01-23 | DashScope 上最强推理模型，文字分析质量高 |

### 为什么后端决定 TYPE？

前端 `detectScreenshotType` 依赖 visibleContent/errorContent 的关键词，而这些内容质量本身就差，形成循环依赖。改为后端 LLM 判断后，前端只读，消除依赖链。

## 影响范围

- `scripts/kbd/ocr.py` - 新增
- `scripts/kbd/analyzer.py` - 新增
- `scripts/kbd/image_proc.py` - 重构（不影响 CLI 接口）
- `scripts/kbd/converter.py` - 新格式输出（向前兼容旧 desc.txt）
- `scripts/kbd/config.py` - 新增 DASHSCOPE_BASE_URL, ANALYSIS_MODEL
- `frontend/admin/src/views/KbdReviewView.vue` - 新增 v2 解析器，保留 v1
- `pyproject.toml` - 新增 `[project.optional-dependencies] ocr` 组

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| Vision LLM 对黑色背景日志截图可能识别为空 | full_text 为空，TYPE 分类退化为其他截图 | OCR Prompt 明确要求照录；BACKGROUND=黑色 时兜底 TYPE=日志截图 |
| Vision LLM 偶发超时（约 60s timeout） | 单张图片跳过，TYPE=其他截图 | 兜底逻辑（背景色）保证 TYPE 仍有意义；超时的图片可单独重跑 |
| 现有 DB content_md 格式不兼容 | 历史条目展示异常 | 前端保留 v1 解析器；重跑 import 更新 DB |
| LLM 分析质量取决于提示词 | KEY/TIPS 内容偶尔偏差 | 提示词版本化，持续迭代 |

## 验收标准

- [x] `uv run python -m scripts.kbd.run vision --ids 34977` 成功生成 v2 格式 desc.txt
- [x] img_0 FULL_TEXT 行数 >= 5，TYPE = 任务截图，KEY 含失败任务
- [x] img_1 FULL_TEXT 行数 >= 5，TYPE = 日志截图（实测 14 行 QEMU 日志）
- [ ] img_2 TYPE = 日志截图（持续超时，分类靠 BACKGROUND 兜底：img_2 背景检测为「其他」，未能命中兜底，遗留问题）
- [x] content_md 更新后前端展示三列，字段2标签正确（已部署 acec87f）
- [x] 前端 v2 解析修复：parseContentMd isFieldLine/isBulletLine 新增 v2 边界识别

## 已知遗留问题

| 问题 | 原因 | 状态 |
|---|---|---|
| img_2 TYPE=其他截图（无日志内容） | Vision LLM 对该图持续超时；BACKGROUND=「其他」未命中黑色兜底 | 遗留，低优先级 |
| img_2 背景色检测为「其他」 | Pillow 采样区域可能处于界面分隔处，颜色混合 | 待查 |
