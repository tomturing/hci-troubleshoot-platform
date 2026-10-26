---
status: active
category: solution
audience: developer
last_updated: 2026-10-26
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
- env-repo-sync 更新 `dbMigrate.tag`。**注意：这一步本身不会触发 PreSync Hook**——hook 资源不参与 ArgoCD 漂移比对（实测 `status.resources` 中 hook 项 `status=null`），真正触发 Hook 的是同批次应用镜像（Deployment）变化导致的 sync。详见文末「2026-10-26 订正记录」。
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

- [ ] 纯应用改动 PR：合并后 db-migrate tag 不变，镜像不重建；PreSync db-migrate Job **仍会执行**（由 workload 变化触发 sync），但走节点缓存，耗时从 65~105s 降到 40~55s
- [ ] 含 DB 改动 PR（修改 `desired_schema.sql`）：合并后 db-migrate tag 更新；**Job 不会因此自动执行**，须确认同批次有 workload 变化触发 sync，否则需显式触发（见文末订正记录）
- [ ] `workflow_dispatch` 手动触发：可强制构建并传递 db-migrate tag
- [ ] env repo values.yaml 应用服务 tag 和 db-migrate tag 独立更新，互不影响

---

## 补充：prod 环境纳入 db-migrate 自动同步（2026-08-13）

### 问题

原 `auto-deploy-non-prod` job 的 `ENVIRONMENTS` 循环仅覆盖 `dev` / `staging`，
`promote_target` 的 `both` 选项也只包含 `dev staging`。这导致：

- dev/staging 的 `dbMigrate.tag` 随 schema 变更（PR #747 机制）自动前进；
- **prod 的 `dbMigrate.tag` 永远不会被 CI 自动更新**，长期停滞在首次手动设定的旧镜像。

后果：若 prod 环境重建或首次部署，db-migrate 镜像停留在旧版本，
`024` 及之后的迁移不会执行（见 KBD 23821 修复后复盘）。

### 修复（ci.yml）

1. `promote_target` 选项新增 `prod` 与 `all`；
2. `case` 分支新增：
   - `prod) ENVIRONMENTS=(prod) ;;`
   - `all) ENVIRONMENTS=(dev staging prod) ;;`
3. main push 自动晋级仍保持 `dev → staging`（生产不自动晋级，符合受控原则）；
4. prod 仅在 `workflow_dispatch` 显式选择 `prod` / `all` 时受控晋级，
   并由既有 `verify_values_file` 校验 `dbMigrate.tag` 已正确写入。

### 设计原则

- **生产受控**：prod 不随 main push 自动晋级，必须经人工 `workflow_dispatch` 触发；
- **同步一致**：`SKIP_DB_MIGRATE=false` 时，所有目标环境（含 prod）的 `dbMigrate.tag`
  与 `migration_history` 强一致，避免环境间迁移版本漂移；
  > ⚠️ 2026-10-26 订正：**tag 一致 ≠ 迁移已执行**。tag 只决定 Hook 用哪个镜像，迁移是否真正跑过取决于 Hook 是否被触发。仅改 tag 不会让 ArgoCD 变 OutOfSync，迁移会静默空转。核对口径应以目标库 `migration_history` 实读为准。
- **幂等安全**：db-migrate Job 对已执行迁移自动跳过，`UPDATE...WHERE` 类语句幂等，重跑安全。

### 验收标准（新增）

- [ ] `workflow_dispatch` 选 `prod`：仅 prod 的 `dbMigrate.tag` 更新并 push env repo；
- [ ] `workflow_dispatch` 选 `all`：dev/staging/prod 三套 `dbMigrate.tag` 全部更新；
- [ ] main push 自动晋级仍只覆盖 `dev` / `staging`，prod 不受影响；
- [ ] prod 的 `migration_history` 在新迁移合并后能随受控发布自动补齐。

---

## 2026-10-26 订正记录：「tag 变化 → 触发 PreSync」假设被证伪

> 本文是 2026-04-10 的设计事件，**决策脉络保留原样**；本节只订正其中与实测不符的事实断言。
> 触发订正的场景：`assistant_evaluation` 表下线后，镜像 tag 已更新但实库表仍在，反向暴露了这条假设。

### 被证伪的原断言

| 位置 | 原断言 | 实测结果 |
|---|---|---|
| 「根本问题」第 2 条 | env-repo-sync 更新 `dbMigrate.tag` → ArgoCD 检测到 tag 变化 → 触发 PreSync Hook | **假**。只改 tag 时 app 仍为 `Synced`，Hook 未执行 |
| 验收标准第 1 条 | 纯应用改动 PR：ArgoCD 不触发 db-migrate Job | **假**。workload 变化触发 sync，Hook 照常执行，只是镜像命中缓存 |
| 验收标准第 2 条 | 含 DB 改动 PR：Job 正常执行 | **假**。tag 更新属实，Job 不随之执行 |
| 「补充」设计原则 | `dbMigrate.tag` 与 `migration_history` 强一致 | **假**。tag 一致只保证 Hook 用哪个镜像，不保证 Hook 跑过 |

### 实测证据（2026-10-26，staging）

1. **hook 资源不参与漂移比对——这是根因。**
   `kubectl -n argocd get application hci-platform-staging -o json` 的 `status.resources` 中，凡 `hook: true` 的项（`Job/db-migrate-*`、`Job/hci-sim-db-migrate-*`、`Secret/hci-sim-db-credentials`）其 `status` 字段均为 `null`；而非 hook 的 `Secret/hci-secrets`、`Deployment/*` 均为 `Synced`。
   `status=null` 即「该资源不参与 desired vs live 比较」——因此 **改 hook 的镜像 tag 永远不会让 app 变 OutOfSync**。
2. **只改 tag 的那次发布确实空转。**
   env 仓提交 `2ae8252` 只改了一行 `dbMigrate.image.tag`（`20261024-1715-e3c4836` → `20261026-0222-c2a9c72`）；app 保持 `Synced`，未产生 sync，PreSync Hook 未重跑；实库 `to_regclass('public.assistant_evaluation')` 仍非空。
3. **对照组确认触发条件是 workload。**
   同批次另一个提交 `638bdcc` 改的是 api-gateway（真实 workload），它才触发了 02:20 的那次 sync。
4. **显式触发后才真正执行。**
   `kubectl -n argocd patch application hci-platform-staging --type merge -p '{"operation":{"initiatedBy":{"username":"qoder-schema-sync"},"sync":{}}}'` 之后，Job `db-migrate-20261026-0222-c-010` 日志出现 `DROP TABLE "public"."assistant_evaluation";`，实库复核 `to_regclass` 为 NULL、`idx_eval%` 索引 0 条。

### 正确的心智模型

```
ArgoCD sync 的触发源 = 非 hook 资源（Deployment/Service/ConfigMap/Secret…）的 desired ≠ live
                      ↓ 产生 sync
PreSync Hook 按 hook-weight 顺序执行（db-migrate Job 在此）
                      ↓
镜像 tag 决定「用哪个迁移镜像」，但决定「跑不跑」的是上游有没有 sync
```

两个反直觉的推论：

- **改 tag 不等于迁移生效**：必须同一批次里有 workload 变化，或人工显式触发 sync。
- **不改 tag 也会跑迁移**：任何一次正常发布都会执行 PreSync db-migrate Job（已执行迁移幂等跳过）。本方案 A 的收益是「镜像命中缓存 → 40~55s」，**不是「不跑 Job」**。

### 运维动作（补丁，不改变原方案）

`dbMigrate.tag` 更新后必须补一步确认，否则迁移静默空转：

```bash
# 1. 看 app 是否因 tag 变化而 OutOfSync（预期结果：仍是 Synced，说明没有触发源）
kubectl -n argocd get application <app> -o jsonpath='{.status.sync.status}{"\n"}'
# 2. 若无 workload 变化，显式触发一次 sync
kubectl -n argocd patch application <app> --type merge \
  -p '{"operation":{"initiatedBy":{"username":"<操作人>"},"sync":{}}}'
# 3. 以目标库实读为准，不以 tag 为准
psql "$DSN" -c "select version, executed_at from migration_history order by executed_at desc limit 3"
```

> 关联教训已沉淀为记忆：`只升 db-migrate 镜像 tag 不会触发 PreSync，迁移静默空转`。
> 同批次还暴露了另一个独立缺陷：PreSync hook 混用 `sync-wave` 与 `hook-weight` 会导致依赖的 Secret 尚未创建（见 `hci-sim-db-credentials` 的 `CreateContainerConfigError`）。
