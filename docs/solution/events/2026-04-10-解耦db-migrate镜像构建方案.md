---
status: active
category: solution
audience: developer
last_updated: 2026-04-10
owner: team
---

# 解耦 db-migrate 镜像构建（方案 A1）

## 背景与需求

### 问题发现过程

PR #134 合并后，用户在 staging 执行 ArgoCD sync 时遇到 `imagePullBackOff`（db-migrate hook 拉取新镜像失败），
触发对 db-migrate 构建机制的审查。

### 根本问题

`database/desired_schema.sql` 和 `desired_extras.sql` 被 `COPY` 进 Docker 镜像（`Dockerfile.migrations`）。
这导致：
- **所有 push 到 main 的 PR** 都会重建 db-migrate 镜像（ci.yml `build-and-push` matrix 无路径过滤）
- env-repo-sync 更新 `dbMigrate.tag` → ArgoCD 检测到 tag 变化 → 触发 PreSync Hook
- **即使没有任何 schema 变更**，每次发布都要额外等待 db-migrate Job（65~105s）

### 量化影响

完整时序分析（一次 push → 应用上线）：

| 阶段 | 耗时 |
|------|------|
| 测试（lint/unit/integration/security 并行） | 5~8 min |
| prepare + build-and-push（8 并行） | ~5 min |
| auto-deploy（dev+staging 串行 sleep 30×2） | ~90s |
| ArgoCD polling → 开始 sync | ~30s |
| **PreSync Hook：db-migrate Job（无 schema 变更）** | **65~105s（新 tag 需拉取）** |
| 应用 rolling deploy | ~90s |
| **总计** | **~14~16 min** |

当 db-migrate tag 不变（节点缓存命中）时，PreSync Job 降为 40~55s，节省 25~60s/次。

---

## 方案比较

### 方案 A（选定）：db-migrate 从 matrix 剥离 + 路径过滤

将 `db-migrate` 从 `build-and-push` matrix 移出，独立创建 `build-db-migrate` job，
仅在以下路径变更时触发：

```
database/desired_schema.sql
database/desired_extras.sql
Dockerfile.migrations
scripts/db-migrate.sh
```

env-repo-sync 增加可选参数 `db_migrate_tag`，有值时才更新 `dbMigrate.tag`，无值跳过。

#### 方案 A1 vs A2 选择

| 维度 | A1（env-repo-sync 加可选参数） | A2（db-migrate 独立 sync workflow） |
|------|------|------|
| 改动范围 | ci.yml + env-repo-sync.yml | ci.yml + 新 workflow |
| env repo git 冲突风险 | 无（单次 push） | 有（两个 workflow 并发 push） |
| DB 变更场景额外耗时 | 0 | ~30s（需串行等待避免冲突） |
| 实现复杂度 | 低 | 中 |

**结论：A1 更优。**

### 方案 B：Schema 改为 Helm ConfigMap（架构最优，但风险高）

把 SQL 文件从镜像移出，改为 Helm `.Files.Get` 引用（SQL 文件需移入 Chart 目录）。
db-migrate 镜像只含工具（atlas + psql），极少重建。

**为什么不选 B：**
1. 旧方案（dbmate + ConfigMap）已有前车之鉴：`sync-db-migrations.sh` 漏跑导致 ConfigMap 不更新。
   方案 B 能解决此问题的前提是 SQL 文件必须成为 Helm Chart 的一部分（`.Files.Get`），
   但这要求改变文件布局（`database/` → `deploy/helm/.../files/`），引入新约定。
2. 当前规模（2 个 SQL 文件）方案 A 的收益已经足够，引入方案 B 的复杂度不值得。

---

## 实现方案（A1）

### 变更文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `.github/workflows/ci.yml` | 修改 | 从 matrix 移除 db-migrate；新增 `build-db-migrate` job（路径触发）；`auto-deploy` 传递 `db_migrate_tag` |
| `.github/workflows/env-repo-sync.yml`（env repo） | 修改 | 新增可选输入 `db_migrate_tag`；有值时单独替换 `dbMigrate.tag` |
| `hci-platform-env` | 跨仓库 | env-repo-sync.yml 在 env repo 中，需单独 PR |

### ci.yml 改动逻辑

```
# 原来：build-and-push matrix 包含 db-migrate（无路径过滤，每次 main push 触发）
# 改后：

build-db-migrate:
  # 仅在 DB 相关文件变更时触发
  if: |
    contains(github.event.head_commit.modified, 'database/desired_schema.sql') ||
    contains(github.event.head_commit.modified, 'database/desired_extras.sql') ||
    ...
  # 构建 db-migrate 镜像，输出 db_migrate_tag

auto-deploy-non-prod:
  needs: [prepare, build-and-push, build-db-migrate]
  # db_migrate_tag：有值（本次有 DB 变更）则传给 env-repo-sync，无值则传空字符串
```

注意：GitHub Actions 的 `paths` filter 只能在 workflow `on` 级别使用，不能在 job 级别使用。
实现路径过滤需要用 `dorny/paths-filter` action 或检测 `git diff` 来判断。

### env-repo-sync.yml 改动逻辑

```yaml
inputs:
  db_migrate_tag:
    description: 'db-migrate 镜像标签（可选，空值则跳过更新）'
    required: false
    default: ''

# 更新应用服务 tag（原逻辑不变）
sed -i 's/^\(\s*tag:\s*\)"[^"]*"/\1"'"${IMAGE_TAG}"'"/' "$VALUES_FILE"

# 单独更新 db-migrate tag（仅当 db_migrate_tag 非空）
if [ -n "$DB_MIGRATE_TAG" ]; then
  sed -i '/dbMigrate:/,/tag:/ s/^\(\s*tag:\s*\)"\([^"]*\)"/\1"'"${DB_MIGRATE_TAG}"'"/' "$VALUES_FILE"
fi
```

---

## 风险与缓解

| 风险 | 说明 | 缓解 |
|------|------|------|
| paths-filter 误判漏构建 | DB 文件改了但 filter 未触发 | workflow_dispatch 保留手动触发 |
| env repo sed 误覆盖 db-migrate tag | sed 范围匹配错误 | 单独用 python/awk 替换更精确，或用 yq |
| 首次 PR 无 db-migrate tag | 历史镜像 tag 不更新 | 仅影响新增路径过滤后的首次 DB 变更之前，现有 tag 保持不变 |

---

## 验收标准

- [ ] 纯应用改动 PR：合并后 db-migrate tag 不变，ArgoCD 不触发 db-migrate Job
- [ ] 含 DB 改动 PR（修改 `desired_schema.sql`）：合并后 db-migrate tag 更新，Job 正常执行
- [ ] `workflow_dispatch` 手动触发：可强制构建并传递 db-migrate tag
- [ ] env repo values.yaml 应用服务 tag 和 db-migrate tag 独立更新，互不影响
