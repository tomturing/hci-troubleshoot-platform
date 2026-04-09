---
status: active
category: solution
audience: developer
last_updated: 2026-04-09
owner: team
---

# kb-service 导入功能修复方案

## 背景与需求
见 [需求文档](../../requirement/events/2026-04-09-kb-service-import-fix.md)

## 方案概述

### 问题一：文件上传处理修复
修改 `backend/kb-service/app/routes/categories.py` 中的 `/import` 端点：
- 使用 `UploadFile = File(...)` 接收文件
- 使用 `dry_run: bool = Query(...)` 接收查询参数
- 移除错误的 `CategoryImportRequest` Pydantic 模型解析

### 问题二：文档完善
更新脚本目录下的文档，说明不同环境的配置方法。

## 详细设计

### 问题一代码变更

**变更前**（错误）：
```python
class CategoryImportRequest(BaseModel):
    dry_run: bool = Field(False, ...)

@router.post("/import")
async def import_categories(request: Request, body: CategoryImportRequest):
    content = await request.body()  # 无法解析 FormData
```

**变更后**（正确）：
```python
@router.post("/import")
async def import_categories(
    request: Request,
    file: UploadFile = File(..., description="YAML 分类文件"),
    dry_run: bool = Query(default=False, description="仅验证不写入"),
):
    content = await file.read()  # 正确读取文件内容
```

## 影响范围
- `backend/kb-service/app/routes/categories.py` — 核心修复
- `scripts/kbd/.env.example` — 文档完善
- `scripts/kbd/import_sop.py` — 文档完善

## 风险与缓解
| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| API 行为变化 | 低 | 低 | 前端已使用 FormData，修复后行为一致 |