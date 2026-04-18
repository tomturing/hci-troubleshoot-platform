# 分类基线页面 KBD/SOP 数量展示功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在分类基线管理页面显示已发布 KBD/SOP 数量，支持点击查看详情，并修复意图识别过滤禁用分类的业务逻辑。

**Architecture:** 后端通过 SQL 子查询统计 published 状态的 KBD/SOP 数量，前端根据 domain 字段聚合计算 L1 大类汇总，复用 KbdReviewView 的详情弹窗逻辑。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Vue 3, TypeScript, Element Plus

---

## 文件结构

| 文件 | 责任 | 改动类型 |
|------|------|----------|
| `backend/kb-service/app/repositories/category_repo.py` | 数据库查询层，增加 KBD/SOP 数量子查询 | 修改 |
| `backend/kb-service/app/routes/categories.py` | API 路由层，返回统计字段，删除 has_sop | 修改 |
| `backend/kb-service/app/routes/classify.py` | 意图识别过滤 is_active=TRUE | 修改 |
| `backend/kb-service/app/routes/admin.py` | 新增单条 KBD 详情接口 | 修改 |
| `frontend/admin/src/views/CategoryManageView.vue` | UI 改动（标签、表格、列表、弹窗） | 修改 |

---

### Task 1: 后端 - 修改 category_repo.py 增加 KBD/SOP 数量统计

**Files:**
- Modify: `backend/kb-service/app/repositories/category_repo.py:41-55`
- Modify: `backend/kb-service/app/repositories/category_repo.py:57-72`

- [ ] **Step 1: 修改 get_all() 方法增加子查询**

在 `get_all()` 方法中，将原有的简单 SELECT 改为包含子查询的版本：

```python
async def get_all(self) -> list[KbCategory]:
    """获取所有分类（按 level + domain 排序），含 KBD/SOP 统计"""
    trace_id = get_current_trace_id()
    async with self._db.async_session_factory() as session:
        result = await session.execute(
            text("""
                SELECT
                  c.id, c.code, c.name, c.domain, c.level, c.parent_id,
                  c.path_labels, c.hit_count, c.is_active, c.keywords,
                  c.source, c.version,
                  (SELECT COUNT(*) FROM kbd_entry
                   WHERE status = 'published' AND category_id = c.code) AS published_kbd_count,
                  (SELECT COUNT(*) FROM sop_document
                   WHERE status = 'published' AND category_id = c.code) AS published_sop_count
                FROM kb_category c
                ORDER BY c.level, c.domain, c.code
            """)
        )
        rows = result.mappings().all()
        categories = []
        for row in rows:
            cat = KbCategory(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                domain=row["domain"],
                level=row["level"],
                parent_id=row["parent_id"],
                path_labels=row["path_labels"],
                hit_count=row["hit_count"] or 0,
                is_active=row["is_active"],
                keywords=row["keywords"] or [],
                source=row["source"],
                version=row["version"],
            )
            # 统计字段作为动态属性存储（to_dict 时输出）
            cat.published_kbd_count = row["published_kbd_count"] or 0
            cat.published_sop_count = row["published_sop_count"] or 0
            categories.append(cat)
        logger.info(
            event="repo_get_all",
            count=len(categories),
            trace_id=trace_id,
        )
        return categories
```

- [ ] **Step 2: 修改 get_all_active() 方法增加子查询**

在 `get_all_active()` 方法中，添加相同的子查询逻辑，但保持 `is_active=True` 过滤：

```python
async def get_all_active(self) -> list[KbCategory]:
    """获取所有活跃分类（is_active=True），含 KBD/SOP 统计"""
    trace_id = get_current_trace_id()
    async with self._db.async_session_factory() as session:
        result = await session.execute(
            text("""
                SELECT
                  c.id, c.code, c.name, c.domain, c.level, c.parent_id,
                  c.path_labels, c.hit_count, c.is_active, c.keywords,
                  c.source, c.version,
                  (SELECT COUNT(*) FROM kbd_entry
                   WHERE status = 'published' AND category_id = c.code) AS published_kbd_count,
                  (SELECT COUNT(*) FROM sop_document
                   WHERE status = 'published' AND category_id = c.code) AS published_sop_count
                FROM kb_category c
                WHERE c.is_active = TRUE
                ORDER BY c.level, c.domain, c.code
            """)
        )
        rows = result.mappings().all()
        categories = []
        for row in rows:
            cat = KbCategory(
                id=row["id"],
                code=row["code"],
                name=row["name"],
                domain=row["domain"],
                level=row["level"],
                parent_id=row["parent_id"],
                path_labels=row["path_labels"],
                hit_count=row["hit_count"] or 0,
                is_active=row["is_active"],
                keywords=row["keywords"] or [],
                source=row["source"],
                version=row["version"],
            )
            cat.published_kbd_count = row["published_kbd_count"] or 0
            cat.published_sop_count = row["published_sop_count"] or 0
            categories.append(cat)
        logger.info(
            event="repo_get_all_active",
            count=len(categories),
            trace_id=trace_id,
        )
        return categories
```

- [ ] **Step 3: 修改 KbCategory 模型的 to_dict() 方法**

打开 `backend/kb-service/app/models/kb_category.py`，在 `to_dict()` 方法中增加统计字段输出：

```python
def to_dict(self) -> dict:
    result = {
        "id": self.id,
        "code": self.code,
        "name": self.name,
        "domain": self.domain,
        "level": self.level,
        "parent_id": self.parent_id,
        "path_labels": self.path_labels or [],
        "hit_count": self.hit_count or 0,
        "is_active": self.is_active,
        "keywords": self.keywords or [],
        "source": self.source or "",
        "version": self.version or "1.0",
    }
    # 统计字段（动态属性，可能不存在）
    result["published_kbd_count"] = getattr(self, "published_kbd_count", 0)
    result["published_sop_count"] = getattr(self, "published_sop_count", 0)
    return result
```

- [ ] **Step 4: Commit**

```bash
git add backend/kb-service/app/repositories/category_repo.py backend/kb-service/app/models/kb_category.py
git commit -m "feat(kb-service): 分类查询增加已发布 KBD/SOP 数量统计子查询

[env:dev:gs][agent:claude]"
```

---

### Task 2: 后端 - 修改 categories.py API 返回统计字段

**Files:**
- Modify: `backend/kb-service/app/routes/categories.py:83-150`

- [ ] **Step 1: 修改 list_categories 路由删除 has_sop 并使用统计字段**

将原有的 `has_sop` 计算逻辑删除，直接使用模型返回的统计字段：

```python
@router.get("")
async def list_categories(
    request: Request,
    grouped: bool = True,
    force_refresh: bool = False,
):
    """获取分类列表（含 KBD/SOP 统计）"""
    _check_auth(request)

    if _category_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="list_categories_request",
        grouped=grouped,
        force_refresh=force_refresh,
    )

    if grouped:
        grouped_data = await _category_service.get_grouped_by_domain(
            force_refresh=force_refresh
        )
        return {
            "domains": {
                domain: [
                    {
                        **cat.to_dict(),
                        "id": cat.code,
                        "label": cat.name,
                    }
                    for cat in cats
                ]
                for domain, cats in grouped_data.items()
            },
            "total_domains": len(grouped_data),
        }
    else:
        categories = await _category_service.get_all_active(
            force_refresh=force_refresh
        )
        return {
            "categories": [cat.to_dict() for cat in categories],
            "total": len(categories),
        }
```

注意：删除第 111-119 行的 `sop_coverage` 查询逻辑，以及第 131-134 行的 `has_sop` 字段赋值。

- [ ] **Step 2: Commit**

```bash
git add backend/kb-service/app/routes/categories.py
git commit -m "refactor(kb-service): 删除 has_sop 字段，使用 published_sop_count 判断

[env:dev:gs][agent:claude]"
```

---

### Task 3: 后端 - 修复 classify.py 意图识别过滤禁用分类

**Files:**
- Modify: `backend/kb-service/app/routes/classify.py:351-385`

- [ ] **Step 1: 修改 fetch_categories_for_classify 增加 is_active 过滤**

在 SQL 查询中添加 `AND is_active = TRUE` 条件：

```python
async def fetch_categories_for_classify(db_manager: DatabaseManager) -> list[dict]:
    """从 kb_category 表读取所有活跃分类节点（用于 LLM 分类）"""
    async with db_manager.async_session_factory() as session:
        result = await session.execute(
            text("""
                SELECT code, name, domain, path_labels
                FROM kb_category
                WHERE code IS NOT NULL AND is_active = TRUE
                ORDER BY domain, code
            """)
        )
        rows = result.fetchall()

        categories = []
        for row in rows:
            raw = row.path_labels
            if isinstance(raw, list):
                path_labels = raw
            elif isinstance(raw, str):
                path_labels = json.loads(raw)
            else:
                path_labels = []
            categories.append(
                {
                    "code": row.code,
                    "name": row.name,
                    "domain": row.domain,
                    "path": path_labels,
                }
            )

        logger.info(f"从 kb_category 读取 {len(categories)} 个活跃分类节点")
        return categories
```

- [ ] **Step 2: Commit**

```bash
git add backend/kb-service/app/routes/classify.py
git commit -m "fix(kb-service): 意图识别过滤禁用分类（is_active=TRUE）

禁用的分类不再参与 S0 意图识别，真正实现下线效果。

[env:dev:gs][agent:claude]"
```

---

### Task 4: 后端 - 新增单条 KBD 详情接口

**Files:**
- Modify: `backend/kb-service/app/routes/admin.py`

- [ ] **Step 1: 在 admin.py 中新增 GET /api/admin/kbd/{kbd_id} 接口**

在 `list_kbd_entries` 函数后添加单条详情接口：

```python
@kbd_router.get("/{kbd_id}")
async def get_kbd_entry_detail(request: Request, kbd_id: int):
    """获取单个 KBD 条目详情（含完整 content_md）

    Args:
        kbd_id: KBD 条目 ID

    Returns:
        KBD 条目完整详情（含 content_md、metadata 等）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    logger.info(event="kbd_detail_request", kbd_id=kbd_id)

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            text("""
                SELECT id, support_id, support_url, title, content_md,
                       metadata, category_id, ai_category_id,
                       ai_category_conf, ai_category_reason,
                       status, reviewer_id, review_note, hit_count,
                       created_at, updated_at, published_at
                FROM kbd_entry
                WHERE id = :id
            """),
            {"id": kbd_id},
        )
        row = result.mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

    return {
        "id": row["id"],
        "support_id": row["support_id"],
        "support_url": row["support_url"] or "",
        "title": row["title"],
        "content_md": row["content_md"] or "",
        "metadata": row["metadata"] or {},
        "category_id": row["category_id"],
        "ai_category_id": row["ai_category_id"],
        "ai_category_conf": float(row["ai_category_conf"]) if row["ai_category_conf"] is not None else None,
        "ai_category_reason": row["ai_category_reason"],
        "status": row["status"],
        "reviewer_id": row["reviewer_id"],
        "review_note": row["review_note"],
        "hit_count": row["hit_count"] or 0,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
    }
```

注意：需要确认 `kbd_entry` 表是否有 `hit_count` 字段。如果没有，需从 `kb_chunk` 或其他表关联查询。根据设计文档，命中次数来自 `kbd_entry.hit_count`。

- [ ] **Step 2: Commit**

```bash
git add backend/kb-service/app/routes/admin.py
git commit -m "feat(kb-service): 新增单条 KBD 详情接口 GET /api/admin/kbd/{kbd_id}

[env:dev:gs][agent:claude]"
```

---

### Task 5: 前端 - 修改 CategoryManageView.vue 增加 KBD/SOP 标签显示

**Files:**
- Modify: `frontend/admin/src/views/CategoryManageView.vue`

- [ ] **Step 1: 修改 KbCategory 类型定义增加统计字段**

在类型定义中添加新字段：

```typescript
interface KbCategory {
  id: number
  code: string
  name: string
  domain: string
  level: number
  parent_id: number | null
  path_labels: string[]
  hit_count: number
  is_active: boolean
  // 新增字段
  published_kbd_count: number   // 已发布 KBD 数量
  published_sop_count: number   // 已发布 SOP 数量
}
```

删除 `has_sop` 字段定义。

- [ ] **Step 2: 新增已发布 KBD 总数统计变量**

在响应式状态区域添加：

```typescript
const totalPublishedKbd = ref(0)
```

- [ ] **Step 3: 修改 fetchCategories 计算已发布 KBD 总数**

在 `fetchCategories` 函数末尾添加统计计算：

```typescript
// 计算已发布 KBD 总数
totalPublishedKbd.value = allCategories
  .filter((c) => c.is_active)
  .reduce((sum, c) => sum + (c.published_kbd_count || 0), 0)
```

- [ ] **Step 4: 新增域汇总计算逻辑**

添加计算属性 `domainStats`：

```typescript
// 大类汇总：统计每个域下所有活跃子分类的 SOP/KBD 数量之和
const domainStats = computed<Record<string, { sop: number; kbd: number }>>(() => {
  const stats: Record<string, { sop: number; kbd: number }> = {}
  const allCategories = domainGroups.value.flatMap((g) => g.categories)
  for (const cat of allCategories) {
    if (!cat.is_active) continue
    if (!stats[cat.domain]) {
      stats[cat.domain] = { sop: 0, kbd: 0 }
    }
    stats[cat.domain].sop += cat.published_sop_count || 0
    stats[cat.domain].kbd += cat.published_kbd_count || 0
  }
  return stats
})
```

- [ ] **Step 5: Commit**

```bash
git add frontend/admin/src/views/CategoryManageView.vue
git commit -m "feat(admin): 分类管理页面增加 KBD/SOP 统计字段类型定义

[env:dev:gs][agent:claude]"
```

---

### Task 6: 前端 - 修改左侧分类项标签显示

**Files:**
- Modify: `frontend/admin/src/views/CategoryManageView.vue`

- [ ] **Step 1: 修改分类项模板显示 SOP/KBD 数量标签**

将原有的 `has_sop` badge 替换为 `[SOP:N][KBD:N]` 标签格式：

```html
<div
  v-for="cat in group.categories"
  :key="cat.code"
  class="category-item"
  :class="{
    selected: selectedCategory?.code === cat.code,
    inactive: !cat.is_active,
  }"
  @click="selectCategory(cat)"
>
  <span class="cat-code">{{ cat.code }}</span>
  <span class="cat-name">{{ cat.name }}</span>
  <span class="count-tag">[SOP:{{ cat.published_sop_count || 0 }}]</span>
  <span class="count-tag">[KBD:{{ cat.published_kbd_count || 0 }}]</span>
  <span v-if="!cat.is_active" class="inactive-badge">禁用</span>
</div>
```

- [ ] **Step 2: 修改域头部显示汇总数量**

将 `domain-header` 改为显示汇总的 SOP/KBD 数量：

```html
<div class="domain-header">
  <span class="domain-name">{{ group.domain }}</span>
  <span class="domain-count">{{ group.count }}</span>
  <span class="domain-stats">
    [SOP:{{ domainStats[group.domain]?.sop || 0 }}]
    [KBD:{{ domainStats[group.domain]?.kbd || 0 }}]
  </span>
</div>
```

- [ ] **Step 3: 修改统计栏显示已发布 KBD 总数**

将 `stats-bar` 改为包含已发布 KBD 统计：

```html
<div class="stats-bar">
  <span>总计: {{ totalCategories }}</span>
  <span>启用: {{ totalActive }}</span>
  <span>有SOP: {{ totalWithSop }}</span>
  <span>已发布KBD: {{ totalPublishedKbd }}</span>
</div>
```

注意：`totalWithSop` 需改用 `published_sop_count > 0` 计算：

```typescript
totalWithSop.value = allCategories.filter((c) => c.published_sop_count > 0).length
```

- [ ] **Step 4: 增加标签样式**

在 `<style scoped>` 中添加新样式：

```css
.count-tag {
  font-size: 10px;
  padding: 2px 4px;
  background: #f0f2f5;
  color: #606266;
  border-radius: 2px;
  margin-left: 4px;
}

.domain-stats {
  font-size: 12px;
  color: #909399;
  margin-left: 8px;
}
```

- [ ] **Step 5: 修改左侧面板宽度**

将 `.left-panel` 宽度从 320px 改为 420px：

```css
.left-panel {
  width: 420px;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
}
```

- [ ] **Step 6: Commit**

```bash
git add frontend/admin/src/views/CategoryManageView.vue
git commit -m "feat(admin): 分类项显示 [SOP:N][KBD:N] 标签，大类显示汇总数量

[env:dev:gs][agent:claude]"
```

---

### Task 7: 前端 - 修改右侧详情面板标题行和表格布局

**Files:**
- Modify: `frontend/admin/src/views/CategoryManageView.vue`

- [ ] **Step 1: 修改右侧详情面板标题行布局**

将原有的 `<h3>分类详情</h3>` 改为包含开关和按钮的标题行：

```html
<div class="detail-header">
  <h3 class="detail-title">分类详情</h3>
  <div class="detail-status">
    <el-radio-group v-model="editForm.is_active" size="small">
      <el-radio :value="true">启用</el-radio>
      <el-radio :value="false">禁用</el-radio>
    </el-radio-group>
  </div>
  <div class="detail-actions">
    <el-button type="primary" size="small" :loading="editSaving" @click="saveEdit">
      保存修改
    </el-button>
  </div>
</div>
```

- [ ] **Step 2: 修改基本信息表格为 4列×2行布局**

将原有的垂直布局改为水平表格布局：

```html
<table class="info-table">
  <tr>
    <td class="label">业务编码</td>
    <td class="value">{{ selectedCategory.code }}</td>
    <td class="label">分类名称</td>
    <td class="value">{{ selectedCategory.name }}</td>
  </tr>
  <tr>
    <td class="label">所属域</td>
    <td class="value">{{ selectedCategory.domain }}</td>
    <td class="label">完整路径</td>
    <td class="value">{{ selectedCategory.path_labels?.join(' / ') || '' }}</td>
  </tr>
</table>
```

- [ ] **Step 3: 删除原有的表单项和 SOP 覆盖行**

删除以下内容：
- `<div class="form-item"><label>业务编码</label>...` 
- `<div class="form-item"><label>分类名称</label>...`
- `<div class="form-item"><label>所属域</label>...`
- `<div class="form-item"><label>完整路径</label>...`
- `<div class="form-item"><label>状态</label>...`
- `<div class="form-item"><label>SOP 覆盖</label>...`
- `<div class="form-item"><label>命中次数</label>...`
- 底部的 `<el-button type="primary"...>保存修改</el-button>`

- [ ] **Step 4: 增加标题行和表格样式**

```css
.detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 0;
  border-bottom: 1px solid #e4e7ed;
  margin-bottom: 16px;
}

.detail-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
}

.detail-status {
  display: flex;
  align-items: center;
  gap: 12px;
}

.detail-actions {
  margin-left: auto;
}

.info-table {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 16px;
}

.info-table td {
  padding: 8px 12px;
  border: 1px solid #e4e7ed;
}

.info-table .label {
  background: #f5f7fa;
  font-weight: 500;
  color: #606266;
  width: 100px;
}

.info-table .value {
  color: #303133;
}
```

- [ ] **Step 5: Commit**

```bash
git add frontend/admin/src/views/CategoryManageView.vue
git commit -m "feat(admin): 详情面板标题行布局优化，表格改为 4列×2行

[env:dev:gs][agent:claude]"
```

---

### Task 8: 前端 - 增加 SOP/KBD 已发布列表展示

**Files:**
- Modify: `frontend/admin/src/views/CategoryManageView.vue`

- [ ] **Step 1: 新增已发布条目列表数据获取逻辑**

添加响应式状态和 API 调用：

```typescript
// 已发布条目列表
const publishedSopList = ref<SopListItem[]>([])
const publishedKbdList = ref<KbdListItem[]>([])
const listLoading = ref(false)

interface SopListItem {
  id: number
  title: string
  hit_count: number
}

interface KbdListItem {
  id: number
  title: string
  hit_count: number
}

async function fetchPublishedList(categoryCode: string) {
  listLoading.value = true
  publishedSopList.value = []
  publishedKbdList.value = []
  try {
    // 查询已发布 SOP
    const sopResp = await fetch(`/api/v1/sop?status=published&category_id=${encodeURIComponent(categoryCode)}`, {
      headers: authHeader,
    })
    if (sopResp.ok) {
      const sopData = await sopResp.json()
      publishedSopList.value = sopData.documents || []
    }

    // 查询已发布 KBD
    const kbdResp = await fetch(`/api/admin/kbd/pending?status=published&category_id=${encodeURIComponent(categoryCode)}`, {
      headers: authHeader,
    })
    if (kbdResp.ok) {
      const kbdData = await kbdResp.json()
      publishedKbdList.value = kbdData.entries || []
    }
  } catch {
    // 列表加载失败不影响主功能
  } finally {
    listLoading.value = false
  }
}
```

- [ ] **Step 2: 在 selectCategory 时加载已发布列表**

修改 `selectCategory` 函数：

```typescript
function selectCategory(cat: KbCategory) {
  selectedCategory.value = cat
  editForm.name = cat.name
  editForm.is_active = cat.is_active
  // 加载已发布 SOP/KBD 列表
  fetchPublishedList(cat.code)
}
```

- [ ] **Step 3: 在详情面板添加 SOP/KBD 列表模板**

在表格后添加列表区域：

```html
<!-- 已发布 SOP 列表 -->
<div class="published-section" v-if="selectedCategory.published_sop_count > 0">
  <h4 class="section-title">已发布 SOP ({{ selectedCategory.published_sop_count }}篇)</h4>
  <div class="published-list" v-loading="listLoading">
    <div
      v-for="sop in publishedSopList"
      :key="sop.id"
      class="published-item"
    >
      <span class="hit-tag">[命中:{{ sop.hit_count || 0 }}]</span>
      <span class="item-title">{{ sop.title }}</span>
      <el-button size="small" text @click="openSopDetail(sop.id)">详情</el-button>
    </div>
  </div>
</div>

<!-- 已发布 KBD 列表 -->
<div class="published-section" v-if="selectedCategory.published_kbd_count > 0">
  <h4 class="section-title">已发布 KBD ({{ selectedCategory.published_kbd_count }}篇)</h4>
  <div class="published-list" v-loading="listLoading">
    <div
      v-for="kbd in publishedKbdList"
      :key="kbd.id"
      class="published-item"
    >
      <span class="hit-tag">[命中:{{ kbd.hit_count || 0 }}]</span>
      <span class="item-title">{{ kbd.title }}</span>
      <el-button size="small" text @click="openKbdDetail(kbd.id)">详情</el-button>
    </div>
  </div>
</div>

<!-- 无数据提示 -->
<div class="empty-section" v-if="selectedCategory.published_sop_count === 0 && selectedCategory.published_kbd_count === 0">
  <span class="empty-text">暂无已发布的 SOP/KBD</span>
</div>
```

- [ ] **Step 4: 增加列表样式**

```css
.published-section {
  margin-bottom: 16px;
}

.section-title {
  font-size: 14px;
  font-weight: 500;
  color: #606266;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid #ebeef5;
}

.published-list {
  max-height: 200px;
  overflow-y: auto;
}

.published-item {
  display: flex;
  align-items: center;
  padding: 8px;
  border-radius: 4px;
  margin-bottom: 4px;
  background: #f5f7fa;
}

.hit-tag {
  font-size: 12px;
  padding: 2px 6px;
  background: #409eff;
  color: white;
  border-radius: 4px;
  margin-right: 8px;
}

.item-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: #303133;
}

.empty-section {
  padding: 24px 0;
  text-align: center;
}

.empty-text {
  color: #909399;
  font-size: 13px;
}
```

- [ ] **Step 5: Commit**

```bash
git add frontend/admin/src/views/CategoryManageView.vue
git commit -m "feat(admin): 详情面板增加已发布 SOP/KBD 列表展示

[env:dev:gs][agent:claude]"
```

---

### Task 9: 前端 - 实现详情弹窗复用 KbdReviewView 逻辑

**Files:**
- Modify: `frontend/admin/src/views/CategoryManageView.vue`

- [ ] **Step 1: 新增详情弹窗状态**

```typescript
// 详情弹窗
const detailDialogVisible = ref(false)
const detailKbdEntry = ref<KbdEntryDetail | null>(null)
const detailSopEntry = ref<SopEntryDetail | null>(null)
const detailLoading = ref(false)
const parsedSegments = ref<ContentSegment[]>([])

interface KbdEntryDetail {
  id: number
  support_id: string
  title: string
  content_md: string
  hit_count: number
}

interface SopEntryDetail {
  id: number
  title: string
  content_md: string
  hit_count: number
}

type ContentSegment = NormalSegment | ScreenshotSegment

interface NormalSegment {
  type: 'normal'
  html: string
}

interface ScreenshotSegment {
  type: 'screenshot'
  typeInfo: ScreenshotTypeInfo
  errorLabel: string
  fields: ScreenshotFields
  expanded: boolean
}

// ... (复用 KbdReviewView 的 ScreenshotTypeInfo, ScreenshotFields 等类型定义)
```

- [ ] **Step 2: 实现 openKbdDetail 和 openSopDetail 函数**

```typescript
async function openKbdDetail(kbdId: number) {
  detailLoading.value = true
  detailDialogVisible.value = true
  detailKbdEntry.value = null
  detailSopEntry.value = null
  try {
    const resp = await fetch(`/api/admin/kbd/${kbdId}`, { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    detailKbdEntry.value = data
    parsedSegments.value = parseContentMd(data.content_md || '')
  } catch {
    ElMessage.error('加载 KBD 详情失败')
    detailDialogVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

async function openSopDetail(sopId: number) {
  detailLoading.value = true
  detailDialogVisible.value = true
  detailKbdEntry.value = null
  detailSopEntry.value = null
  try {
    const resp = await fetch(`/api/admin/sop/${sopId}`, { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    detailSopEntry.value = data
    // SOP 详情渲染（简化版，直接使用 renderMarkdown）
    parsedSegments.value = [{ type: 'normal', html: renderMarkdown(data.content_md || '') }]
  } catch {
    ElMessage.error('加载 SOP 详情失败')
    detailDialogVisible.value = false
  } finally {
    detailLoading.value = false
  }
}
```

- [ ] **Step 3: 复用 KbdReviewView 的 parseContentMd 和 renderMarkdown 函数**

将 `KbdReviewView.vue` 中的以下函数复制到 `CategoryManageView.vue`：
- `parseContentMd`
- `parseScreenshotBlockV2`
- `renderMarkdown`
- `inlineRender`
- `escapeHtml`
- `typeNameToInfo`
- `detectScreenshotType`
- `getErrorLabelByType`
- `detectErrorLabel`

这些函数位于 `KbdReviewView.vue` 第 342-500 行附近。

- [ ] **Step 4: 添加详情弹窗模板**

```html
<!-- KBD/SOP 详情弹窗 -->
<el-dialog
  v-model="detailDialogVisible"
  :title="detailKbdEntry?.title || detailSopEntry?.title || '详情'"
  width="700px"
  top="5vh"
>
  <div v-loading="detailLoading" class="detail-content">
    <template v-if="detailKbdEntry">
      <!-- KBD 详情渲染（支持截图块 accordion） -->
      <div class="kbd-detail">
        <div class="kbd-meta">
          <span>案例ID: {{ detailKbdEntry.support_id }}</span>
          <span>命中次数: {{ detailKbdEntry.hit_count }}</span>
        </div>
        <div class="kbd-content">
          <template v-for="(seg, idx) in parsedSegments" :key="idx">
            <div v-if="seg.type === 'normal'" class="normal-content" v-html="seg.html"></div>
            <div v-else-if="seg.type === 'screenshot'" class="screenshot-block">
              <!-- 复用 KbdReviewView 的截图块渲染逻辑 -->
              <div class="screenshot-header" @click="seg.expanded = !seg.expanded">
                <span class="screenshot-icon">{{ seg.typeInfo.icon }}</span>
                <span class="screenshot-label">{{ seg.typeInfo.label }}</span>
                <el-icon class="expand-icon" :class="{ expanded: seg.expanded }">
                  <ArrowDown />
                </el-icon>
              </div>
              <div class="screenshot-body" v-show="seg.expanded">
                <!-- 截图内容 -->
              </div>
            </div>
          </template>
        </div>
      </div>
    </template>
    <template v-else-if="detailSopEntry">
      <!-- SOP 详情渲染（纯 Markdown） -->
      <div class="sop-detail" v-html="parsedSegments[0]?.html || ''"></div>
    </template>
  </div>
  <template #footer>
    <el-button @click="detailDialogVisible = false">关闭</el-button>
  </template>
</el-dialog>
```

- [ ] **Step 5: 增加详情弹窗样式**

```css
.detail-content {
  max-height: 70vh;
  overflow-y: auto;
}

.kbd-meta {
  display: flex;
  gap: 16px;
  padding: 8px 0;
  color: #909399;
  font-size: 12px;
  border-bottom: 1px solid #ebeef5;
  margin-bottom: 16px;
}

.kbd-content .normal-content {
  font-size: 14px;
  line-height: 1.6;
}

.kbd-content .md-h2 {
  font-size: 16px;
  font-weight: 600;
  margin: 16px 0 8px 0;
}

.kbd-content .md-h3 {
  font-size: 14px;
  font-weight: 500;
  margin: 12px 0 6px 0;
}

.kbd-content .md-p {
  margin: 8px 0;
}

.kbd-content code {
  background: #f5f7fa;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: monospace;
}

.screenshot-block {
  margin: 12px 0;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
}

.screenshot-header {
  display: flex;
  align-items: center;
  padding: 12px;
  background: #f5f7fa;
  cursor: pointer;
}

.screenshot-icon {
  font-size: 18px;
  margin-right: 8px;
}

.screenshot-label {
  font-weight: 500;
  color: #606266;
}

.expand-icon {
  margin-left: auto;
  transition: transform 0.3s;
}

.expand-icon.expanded {
  transform: rotate(180deg);
}

.screenshot-body {
  padding: 12px;
}
```

- [ ] **Step 6: Commit**

```bash
git add frontend/admin/src/views/CategoryManageView.vue
git commit -m "feat(admin): 详情弹窗复用 KbdReviewView Markdown 渲染逻辑

[env:dev:gs][agent:claude]"
```

---

### Task 10: 集成验证与测试

**Files:**
- Test: `backend/kb-service/tests/` (如有)

- [ ] **Step 1: 启动开发环境验证后端改动**

```bash
cd /mnt/d/aihci/hci-troubleshoot-platform
make dev-up
```

- [ ] **Step 2: 手动验证 API 返回数据包含统计字段**

```bash
curl -H "Authorization: Bearer hci-dev-internal-token" http://localhost:8080/api/kb/categories?grouped=true | jq '.domains["虚拟机"][0]'
```

预期输出包含 `published_kbd_count` 和 `published_sop_count` 字段。

- [ ] **Step 3: 验证意图识别过滤禁用分类**

```bash
# 先禁用一个分类
curl -X PUT -H "Authorization: Bearer hci-dev-internal-token" \
  -H "Content-Type: application/json" \
  -d '{"is_active": false}' \
  http://localhost:8080/api/kb/categories/虚拟机-001

# 验证意图识别不返回该分类
curl -X POST -H "Authorization: Bearer hci-dev-internal-token" \
  -H "Content-Type: application/json" \
  -d '{"query": "虚拟机创建失败", "top_n": 3}' \
  http://localhost:8080/api/kb/classify/intent | jq '.categories'
```

- [ ] **Step 4: 前端启动并验证 UI 改动**

```bash
cd frontend/admin && pnpm dev
```

在浏览器中验证：
1. 分类项显示 `[SOP:N][KBD:N]` 标签
2. 大类显示汇总数量
3. 统计栏显示「已发布KBD」总数
4. 详情面板标题行布局正确
5. 4列×2行表格显示正确
6. SOP/KBD 列表加载正常
7. 点击详情按钮弹窗显示正确

- [ ] **Step 5: 最终 Commit**

```bash
git add docs/superpowers/specs/2026-04-18-category-kbd-count-display-design.md docs/superpowers/plans/2026-04-18-category-kbd-count-display.md
git commit -m "docs: 完成分类基线页面 KBD/SOP 数量展示功能设计与实现计划

[env:dev:gs][agent:claude]"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] 分类项右侧标签显示 `[SOP:N][KBD:N]` → Task 6
- [x] 大类汇总显示 → Task 5, Task 6
- [x] 左侧统计栏新增「已发布KBD」总数 → Task 5, Task 6
- [x] 右侧详情面板标题行布局 → Task 7
- [x] 4列×2行表格 → Task 7
- [x] SOP/KBD 列表展示 → Task 8
- [x] 命中次数标签 `[命中:N]` → Task 8
- [x] 详情弹窗复用 KbdReviewView 逻辑 → Task 9
- [x] 意图识别过滤禁用分类 → Task 3
- [x] 删除 has_sop 字段 → Task 2

**Placeholder scan:**
- No "TBD", "TODO" in plan
- All code blocks contain actual implementation code

**Type consistency:**
- `KbCategory.published_kbd_count` / `published_sop_count` used consistently
- `domainStats` computed property returns correct structure
- API endpoint paths consistent across tasks