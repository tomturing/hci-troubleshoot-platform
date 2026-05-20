# KBD Pipeline 配置修复及自动 Port-Forward 检测

## 问题背景

执行 `uv run PYTHONPATH=data-pipeline python -m kbd.run pipeline --ids 35694` 时遇到两个问题：

1. **Vision OCR 修复后，内容未更新**：图片识别成功，但 API 返回幂等跳过，数据库内容未变更
2. **kb-service 不可达**：本地执行 `import` stage 时，无法访问 k3s ClusterIP 服务

## 根因分析

### 问题一：API 幂等性导致无法更新

原 API 设计：
```python
# backend/kb-service/app/routes/ingest.py
@router.post("/kbd/ingest")
async def ingest_kbd_entry(request: Request, body: KbdIngestRequest):
    # 已存在记录 → 直接跳过，无法更新
    if existing_entry:
        return KbdIngestResponse(action="skipped", message="记录已存在，幂等跳过")
```

Pipeline 调用端：
```python
# data-pipeline/kbd/importer.py
stats = await import_batch(ready_ids, None, force_draft=args.force, client=client)
```

问题链路：
1. Vision OCR 成功，生成新的 `content_md`
2. Import API 检测到 `support_id` 已存在
3. API 幂等跳过，不更新 `content_md`
4. 数据库内容未变更

### 问题二：k3s ClusterIP 服务不可达

原代码：
```python
# data-pipeline/kbd/importer.py
async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
    stats = await import_batch(...)  # 直接调用 API，失败返回 ConnectionError
```

问题链路：
1. kb-service 部署在 k3s，Service 类型为 ClusterIP
2. 本地开发环境无法直接访问 ClusterIP
3. Import API 调用失败，返回 ConnectionError

## 修复方案

### 方案一：Override + Override_status 参数设计

**设计依据（第一性原理 + 业界最佳实践）：**

1. **幂等 vs 覆盖**：参考 REST API 设计规范
   - POST 默认幂等（相同 resource_id 不重复创建）
   - 需要更新时，提供 `override` 参数强制覆盖

2. **状态过滤**：防止误覆盖已发布数据
   - 默认仅覆盖 `draft` 状态（安全默认）
   - `['all']` = 所有状态（需谨慎使用）
   - `['draft', 'published']` = 仅指定状态

**API 参数矩阵：**

| override | override_status | 记录状态 | 结果 |
|----------|-----------------|---------|------|
| false    | -               | 不存在  | ✅ created |
| false    | -               | 已存在  | ⏭️ skipped |
| true     | 不传（默认draft）| draft   | ✅ overridden |
| true     | 不传（默认draft）| published | ⏭️ skipped（状态不匹配）|
| true     | ['all']         | draft   | ✅ overridden |
| true     | ['all']         | published | ✅ overridden（谨慎使用）|
| true     | ['draft','published'] | draft | ✅ overridden |

**实现变更：**

API 层（backend/kb-service/app/routes/ingest.py）：
```python
class KbdIngestRequest(BaseModel):
    override: bool = Field(False, description="强制覆盖已存在的记录")
    override_status: list[str] | None = Field(None, description="仅覆盖指定状态的记录")
    force_update: bool = Field(False, description="[已废弃] 请使用 override 参数")

# 参数解析逻辑
DEFAULT_OVERRIDE_STATUS = ["draft"]
ALL_STATUS_MARKER = ["all", "*"]
if body.override_status is None:
    allowed_statuses = DEFAULT_OVERRIDE_STATUS
elif any(s in ALL_STATUS_MARKER for s in body.override_status):
    allowed_statuses = None  # 无限制
else:
    allowed_statuses = body.override_status
```

调用层（data-pipeline/kbd/importer.py）：
```python
async def import_batch(
    support_ids: list[str],
    _pool: Any = None,
    *,
    override: bool = False,
    override_status: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
```

Pipeline 层（data-pipeline/kbd/pipeline.py）：
```python
async def run_pipeline(
    case_ids: list[str],
    stages: Sequence[Stage],
    *,
    force_fetch: bool = False,  # Stage 1：强制重新抓取
    override: bool = False,      # Stage 3：强制覆盖
    override_status: list[str] | None = None,  # Stage 3：状态过滤
) -> dict[str, dict]:
```

CLI 层（data-pipeline/kbd/run.py）：
```bash
# 强制覆盖 draft 状态的记录
PYTHONPATH=data-pipeline python -m kbd.run import --ids 35694 --override

# 强制覆盖所有状态的记录（谨慎使用）
PYTHONPATH=data-pipeline python -m kbd.run import --ids 35694 --override --override-status all

# 强制覆盖指定状态的记录
PYTHONPATH=data-pipeline python -m kbd.run import --ids 35694 --override --override-status draft,published
```

### 方案二：自动 Port-Forward 检测

**设计依据：**

1. **k3s ClusterIP 特性**：Service 仅在集群内部可达
2. **本地开发需求**：需要访问 kb-service API
3. **自动化原则**：无需手动启动 port-forward

**实现变更（data-pipeline/kbd/importer.py）：**

```python
def _check_kb_service_reachable(timeout: float = 2.0) -> bool:
    """快速检测 kb-service 是否可达"""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{settings.KB_SERVICE_URL}/docs", follow_redirects=True)
            return resp.status_code < 500
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        return False

def _start_port_forward() -> subprocess.Popen | None:
    """启动 kubectl port-forward 将 kb-service 暴露到本地"""
    cmd = ["kubectl", "port-forward", "svc/kb-service", "-n", "hci-dev", "8080:8080"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 等待端口就绪（最多 5 秒）
    for _ in range(10):
        time.sleep(0.5)
        if _check_kb_service_reachable():
            return proc
    proc.terminate()
    return None

def ensure_kb_service_reachable() -> bool:
    """确保 kb-service 可达，自动启动 port-forward（如果需要）"""
    if _check_kb_service_reachable():
        return True
    # 启动 port-forward
    proc = _start_port_forward()
    return proc is not None
```

调用时机：
```python
async def import_batch(...) -> dict[str, int]:
    # 自动检测并启动 port-forward
    if not ensure_kb_service_reachable():
        logger.error("kb-service 不可达，无法执行入库操作")
        stats["error"] = total
        return stats
    # 继续执行 import ...
```

## 影响文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `backend/kb-service/app/routes/ingest.py` | 功能增强 | API override + override_status 支持 |
| `data-pipeline/kbd/importer.py` | 功能增强 | 调用端 override 支持 + 自动 port-forward |
| `data-pipeline/kbd/pipeline.py` | 功能增强 | Pipeline 参数传递 |
| `data-pipeline/kbd/run.py` | 功能增强 | CLI 参数解析 |

## 后续任务

- 重新构建 kb-service Docker 镜像以启用 API override 功能
- 测试完整流水线：`PYTHONPATH=data-pipeline python -m kbd.run pipeline --ids 35694 --override`

## 关联 PIT

无（新功能设计）

---

[env:dev:gs][agent:claude]