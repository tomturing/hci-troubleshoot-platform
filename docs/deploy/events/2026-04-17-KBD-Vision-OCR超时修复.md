# KBD Pipeline Vision OCR 超时修复

## 问题背景

执行 `uv run python -m scripts.kbd.run pipeline --ids 35694` 时，图片识别失败，没有任何报错信息，desc.txt 显示 `（无文字）`。

## 根因分析

通过 systematic debugging 流程定位：

### Phase 1: 证据收集

| 图片 | 大小 | Vision LLM 结果 | 原因 |
|------|------|-----------------|------|
| img_0.png | 45KB | ✅ 成功，9 行文字 | 正常处理 |
| img_1.png | 1MB | ❌ `httpx.ReadTimeout` | 图片太大，60s timeout 不足 |

### Phase 2: 问题定位

核心问题链路：
1. 大图片（>500KB）调用 Vision LLM API 超时
2. 超时异常被 `_vision_ocr_fallback` 捕获，返回空列表 `[]`
3. `desc.txt` 写入 `（无文字）`
4. 幂等机制导致后续跳过处理（desc.txt 已存在）

### Phase 3: 日志缺失问题

原有日志只有 `logger.error("Vision 兜底 OCR 失败 path=%s 原因=%s", ...)`，无法区分：
- 超时 vs 其他错误
- API 响应内容
- 图片大小信息

## 修复方案

### 1. 图片压缩预处理

新增 `_compress_image_if_needed()` 函数：
- 超过 500KB 的图片自动压缩
- 缩放宽度限制 2000px
- 转换为 JPEG（质量 85）
- 压缩率约 77%（1MB → 231KB）

### 2. 详细日志增强

**Vision OCR 层：**
```python
logger.info(
    "Vision OCR 开始 path=%s size=%dKB base64_len=%d model=%s timeout=%ds",
    ...
)
logger.info("Vision OCR 响应内容（前200字）：%s", ...)
logger.error("Vision OCR 超时失败 path=%s size=%dKB timeout=%ds 原因=%s", ...)
```

**LLM 分析层：**
```python
logger.info(
    "LLM 分析开始 background=%s text_lines=%d model=%s timeout=%ds",
    ...
)
logger.info("LLM 分析响应内容（前200字）：%s", ...)
logger.info("LLM 分析完成 type=%s key_count=%d tips_count=%d tokens=%d", ...)
```

## 修复后效果

```
2026-04-17 18:57:31 — Vision OCR 开始 path=img_0.png size=44KB ...
2026-04-17 18:57:36 — 图片过大，开始压缩 path=img_1.png original_size=1019KB
2026-04-17 18:57:37 — 图片压缩完成 path=img_1.png ratio=77.3%
2026-04-17 18:57:41 — Vision OCR 完成 path=img_0.png 行数=7
2026-04-17 18:57:55 — Vision OCR 完成 path=img_1.png 行数=31
Stats: {'done': 2, 'failed': 0, 'skipped': 0}
```

| 图片 | 类型 | 文字行数 | KEY 数 | TIPS 数 |
|------|------|---------|--------|---------|
| img_0.png | 任务截图 | 7 | 3 | 3 |
| img_1.png | 日志截图 | 31 | 6 | 3 |

## 影响文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `scripts/kbd/image_proc.py` | 功能增强 | 图片压缩预处理 + 详细日志 |
| `scripts/kbd/analyzer.py` | 日志增强 | LLM 分析详细日志 |

## 关联 PIT

无（新发现问题）

---

[env:dev:gs][agent:claude]