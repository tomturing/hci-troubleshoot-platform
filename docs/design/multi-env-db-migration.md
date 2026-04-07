# 多环境数据库迁移自动化方案

| 项 | 值 |
|---|---|
| 日期 | 2026-04-07 |
| 状态 | 草案 |
| 关联 | PIT-039（新编号） |

---

## 1. 现状概述

### 1.1 双仓库 GitOps 架构

```
┌─────────────────────────────┐     ┌──────────────────────────┐
│ hci-troubleshoot-platform   │     │ hci-platform-env         │
│ （应用仓库）                │     │ （环境仓库）             │
│                             │     │                          │
│ deploy/helm/hci-platform/   │     │ environments/            │
│  ├── templates/hooks/       │     │  ├── dev/values.yaml     │
│  │   ├── db-migrate-job     │     │  ├── staging/values.yaml │
│  │   ├── db-migrations-cm   │     │  └── prod/values.yaml    │
│  │   └── smoke-test-job     │     │                          │
│  └── values.yaml（默认值）  │     │                          │
└─────────────────────────────┘     └──────────────────────────┘
         │                                     │
         └─────── ArgoCD Application ──────────┘
                  (Helm + values overlay)
```

### 1.2 当前迁移机制

| 环境 | `dbMigrate.enabled` | 镜像标签 | ArgoCD syncPolicy | 迁移自动化 |
|------|---------------------|----------|-------------------|-----------|
| dev | `true`（默认值） | `20260407-0120-5d807f4` | auto-sync | ✅ 全自动 |
| staging | `false` | `20260330-1037-4db398a` | 手动 sync | ❌ 未启用 |
| prod | `false` | `20260325-1611-6a0d235` | 手动 sync | ❌ 未启用 |

### 1.3 问题诊断

本次 dev 环境发现的根因：**schema_migrations 表记录了 8 次迁移已执行，但实际 DDL 未生效**。推测 DB 从早期备份恢复导致漂移。

staging/prod 环境：
- `dbMigrate.enabled: false`，注释说"待 baseline 执行完毕后改为 true"
- 但 baseline 尚未执行，意味着 staging/prod 的 DB schema 可能也存在类似漂移

---

## 2. 方案设计

### 2.1 核心原则

1. **幂等迁移**：所有 DDL 使用 `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`，任何环境都能安全执行
2. **先修后启**：staging/prod 先手动执行修复迁移，再启用 `dbMigrate.enabled: true`
3. **渐进推进**：dev → staging → prod 逐环境验证
4. **可观测**：每次迁移执行后自动输出验证 SQL，结果写入 Pod 日志

### 2.2 分环境操作步骤

#### Phase 1: Dev 环境（已完成 ✅）

```bash
# 直接在 postgres-0 执行修复 SQL
kubectl cp database/20260407001_schema_repair.sql hci-dev/postgres-0:/tmp/
kubectl exec postgres-0 -n hci-dev -- psql -U hci_admin -d hci_troubleshoot -f /tmp/schema_repair.sql
# 注册版本
kubectl exec postgres-0 -n hci-dev -- psql -U hci_admin -d hci_troubleshoot \
  -c "INSERT INTO schema_migrations (version) VALUES ('20260407001') ON CONFLICT DO NOTHING;"
```

结果：23 张表，case-service 创建工单返回 HTTP 201 ✅

#### Phase 2: Staging 环境

**前提**：staging K8s 集群可达且 postgres StatefulSet 正在运行。

```bash
# 步骤 1：诊断当前 staging DB 状态
kubectl exec postgres-0 -n hci-staging -- psql -U hci_admin -d hci_troubleshoot -c "\dt+"
kubectl exec postgres-0 -n hci-staging -- psql -U hci_admin -d hci_troubleshoot \
  -c "SELECT version FROM schema_migrations ORDER BY version;"

# 步骤 2：复制并执行修复迁移（幂等，无论当前状态如何都安全）
kubectl cp database/20260407001_schema_repair.sql hci-staging/postgres-0:/tmp/
kubectl exec postgres-0 -n hci-staging -- psql -U hci_admin -d hci_troubleshoot \
  -f /tmp/schema_repair.sql

# 步骤 3：注册所有迁移版本（让 dbmate 认为过去的迁移都已执行）
kubectl exec postgres-0 -n hci-staging -- psql -U hci_admin -d hci_troubleshoot -c "
INSERT INTO schema_migrations (version) VALUES
    ('20260305001'),('20260312001'),('20260312002'),
    ('20260326001'),('20260326002'),('20260326003'),
    ('20260401001'),('20260407001')
ON CONFLICT DO NOTHING;"

# 步骤 4：启用 dbMigrate
# 修改 hci-platform-env/environments/staging/values.yaml:
#   dbMigrate:
#     enabled: true
# 提交并推送到环境仓库

# 步骤 5：触发 ArgoCD sync（或等待自动同步）
# 验证 db-migrate Job 执行成功（应为 no-op，因为所有版本已注册）
kubectl logs job/db-migrate-<revision> -n hci-staging

# 步骤 6：验证
kubectl exec -n hci-staging deploy/case-service -- python3 -c "
import urllib.request, json
req = urllib.request.Request('http://localhost:8001/api/cases/',
    data=json.dumps({'title':'staging迁移验证','description':'test','assistant_type':'openclaw','client_id':'staging-verify'}).encode(),
    headers={'Content-Type':'application/json'}, method='POST')
resp = urllib.request.urlopen(req)
print(f'HTTP {resp.status}: {json.loads(resp.read())[\"case_id\"]}')
"
```

#### Phase 3: Prod 环境

与 staging 步骤相同，但需额外注意：

1. **执行前备份**：`pg_dump -U hci_admin -d hci_troubleshoot > /tmp/prod_backup_$(date +%Y%m%d).sql`
2. **维护窗口**：建议在业务低峰期执行
3. **人工审批**：修改 `dbMigrate.enabled: true` 必须通过 PR + 审批
4. **回滚计划**：如出问题，恢复 `dbMigrate.enabled: false` 并回滚 DB 备份

### 2.3 自动化脚本

建议新增 `scripts/ops/db-repair-env.sh`：

```bash
#!/usr/bin/env bash
# 用法: ./scripts/ops/db-repair-env.sh <namespace>
# 示例: ./scripts/ops/db-repair-env.sh hci-staging

set -euo pipefail

NS="${1:?用法: $0 <namespace>  示例: hci-dev / hci-staging / hci-prod}"

echo "=== 目标: ${NS} ==="

# 1. 诊断
echo ">>> 当前表列表"
kubectl exec postgres-0 -n "${NS}" -- psql -U hci_admin -d hci_troubleshoot -c "\dt+" 2>/dev/null

echo ">>> 当前迁移记录"
kubectl exec postgres-0 -n "${NS}" -- psql -U hci_admin -d hci_troubleshoot \
  -c "SELECT version FROM schema_migrations ORDER BY version;" 2>/dev/null

# 2. 执行修复
echo ">>> 复制修复 SQL"
kubectl cp database/20260407001_schema_repair.sql "${NS}/postgres-0:/tmp/schema_repair.sql"

echo ">>> 执行修复迁移"
kubectl exec postgres-0 -n "${NS}" -- psql -U hci_admin -d hci_troubleshoot \
  -f /tmp/schema_repair.sql

# 3. 注册版本
echo ">>> 注册迁移版本"
kubectl exec postgres-0 -n "${NS}" -- psql -U hci_admin -d hci_troubleshoot -c "
INSERT INTO schema_migrations (version) VALUES
    ('20260305001'),('20260312001'),('20260312002'),
    ('20260326001'),('20260326002'),('20260326003'),
    ('20260401001'),('20260407001')
ON CONFLICT DO NOTHING;"

# 4. 验证
echo ">>> 验证结果"
kubectl exec postgres-0 -n "${NS}" -- psql -U hci_admin -d hci_troubleshoot -c "
SELECT COUNT(*) AS table_count
FROM information_schema.tables
WHERE table_schema='public' AND table_type='BASE TABLE';"

echo ">>> 迁移版本"
kubectl exec postgres-0 -n "${NS}" -- psql -U hci_admin -d hci_troubleshoot \
  -c "SELECT version FROM schema_migrations ORDER BY version;"

echo "=== ${NS} 修复完成 ==="
```

---

## 3. 后续自动化保障机制

### 3.1 CI Schema 一致性检查（推荐）

在 `.github/workflows/ci.yml` 中新增 `schema-consistency` job：

```yaml
schema-consistency:
  runs-on: ubuntu-latest
  services:
    postgres:
      image: pgvector/pgvector:pg15
      env:
        POSTGRES_USER: hci_admin
        POSTGRES_DB: hci_troubleshoot
        POSTGRES_PASSWORD: test
      ports: ["5432:5432"]
  steps:
    - uses: actions/checkout@v4
    - name: Install dbmate
      run: curl -fsSL -o /usr/local/bin/dbmate https://github.com/amacneil/dbmate/releases/latest/download/dbmate-linux-amd64 && chmod +x /usr/local/bin/dbmate
    - name: Run all migrations
      env:
        DATABASE_URL: postgres://hci_admin:test@localhost:5432/hci_troubleshoot?sslmode=disable
      run: |
        # 从 ConfigMap 模板提取 SQL，去掉 Helm 模板语法
        python3 scripts/ci/extract-migrations.py \
          deploy/helm/hci-platform/templates/hooks/db-migrations-configmap.yaml \
          /tmp/migrations/
        dbmate --no-dump-schema --migrations-dir /tmp/migrations/ up
    - name: Compare ORM vs DB schema
      run: |
        # 通过 SQLAlchemy metadata reflection 对比 ORM 定义与实际 DB
        cd backend && uv run python -m scripts.ci.schema_check
```

### 3.2 迁移文件命名规范

```
<YYYYMMDD><序号3位>_<描述>.sql
例: 20260407001_schema_repair.sql
```

- 日期部分 `YYYYMMDD` 确保全局排序
- 序号 `001-999` 允许同一天多个迁移
- dbmate 按文件名字典序执行

### 3.3 ArgoCD Sync 防护

当前配置已具备保护层：

| 层 | 机制 | 作用 |
|----|------|------|
| PreSync Hook | db-migrate Job | 迁移失败 → Job failed → Sync 中止 |
| PostSync Hook | smoke-test Job | 服务不健康 → Sync 状态 Degraded |
| backoffLimit: 0 | 不重试 | 避免部分执行状态 |
| ttlSecondsAfterFinished: 3600 | 日志保留 1h | 事后排查 |

### 3.4 双迁移系统对齐矩阵

| dbmate 版本 | Alembic 版本 | 内容 |
|------------|-------------|------|
| 20260305001 | 0001 (no-op) | 基础 schema |
| 20260312001 | — | KB v3 迁移 |
| 20260312002 | — | 评分评价体系 |
| 20260326001 | 0002 (部分) | P4 raw_cases / knowledge_atoms |
| 20260326002 | 0003 (部分) | conversation 诊断字段 |
| 20260326003 | — | tool_audit_log |
| 20260401001 | — | KBD 管道 |
| **20260407001** | **0005** | **Schema 修复（本次）** |

> 长期建议：收敛到单一迁移工具。推荐保留 dbmate（K8s 原生，Go 单二进制，无 Python 依赖），Alembic 仅用于本地开发快速验证。

---

## 4. 操作检查清单

### staging 环境启用前检查

- [ ] staging K8s 集群 postgres-0 正常运行
- [ ] 执行 `db-repair-env.sh hci-staging` 成功
- [ ] 验证 23 张表存在
- [ ] 修改 `hci-platform-env/environments/staging/values.yaml` → `dbMigrate.enabled: true`
- [ ] 创建 PR 并审批合并
- [ ] ArgoCD sync 后检查 db-migrate Job 日志（应为 no-op）
- [ ] case-service 创建工单返回 201

### prod 环境启用前检查

- [ ] staging 观察窗口 ≥ 72 小时无迁移相关问题
- [ ] prod pg_dump 备份已完成
- [ ] 维护窗口已通知
- [ ] 执行 `db-repair-env.sh hci-prod` 成功
- [ ] 修改 `dbMigrate.enabled: true`
- [ ] PR 审批合并
- [ ] ArgoCD sync + smoke-test 通过
- [ ] case-service + conversation-service 端到端验证
