---
status: active
category: solution
audience: developer
last_updated: 2026-04-13
changelog:
  - 2026-04-13: 初版（PaddleOCR + LLM 双引擎设计）
  - 2026-04-13: 修订 v2——PaddleOCR 在 WSL2 无 GPU 环境 import 阶段挂死，临时改为 Vision LLM 单路径
  - 2026-04-13: Bugfix——修复日志截图识别：改进 OCR Prompt 照录黑色背景文字；增加 full_text 为空时黑色背景兜底分类
  - 2026-04-13: 修订 v3（当前）——恢复多后端架构：EasyOCR（dev/WSL2）或 PaddleOCR（staging/prod 原生Linux）为主路径，Vision LLM 为兜底；加入指数退避重试；环境通过 kubectl hci.env.role 自动检测
owner: team
---

# KBD 截图识别与说明展示优化方案

## 背景与需求

参见：[docs/requirement/events/2026-04-13-kbd-screenshot-ocr-redesign.md](../requirement/events/2026-04-13-kbd-screenshot-ocr-redesign.md)

## 方案概述

采用 **本地 OCR 主路径 + Vision LLM 兜底 + 分析 LLM** 三层架构：

```
图片
  ↓ [1] 本地 OCR（按环境自动选后端）
       dev         → EasyOCR（WSL2 兼容，无 OneDNN 依赖）
       staging/prod → PaddleOCR（原生 Linux，精度更高）
       失败/空      → 降级到 Vision LLM 兜底
  ↓ [2] Vision LLM 兜底（qwen3.5-plus）
       仅当本地 OCR 失败或返回空时启用
       带指数退避重试（最多 3 次，2s/4s/8s 间隔）
  ↓ [3] Pillow（背景色采样，独立于所有 LLM）
  ↓ [4] qwen3-max 分析 LLM（类型判断 + KEY 提取 + TIPS 生成，带重试）
  → desc.txt（v2 格式 BACKGROUND/TYPE/FULL_TEXT/KEY/TIPS）
  ↓ [converter.py]
  → content_md（每行加 "> " 前缀）
  ↓ [KbdReviewView.vue]
  → 按 TYPE 差异化展示三个字段
```

**环境自动检测**：启动时执行 `kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'`，
返回值决定 OCR 后端；kubectl 失败时降级为 `vision_only`（只用 Vision LLM）。
可通过环境变量 `KBD_ENV` 或 `OCR_BACKEND` 手动覆盖。

## 详细设计

### 1. data-pipeline/kbd/ocr.py（双后端封装）

- **PaddleOCR 后端**：`extract_text(image_path: Path) -> list[str]`
  - 单例懒加载，避免进程启动时等待
  - 适用环境：staging/prod（原生 Linux，无 OneDNN 限制）
- **EasyOCR 后端**：`extract_text_easyocr(image_path: Path) -> list[str]`
  - 单例懒加载，`gpu=False` 强制 CPU 模式，WSL2 完全兼容
  - 适用环境：dev（WSL2，PaddleOCR import 阶段挂死）
  - 置信度阈值 0.3（低于 PaddleOCR 的 0.5，因 EasyOCR 对终端等宽字体得分偏低）
- 共用 `_merge_same_row()` 按坐标合并同行文字块

### 2. 新增 data-pipeline/kbd/analyzer.py（LLM 分析层）

- 接口：`analyze_screenshot(background: str, full_text: list[str]) -> AnalysisResult`
- 调用 DashScope qwen3-max-2026-01-23（文本模式，无图片）
- Prompt 驱动：提供背景色 + 完整文字，要求输出 TYPE/KEY/TIPS
- 解析 LLM 输出为 `AnalysisResult(type, key, tips)`

### 3. 重构 data-pipeline/kbd/image_proc.py

**完整流程**：
```python
async def _process_image(img_path) -> str:
    # Step 1: 本地 OCR 主路径（按环境选后端）
    backend = settings.ocr_backend_effective  # easyocr / paddleocr / vision_only
    if backend in ("paddleocr", "easyocr"):
        extractor = extract_text if backend == "paddleocr" else extract_text_easyocr
        full_text = await asyncio.to_thread(extractor, img_path)
    
    # Step 2: 本地 OCR 失败/空 → Vision LLM 兜底（带指数退避重试）
    if not full_text:
        full_text = await _vision_ocr_fallback(...)  # 最多重试3次
    
    # Step 3: 背景色检测（Pillow，独立于 LLM）
    background = await asyncio.to_thread(_detect_background, img_path)
    
    # Step 4: 分析 LLM（qwen3-max，文本模式，带重试）
    result = await analyze_screenshot(background, full_text, ...)
    
    # Step 5: 组装 desc.txt v2
    return _format_desc_v2(background, result, full_text)
```

**重试机制**（两处 LLM 调用均适用）：
- 单次超时：`LLM_TIMEOUT=90s`
- 最大重试次数：`LLM_MAX_RETRIES=3`（可配置）
- 退避间隔：`LLM_RETRY_BACKOFF=2.0`，实际等待 2s → 4s → 8s
- 全部失败才返回空结果，触发 analyzer 兜底

**Vision LLM OCR Prompt 关键要求**（针对日志截图）：
- 英文日志、命令行输出、错误堆栈等原文照录，每行单独输出
- 黑色背景的终端/日志截图同样需要照录所有文字，不要跳过
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
- `FULL_TEXT`：本地 OCR 或 Vision LLM 完整输出，不截断，全量存储
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

### 方案演进历史

| 版本 | OCR 路径 | 放弃原因 |
|---|---|---|
| v1 初版 | PaddleOCR 主路径 + Vision LLM 兜底 | WSL2 开发环境 `import paddle` 阶段挂死（OneDNN 内核兼容问题），无法绕过 |
| v2 临时 | Vision LLM 单路径 | API 偶发超时导致整张图片无法识别；无本地 OCR 兜底 |
| **v3 当前** | **本地 OCR 主路径（EasyOCR/PaddleOCR）+ Vision LLM 兜底** | — |

### 最终方案选型

| 方案 | 文字精度 | 智能分析 | WSL2 兼容 | 选中 |
|---|---|---|---|---|
| glm-4v-flash 单次（原状） | 低（字符混淆）| 弱 | ✅ | ❌ |
| PaddleOCR + LLM | 极高 | 强 | ❌ import 挂死 | staging/prod ✅ |
| EasyOCR + LLM | 高 | 强 | ✅ | dev ✅ |
| Vision LLM + 分析 LLM | 中高 | 强 | ✅ | 兜底 ✅ |

**EasyOCR 选为 dev 主路径的原因**：无 OneDNN 依赖，WSL2 完全兼容；纯 PyTorch，CPU 模式可用；对中英混合终端截图精度良好。首次下载模型约 100MB（PaddleOCR 约 300MB）。

**PaddleOCR 保留为 staging/prod 主路径**：原生 Linux 环境无兼容问题；专为 OCR 设计的深度学习模型，对等宽字体终端/日志截图精度极高。

### DashScope 模型选型

| 用途 | 模型 | 理由 |
|---|---|---|
| Vision 兜底 OCR | qwen3.5-plus | DashScope 上视觉理解最强，替代 glm-4v-flash |
| 文本分析 LLM | qwen3-max-2026-01-23 | DashScope 上最强推理模型，文字分析质量高 |

### 为什么后端决定 TYPE？

前端 `detectScreenshotType` 依赖 visibleContent/errorContent 的关键词，而这些内容质量本身就差，形成循环依赖。改为后端 LLM 判断后，前端只读，消除依赖链。

## 影响范围

- `data-pipeline/kbd/ocr.py` - 新增（PaddleOCR 封装 + EasyOCR 封装，共用坐标合并逻辑）
- `data-pipeline/kbd/analyzer.py` - 新增（分析 LLM，带指数退避重试）
- `data-pipeline/kbd/image_proc.py` - 重构（多后端路由 + Vision LLM 兜底重试，不影响 CLI 接口）
- `data-pipeline/kbd/converter.py` - 新格式输出（向前兼容旧 desc.txt）
- `data-pipeline/kbd/config.py` - 新增 ZAI_API_KEY/ANALYSIS_MODEL/KBD_ENV/OCR_BACKEND/LLM_MAX_RETRIES/LLM_RETRY_BACKOFF，新增 `ocr_backend_effective` 属性
- `frontend/admin/src/views/KbdReviewView.vue` - 新增 v2 解析器，保留 v1
- `pyproject.toml` - 新增 `[project.optional-dependencies] ocr` 组

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| EasyOCR 首次运行需下载模型（约 100MB） | 首次调用等待约 10-30s | 单例懒加载，后续使用缓存；文档说明 |
| EasyOCR 对复杂中文界面精度低于 PaddleOCR | 部分截图 KEY 内容不完整 | 失败/空时降级到 Vision LLM；dev 环境接受精度略低 |
| Vision LLM API 偶发超时（90s/次） | 最坏情况 90×4+14s ≈ 6 分钟/张 | 已加指数退避重试（×3）；本地 OCR 成功则完全绕过 Vision LLM |
| Vision LLM 对黑色背景截图识别为空 | full_text 为空，TYPE 退化为其他截图 | OCR Prompt 明确要求照录；BACKGROUND=黑色时兜底 TYPE=日志截图 |
| 现有 DB content_md 格式不兼容 | 历史条目展示异常 | 前端保留 v1 解析器；重跑 import 更新 DB |
| LLM 分析质量取决于提示词 | KEY/TIPS 内容偶尔偏差 | 提示词版本化（_ANALYSIS_PROMPT_VERSION），持续迭代 |
| kubectl 在某些机器不可用 | 环境检测失败 | 自动降级为 vision_only；可用 KBD_ENV 手动覆盖 |

## 验收标准

- [x] `uv run PYTHONPATH=data-pipeline python -m kbd.run vision --ids 34977` 成功生成 v2 格式 desc.txt
- [x] img_0 FULL_TEXT 行数 >= 5，TYPE = 任务截图，KEY 含失败任务
- [x] img_1 FULL_TEXT 行数 >= 5，TYPE = 日志截图（实测 14 行 QEMU 日志，Vision LLM 主路径）
- [ ] img_2 TYPE = 日志截图（持续超时后返回「无文字」，BACKGROUND=「其他」未命中兜底，遗留）
- [x] content_md 更新后前端展示三列，字段2标签正确（已部署 acec87f）
- [x] 前端 v2 解析修复：parseContentMd isFieldLine/isBulletLine 新增 v2 边界识别
- [ ] EasyOCR 主路径效果验证（安装中，待测）
- [ ] PaddleOCR 在原生 Linux 环境效果验证（待 staging 部署后验证）

## 运行时配置说明

```bash
# OCR 后端手动指定（覆盖环境自动检测）
OCR_BACKEND=easyocr    # 强制 EasyOCR
OCR_BACKEND=paddleocr  # 强制 PaddleOCR
OCR_BACKEND=vision_only # 强制纯 Vision LLM

# 环境手动指定（覆盖 kubectl 检测）
KBD_ENV=dev      # → easyocr
KBD_ENV=staging  # → paddleocr
KBD_ENV=prod     # → paddleocr

# 重试相关（可在 .env 中覆盖）
LLM_TIMEOUT=90          # 单次 LLM 请求超时（秒）
LLM_MAX_RETRIES=3       # 最大重试次数
LLM_RETRY_BACKOFF=2.0   # 指数退避基数（秒）
```

## 已知遗留问题

| 问题 | 原因 | 状态 |
|---|---|---|
| img_2 TYPE=其他截图（无日志内容） | Vision LLM 对该图持续超时（重试3次仍返回「无文字」）；BACKGROUND=「其他」未命中黑色兜底 | 遗留，低优先级 |
| img_2 背景色检测为「其他」 | Pillow 采样区域可能处于界面分隔处，颜色混合 | 待查 |
| EasyOCR 效果未验证 | 安装中 | 待测 |
