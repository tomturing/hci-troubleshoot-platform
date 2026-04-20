---
status: active
category: requirement
audience: all
last_updated: 2026-04-20
owner: team
---

# KBD Pipeline 日志与进度追踪改进

## 变更历史
| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-20 | v1.0 | 初版 | 本文档 |

## 背景与问题

### 业务背景
KBD（知识库文档）Pipeline 是一个四阶段数据处理管道，用于从深信服技术支持门户抓取案例、识别图片内容、转换格式并导入数据库。目前已处理 19557 张图片，其中 16032 张识别结果为"无文字"，疑似 API 限流导致。

### 核心痛点
1. **日志不持久化**：所有日志仅输出到终端，运行结束后丢失，无法追溯问题
2. **缺少进度追踪**：无法知道哪些案例在哪一步完成/失败，无法统计整体进度
3. **无法从失败点继续**：每次运行都要从头开始，或需要手动筛选失败的案例
4. **大批量失败无法快速定位**：16032 张图片识别为"无文字"，无法快速定位原因和重试

### 影响范围
- scripts/kbd/ 目录下的所有 Pipeline 脚本
- 知识库数据导入效率
- 运维排障效率

## 需求描述

### 功能概述
为 KBD Pipeline 添加：
1. 日志文件持久化（保存到 `logs/kbd_{run_id}.log`）
2. 进度 JSON 追踪（保存到 `logs/progress_{run_id}.json`）
3. `--resume` 参数（从上次中断处继续）
4. `--failed-only` 参数（仅处理失败案例）

### 用户场景
1. **运维排障**：Pipeline 运行后出现问题，需要查看日志文件定位原因
2. **批量重试**：16032 张图片识别失败后，删除失败的 desc.txt 文件，使用 `--failed-only` 重试
3. **中断恢复**：Pipeline 运行到一半被中断（Ctrl+C 或网络故障），使用 `--resume` 继续
4. **进度监控**：查看 progress.json 了解当前处理进度和失败统计

### 预期收益
- 日志可追溯：每次运行都有完整的日志文件
- 进度可视化：实时了解 Pipeline 执行进度
- 断点续传：减少重复工作，提高效率
- 快速重试：针对失败案例定向处理

## 功能需求

### FR1: 日志文件持久化
- 日志保存路径：`scripts/kbd/logs/kbd_{run_id}.log`
- run_id 格式：`YYYYMMDD_HHMMSS`（如 `20260420_100000`）
- 日志同时输出到终端和文件（双 handlers）
- 文件日志级别为 DEBUG（比终端 INFO 更详细）
- 日志格式：`%(asctime)s [%(levelname)s] %(name)s — %(message)s`
- 所有子模块日志（kbd.run, kbd.pipeline, kbd.fetcher, kbd.image_proc 等）统一写入同一文件

### FR2: 进度 JSON 追踪
- 进度保存路径：`scripts/kbd/logs/progress_{run_id}.json`
- 进度结构：
  ```json
  {
    "run_id": "20260420_100000",
    "started_at": "2026-04-20T10:00:00",
    "finished_at": null,
    "total_ids": 100,
    "stages_run": ["fetch", "vision"],
    "stages": {
      "fetch": {"completed_ids": [], "failed_ids": [], "skipped_ids": []},
      "vision": {"completed_ids": [], "failed_ids": [], "skipped_ids": []}
    },
    "cases": {
      "15414": {"fetch": "done", "vision": "pending", "import": "pending", "classify": "pending"}
    }
  }
  ```
- 每个 Stage 完成后更新进度文件
- 运行结束时设置 `finished_at`

### FR3: --resume 参数
- 功能：从上次中断处继续，自动跳过已完成的案例
- 适用命令：`pipeline`, `fetch`, `vision`, `import`
- 逻辑：
  - 加载最新的 progress.json（或指定 `--resume-run-id`）
  - 根据进度跳过已完成的案例
  - 继续使用原有 run_id（追加日志）

### FR4: --failed-only 参数
- 功能：仅处理失败的案例（有 `.failed` 标记）
- 适用命令：`vision`, `import`
- 逻辑：
  - 扫描 cache 目录查找有 `.failed` 文件的案例
  - 或查找识别为"无文字"的 desc.txt 文件

## 非功能需求

### 性能要求
- 进度 JSON 更新不应显著影响 Pipeline 性能（异步写入或批量更新）
- 日志文件大小可控（单次运行预计不超过 10MB）

### 可维护性
- 新增模块 `progress.py`，职责单一
- 不新增外部依赖，仅使用 Python stdlib
- 代码有清晰的中文注释

### 可观测性
- 日志文件记录所有关键操作和错误
- 进度文件可用于后续分析和统计

## 验收标准

- [ ] 标准1：运行 `pipeline --ids 15414 --stages fetch` 后，`scripts/kbd/logs/kbd_*.log` 文件存在且包含完整日志
- [ ] 标准2：运行后 `progress_*.json` 文件存在，结构符合设计，包含 run_id、started_at、cases 状态
- [ ] 标准3：`--resume` 参数能正确跳过已完成的案例，日志追加到原有文件
- [ ] 标准4：`--failed-only` 参数能筛选出失败的案例并只处理这些案例
- [ ] 标准5：lint 检查通过，无新增外部依赖
- [ ] 标准6：已更新 `docs/solution/knowledge-base/知识库设计.md` 对应章节

## 约束条件

### 技术约束
- Python 3.12，使用 stdlib（json, logging, pathlib, datetime）
- 不新增外部依赖
- 向后兼容，现有 CLI 行为不变

### 资源约束
- 预计开发时间：4-6 小时

### 依赖约束
- 需确认 `scripts/kbd/logs/` 目录权限

## 风险与假设

### 已知风险
- 大批量运行时进度 JSON 可能频繁更新，需注意性能

### 假设条件
- logs 目录可通过 `settings.KBD_LOGS_DIR.mkdir(parents=True)` 自动创建