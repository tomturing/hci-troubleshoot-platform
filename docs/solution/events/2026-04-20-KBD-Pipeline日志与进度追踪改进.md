---
status: active
category: solution
audience: developer
last_updated: 2026-04-20
owner: team
---

# KBD Pipeline 日志与进度追踪改进方案

## 背景与需求
详见 [需求文档](../../requirement/events/2026-04-20-KBD-Pipeline日志与进度追踪改进.md)

## 方案概述 (WHAT)
为 KBD Pipeline 添加日志持久化、进度追踪、--resume 和 --failed-only 参数，增强运维可观测性和断点续传能力。

## 详细设计

### 架构变更
无架构变更，仅模块内部新增功能：
- 新增 `data-pipeline/kbd/progress.py` 模块（进度追踪）
- 新增 `data-pipeline/kbd/logs/` 目录（日志和进度文件存储）

### 文件变更

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `data-pipeline/kbd/config.py` | 修改 | 添加 `KBD_LOGS_DIR` 字段 |
| `data-pipeline/kbd/progress.py` | 新增 | 进度追踪模块（约 100 行） |
| `data-pipeline/kbd/fetcher.py` | 修改 | 添加 `_is_fetch_failed()`, `get_failed_fetch_ids()` |
| `data-pipeline/kbd/image_proc.py` | 修改 | 添加 `_has_failed_vision()`, `get_failed_vision_ids()` |
| `data-pipeline/kbd/pipeline.py` | 修改 | 集成进度追踪，添加 resume/failed-only 逻辑 |
| `data-pipeline/kbd/run.py` | 修改 | 日志配置、CLI 参数、主函数入口 |
| `docs/solution/knowledge-base/知识库设计.md` | 修改 | 更新 Pipeline 运行日志章节 |

### 关键技术点

#### 1. 日志配置（双 Handler）

**设计原则**：
- 终端输出 INFO 级别（简洁）
- 文件保存 DEBUG 级别（详细）
- 所有 kbd.* 子模块日志统一写入同一文件

**核心代码**：
```python
def _setup_logging(run_id: str | None = None) -> str:
    if run_id is None:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    settings.KBD_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.KBD_LOGS_DIR / f"kbd_{run_id}.log"
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    
    # 终端输出
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)
    
    # 文件持久化
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    return run_id
```

#### 2. 进度 JSON 结构

**设计原则**：
- run_id 为唯一标识
- 每个 case 有四个 stage 状态
- Stage 统计字段便于快速查看进度

**JSON 结构**：
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

#### 3. --resume 逻辑

**设计原则**：
- 加载最新 progress.json
- 跳过已完成的 case（状态为 done/skipped）
- 继续使用原有 run_id（追加日志）

**流程**：
```python
if resume:
    if resume_run_id is None:
        resume_run_id = find_latest_progress_file()
    if resume_run_id:
        progress = load_progress(resume_run_id)
        # 跳过已完成的案例
        completed = get_completed_ids_for_stage(progress, stage)
        case_ids = [cid for cid in case_ids if cid not in completed]
        run_id = resume_run_id
```

#### 4. --failed-only 逻辑

**设计原则**：
- 扫描 cache 目录查找 `.failed` 或识别失败的标记
- Vision: 检查 `img_N.desc.failed` 或 desc.txt 内容为"无文字"
- Fetch: 检查 `fetch.failed` 或 `img_N.failed`

**实现**：
```python
def get_failed_vision_ids(case_ids: list[str]) -> list[str]:
    from .fetcher import _case_dir
    failed = []
    for cid in case_ids:
        case_dir = _case_dir(cid)
        # 有 .desc.failed 文件
        if list(case_dir.glob("img_*.desc.failed")):
            failed.append(cid)
            continue
        # desc.txt 内容为"无文字"
        for desc_file in case_dir.glob("img_*.desc.txt"):
            content = desc_file.read_text()
            if "（无文字）" in content:
                failed.append(cid)
                break
    return failed
```

## 决策依据 (WHY)

### 方案选择：进度文件格式

| 方案 | 优点 | 缺点 | 评分 |
|------|------|------|------|
| **方案A：JSON 文件（选中）** | 结构化、易于解析、Python stdlib 支持 | 文件 IO | ★★★★☆ |
| 方案B：SQLite 数据库 | 查询灵活、并发安全 | 新增依赖、复杂度高 | ★★★☆☆ |
| 方案C：CSV 文件 | 简单 | 无结构、解析麻烦 | ★★☆☆☆ |

### 为什么选择方案A？
- JSON 是最简单的结构化格式
- Python stdlib 内置 json 模块，无需新增依赖
- 便于人工查看和调试
- 进度文件通常小于 1MB，性能足够

### 方案选择：日志 Handler 配置

| 方案 | 优点 | 缺点 | 评分 |
|------|------|------|------|
| **方案A：root logger + 双 handler（选中）** | 所有子模块统一管理 | 可能影响其他模块 | ★★★★☆ |
| 方案B：每个模块单独 FileHandler | 精确控制 | 日志分散、重复配置 | ★★★☆☆ |
| 方案C：仅文件日志（无终端） | 简单 | 无法实时监控 | ★★☆☆☆ |

### 为什么选择方案A？
- 所有 kbd.* 子模块日志统一写入同一文件
- 终端实时输出便于监控
- 文件保存便于追溯

### 权衡与妥协
- 进度 JSON 在每个 Stage 完成后更新（而非每个 case），减少 IO 频率
- --failed-only 仅检查 `.failed` 文件和"无文字"内容，不检查所有可能失败类型

## 影响范围

### 受影响的模块
- data-pipeline/kbd/ — Pipeline 所有脚本
- docs/solution/knowledge-base/ — 知识库设计文档

### 需要更新的文档
- [x] `docs/requirement/events/2026-04-20-KBD-Pipeline日志与进度追踪改进.md`
- [x] `docs/solution/events/2026-04-20-KBD-Pipeline日志与进度追踪改进.md`
- [ ] `docs/solution/knowledge-base/知识库设计.md` — 更新 Pipeline 运行日志章节
- [ ] `docs/task/events/2026-04-20-KBD-Pipeline日志与进度追踪改进.md`

### API兼容性
无破坏性变更，现有 CLI 行为不变。

## 实施计划

### 任务分解（预计 4-6 小时）

| 任务 | 预计耗时 | 依赖 |
|------|---------|------|
| T1: config.py 添加 KBD_LOGS_DIR | 10min | 无 |
| T2: 创建 progress.py 模块 | 60min | T1 |
| T3: fetcher.py 添加失败检测函数 | 20min | 无 |
| T4: image_proc.py 添加失败检测函数 | 20min | 无 |
| T5: run.py 日志配置和 CLI 参数 | 40min | T1, T2 |
| T6: pipeline.py 集成进度追踪 | 60min | T2, T3, T4 |
| T7: 本地验证 | 30min | T5, T6 |
| T8: 文档更新 | 30min | T7 |

## 测试策略

### 单元测试
- progress.py 函数测试（init_progress, update_stage_status, find_latest_progress_file）
- get_failed_vision_ids() 测试（检查 .failed 文件和"无文字"内容）

### 集成测试
- 运行 Pipeline 后检查日志文件和进度文件
- --resume 参数验证（中断后继续）
- --failed-only 参数验证（筛选失败案例）

### 人工测试
```bash
# 测试日志持久化
uv run PYTHONPATH=data-pipeline python -m kbd.run pipeline --ids 15414 --stages fetch
ls data-pipeline/kbd/logs/
cat data-pipeline/kbd/logs/kbd_*.log

# 测试进度文件
cat data-pipeline/kbd/logs/progress_*.json

# 测试 resume
uv run PYTHONPATH=data-pipeline python -m kbd.run pipeline --ids 15414,15415 --stages fetch --resume

# 测试 failed-only（先删除部分 desc.txt）
find data-pipeline/kbd/cache -name "*.desc.txt" -exec grep -l "（无文字）" {} \; | head -5 | xargs rm
uv run PYTHONPATH=data-pipeline python -m kbd.run vision --failed-only --ids 15414
```

## 验收标准
- [x] 标准1：需求文档已编写并确认
- [x] 标准2：方案文档已编写，包含决策依据
- [ ] 标准3：代码实现完成，lint 检查通过
- [ ] 标准4：本地验证成功
- [ ] 标准5：知识库设计.md 已更新

## 变更历史
| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-20 | v1.0 | 初版 | 本文档 |