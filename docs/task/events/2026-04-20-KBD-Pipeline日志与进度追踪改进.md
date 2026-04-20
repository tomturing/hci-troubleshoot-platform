---
status: active
category: task
audience: developer
last_updated: 2026-04-20
owner: claude
---

# KBD Pipeline 日志与进度追踪改进任务

## 关联方案
[方案文档](../../solution/events/2026-04-20-KBD-Pipeline日志与进度追踪改进.md)

## 任务清单

### T1: config.py 添加 KBD_LOGS_DIR
- **描述**：在 `scripts/kbd/config.py` 添加 `KBD_LOGS_DIR` 字段，并更新 validator
- **文件变更**：`scripts/kbd/config.py`
- **验收标准**：lint 通过，`settings.KBD_LOGS_DIR` 可用
- **依赖**：无
- **预计耗时**：10min
- **状态**：✅ 已完成

### T2: 创建 progress.py 模块
- **描述**：创建 `scripts/kbd/progress.py`，包含进度追踪核心函数
- **文件变更**：`scripts/kbd/progress.py`（新增）
- **验收标准**：
  - 函数签名符合设计
  - JSON 结构符合设计文档
  - 文件创建/读取/更新功能正常
- **依赖**：T1
- **预计耗时**：60min
- **状态**：待开始

### T3: fetcher.py 添加失败检测函数
- **描述**：添加 `_is_fetch_failed()` 和 `get_failed_fetch_ids()` 函数
- **文件变更**：`scripts/kbd/fetcher.py`
- **验收标准**：函数能正确识别有 `.failed` 标记的案例
- **依赖**：无
- **预计耗时**：20min
- **状态**：待开始

### T4: image_proc.py 添加失败检测函数
- **描述**：添加 `_has_failed_vision()` 和 `get_failed_vision_ids()` 函数
- **文件变更**：`scripts/kbd/image_proc.py`
- **验收标准**：函数能正确识别 `.desc.failed` 和"无文字"内容
- **依赖**：无
- **预计耗时**：20min
- **状态**：待开始

### T5: run.py 日志配置和 CLI 参数
- **描述**：
  - 添加 `_setup_logging(run_id)` 函数（双 Handler）
  - 添加 `--resume`, `--resume-run-id`, `--failed-only` CLI 参数
  - 修改 `main()` 初始化日志
  - 修改 `_cmd_pipeline()` 传递 run_id
- **文件变更**：`scripts/kbd/run.py`
- **验收标准**：
  - 日志文件正确生成
  - CLI 参数解析正常
  - 终端和文件双输出
- **依赖**：T1, T2
- **预计耗时**：40min
- **状态**：待开始

### T6: pipeline.py 集成进度追踪
- **描述**：
  - 修改 `run_pipeline()` 函数签名（添加 resume, failed_only, run_id 参数）
  - 每个 Stage 完成后调用 `update_stage_status()` 和 `save_progress()`
  - 实现 resume 和 failed_only 逻辑
  - 修改 `run_from_excel()` 同样支持新参数
- **文件变更**：`scripts/kbd/pipeline.py`
- **验收标准**：
  - progress.json 正确生成和更新
  - --resume 正确跳过已完成案例
  - --failed-only 正确筛选失败案例
- **依赖**：T2, T3, T4
- **预计耗时**：60min
- **状态**：待开始

### T7: 本地验证
- **描述**：运行测试验证所有功能正常
- **验收标准**：
  - 日志文件 `kbd_*.log` 存在且内容完整
  - 进度文件 `progress_*.json` 结构正确
  - --resume 和 --failed-only 参数工作正常
- **依赖**：T5, T6
- **预计耗时**：30min
- **状态**：待开始

### T8: 文档更新
- **描述**：
  - 更新 `docs/solution/knowledge-base/知识库设计.md`
  - 添加 Pipeline 运行日志章节
- **文件变更**：`docs/solution/knowledge-base/知识库设计.md`
- **验收标准**：文档结构完整，内容准确
- **依赖**：T7
- **预计耗时**：30min
- **状态**：待开始

## 任务依赖图

```
T1 ──→ T2 ──→ T5 ──→ T7 ──→ T8
       │      │
       ↓      ↓
T3 ────────→ T6
T4 ────────→ T6
```

## 执行顺序建议

1. **第一批**：T1（已完成）
2. **第二批**：T2, T3, T4（可并行）
3. **第三批**：T5, T6（依赖 T2, T3, T4）
4. **第四批**：T7
5. **第五批**：T8

## 文档更新计划

按照文档管理规范，以下文档需要在任务完成后同步更新：
- [ ] `docs/solution/knowledge-base/知识库设计.md` — 更新 Pipeline 运行日志章节
- [ ] `docs/solution/events/2026-04-20-KBD-Pipeline日志与进度追踪改进.md` — 更新验收标准

## 测试计划

### 单元测试
- progress.py 函数测试
- get_failed_* 函数测试

### 集成测试
- Pipeline 运行后检查日志和进度文件
- --resume 参数验证
- --failed-only 参数验证

### 人工测试命令
```bash
# 测试日志持久化
uv run python -m scripts.kbd.run pipeline --ids 15414 --stages fetch
ls scripts/kbd/logs/
cat scripts/kbd/logs/kbd_*.log
cat scripts/kbd/logs/progress_*.json

# 测试 resume
uv run python -m scripts.kbd.run pipeline --ids 15414,15415 --stages fetch --resume

# 测试 failed-only
uv run python -m scripts.kbd.run vision --failed-only --excel --limit 5
```

## 变更历史
| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-20 | v1.0 | 初版 | 本文档 |