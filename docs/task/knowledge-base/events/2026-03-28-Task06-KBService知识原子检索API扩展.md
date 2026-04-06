---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 06
---

# Task 06：KB Service 知识原子检索 API 扩展（P1）

```
你是一名负责 hci-troubleshoot-platform kb-service 的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
Task 04 新建了 knowledge_atoms 表，Task 05 实现了内容写入。
本任务扩展 KB Service 的检索 API，让 conversation-service 能通过
双路检索（精确匹配 + 语义检索）获取知识原子，为后续 ReAct 工具调用提供支撑。

双路检索设计：
  路由1（优先）：task_error_keywords 精确匹配 → 返回关联知识原子
  路由2（fallback）：语义检索（BM25 + pgvector + RRF，现有实现改造）
  结果融合：精确匹配优先，补充语义检索结果，按 confidence × similarity 排序
  版本过滤：applicable_version_min/max 与请求中的 hci_version 对比

前置条件：Task 04（表结构）、Task 05（数据写入）已完成。

【任务目标】
1. 在 kb-service 新增 POST /api/v1/atoms/search 接口
2. 实现双路检索逻辑（精确 + 语义，结果融合）
3. 支持按 hci_version 过滤（排除不适用版本的知识原子）
4. 在 conversation-service 的 kb_client.py 中新增调用方法
5. 端到端验证：agent 发问 → kb_client → KB Service → 返回知识原子

【涉及服务 / 文件范围】
允许修改：
  - backend/kb-service/app/api/（新增 atoms.py 路由）
  - backend/kb-service/app/services/（新增 atom_search_service.py）
  - backend/conversation-service/app/services/kb_client.py（新增方法）
只读参考：
  - backend/kb-service/app/services/search_engine.py（现有检索实现）
  - docs/architecture/知识库重建设计方案.md § 六、2 检索策略

【详细实现步骤】

Step 1：新增 atoms 路由

backend/kb-service/app/api/atoms.py：

```python
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/atoms", tags=["知识原子"])

class AtomSearchRequest(BaseModel):
    query: str
    category_id: str | None = None
    knowledge_type: str | None = None          # diagnostic_step|fix_action等
    stage: str | None = None                   # S0-S6
    hci_version: str | None = None             # 如 "6.11.1"
    top_k: int = 5
    task_error_keywords: list[str] = []        # 精确匹配关键词

class AtomSearchResponse(BaseModel):
    atoms: list[dict]
    total: int
    matched_by: str    # "exact" | "semantic" | "hybrid"

@router.post("/search", response_model=AtomSearchResponse)
async def search_atoms(req: AtomSearchRequest, db=Depends(get_db)):
    service = AtomSearchService(db)
    return await service.search(req)

@router.post("", status_code=201)
async def create_atom(atom: dict, db=Depends(get_db)):
    """写入知识原子（供 DocxExtractor 调用）"""
    ...
```

Step 2：实现双路检索服务

backend/kb-service/app/services/atom_search_service.py：

```python
class AtomSearchService:
    async def search(self, req: AtomSearchRequest) -> AtomSearchResponse:
        results = []
        matched_by = "semantic"

        # 路由1：精确匹配（优先）
        if req.task_error_keywords:
            exact_results = await self._exact_match(req.task_error_keywords)
            if exact_results:
                results.extend(exact_results)
                matched_by = "exact"

        # 路由2：语义检索（补充或 fallback）
        if len(results) < req.top_k:
            remaining = req.top_k - len(results)
            semantic_results = await self._semantic_search(req.query, remaining)
            # 去重（已在 exact 结果中的不重复添加）
            seen_ids = {r['id'] for r in results}
            results.extend([r for r in semantic_results if r['id'] not in seen_ids])
            if results and matched_by == "exact":
                matched_by = "hybrid"

        # 版本过滤
        if req.hci_version:
            results = self._filter_by_version(results, req.hci_version)

        # 按 confidence 排序
        results.sort(key=lambda x: x.get('confidence', 0.8), reverse=True)

        return AtomSearchResponse(
            atoms=results[:req.top_k],
            total=len(results),
            matched_by=matched_by
        )

    async def _exact_match(self, keywords: list[str]) -> list[dict]:
        """通过 task_error_keywords 精确匹配 trigger.task_error_keywords JSONB"""
        # PostgreSQL JSONB 包含查询
        # WHERE trigger->'task_error_keywords' @> ANY(ARRAY['"CPU不足"']::jsonb[])
        ...

    def _filter_by_version(self, atoms: list[dict], version: str) -> list[dict]:
        """过滤不适用当前 HCI 版本的知识原子"""
        from packaging import version as v
        current = v.parse(version)
        filtered = []
        for atom in atoms:
            min_v = atom.get('applicable_version_min')
            max_v = atom.get('applicable_version_max')
            if min_v and v.parse(min_v) > current:
                continue   # 版本太低，跳过
            if max_v and v.parse(max_v) < current:
                continue   # 版本太高，跳过
            filtered.append(atom)
        return filtered
```

Step 3：扩展 kb_client.py

```python
# 在 backend/conversation-service/app/services/kb_client.py 中新增：
async def search_atoms(
    self,
    query: str,
    category_id: str | None = None,
    task_error_keywords: list[str] | None = None,
    hci_version: str | None = None,
    top_k: int = 5,
) -> list[dict]:
    """检索知识原子（新接口，供 ReAct 工具调用）"""
    resp = await self.client.post(
        f"{self.base_url}/api/v1/atoms/search",
        json={
            "query": query,
            "category_id": category_id,
            "task_error_keywords": task_error_keywords or [],
            "hci_version": hci_version,
            "top_k": top_k,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["atoms"]
```

Step 4：端到端验证

```bash
# 1. 直接查 KB Service
curl -X POST http://localhost:8004/api/v1/atoms/search \
  -H "Content-Type: application/json" \
  -d '{"query": "虚拟机开机失败", "task_error_keywords": ["CPU不足"]}'
# 预期：matched_by="exact", atoms 包含含 acli system top 的知识原子

# 2. 通过 conversation-service 联调
# 发送包含"CPU不足"的对话消息，在 conversation-service 日志中
# 确认 kb_client.search_atoms 被调用且返回结果
```

【约束】
- 精确匹配必须比语义检索快（不做向量计算）
- 版本过滤失败时不应抛异常（降级为不过滤）
- kb_client 的新方法与旧的 search_knowledge 方法并存（旧方法不删除）

【验收标准】
- [ ] POST /api/v1/atoms/search 返回 200，且含 matched_by 字段
- [ ] task_error_keywords=["CPU不足"] 命中精确匹配（matched_by="exact"）
- [ ] version="5.0" 过滤时不返回 min_version="6.7.0" 的知识原子
- [ ] kb_client.search_atoms 方法正常工作
- [ ] uv run pytest backend/kb-service/tests/ -q 通过（含新增原子检索测试）
- [ ] make lint 无新增错误
```

---

# 33_任务编排_P2_诊断状态机

> **阶段**：Phase 2 — 诊断状态机（Phase 0 和 Phase 1 知识库复活完成后）  
> **目标**：实现 S0-S6 诊断阶段状态机，让对话能沿着专家诊断流程推进，而不是停留在一轮问答  
> **并行条件**：T07（DB迁移）必须先于 T08，T07 完成后 T08 可独立执行  
> **前置依赖**：Task 01（Prompt重写）、Task 04（知识原子DB设计）  
> **创建日期**：2026-03-22  
> **关联文档**：
> - [docs/architecture/完整技术方案.md](../architecture/完整技术方案.md) § 五、Phase 2
> - [docs/architecture/各层最优设计.md](../architecture/各层最优设计.md) § Layer 1/6

---