---
status: in-progress
category: verify
audience: dba, developer, operator, reviewer, release
last_updated: 2026-08-13
owner: team
---

# 主库 agent_test_* 僵尸表清理验证

对应需求：hci_sim 独立数据库隔离（见 `2026-08-10-hci_sim独立数据库隔离需求.md`）
对应方案：hci_sim 独立数据库隔离（见 `2026-08-10-hci_sim独立数据库隔离方案.md`）
关联验证：本清理是 `2026-08-10-hci_sim独立数据库隔离验证.md` 的 **contract/收尾阶段**，补齐迁移遗留的主库 schema 残留。

## 1. 第一性原理：为什么主库还有 agent_test_* 表

hci-sim 已通过 `database/hci-sim-migrations/000001_control_plane.sql` 在**独立库**以带 schema 前缀（control_plane/fixture/artifact/audit）的新命名建表，后端（`backend/`）对所有 `agent_test_*` 表 **grep 零引用**（已验证）。主库中 `agent_test_*` 仅由以下两处遗留定义支撑：

- `database/atlas-migrations/20260806000000_*.sql` 与 `20260806000001_*.sql`（CREATE TABLE）
- `database/desired_schema.sql`（声明式期望 schema，Atlas `schema apply` 的唯一权威来源）

由于 `scripts/db-migrate.sh` 与 CI `db-migration-test.yml` 实际只执行 `atlas schema apply --to desired_schema.sql`，**从未调用 `atlas migrate apply`**，因此 `atlas-migrations/` 中的创建迁移在该架构下并未真正执行；主库 `agent_test_*` 表是历史基线/人工 apply 的副产物。它们与业务代码、与其他主库业务表**无任何依赖关系**（仅彼此间 `ON DELETE RESTRICT` 自引用）。

**结论**：主库 `agent_test_*` 是 hci-sim 迁移完成后未清理的孤儿 schema（zombie schema），删除零业务风险。

## 2. 对抗性审查：清理范围与风险

### 2.1 清理范围（最终确认）
主库 `agent_test_*` 全族共 15 张表（含此前在 #746 中对齐 CHECK 的 8 张及其附属 7 张）：

```
agent_test_scenario, agent_test_artifact, agent_test_artifact_scan,
agent_test_artifact_approval, agent_test_fixture_bundle,
agent_test_fixture_dependency, agent_test_fixture_provenance,
agent_test_fixture_approval, agent_test_fixture_audit,
agent_test_fixture_stale_outbox, agent_test_run, agent_test_run_attempt,
agent_test_run_event, agent_test_run_result, agent_test_runtime_instance
```

### 2.2 风险与对策
| 风险 | 评估 | 对策 |
|---|---|---|
| 误删业务表 | 无：backend 零引用，独立库已有等价表 | 删除前 grep 确认 `backend/` 0 命中 |
| 外键 `ON DELETE RESTRICT` 阻断 DROP | 中：表间自引用 | DROP 用 `CASCADE` 规避顺序依赖 |
| 波及其他业务表 | 无：无外键指向 `agent_test_*` | CASCADE 仅清理该族内部依赖 |
| 删除后 Atlas 重建 | 高（若 `desired_schema.sql` 不删） | 同步从 `desired_schema.sql` 删除全段定义 |
| 生产 `atlas.sum` 完整性失败 | 低：本架构不调用 `atlas migrate`，不读 sum | 已确认 `db-migrate.sh`/CI 仅用 `schema apply` |
| 各环境数据差异 | 低：均为空/弃用孤儿表 | 删除为 DROP，幂等 `IF EXISTS` |

### 2.3 多环境差异根因（用户原始疑问）
「当前 dev 有 agent_test_*，其他 dev/staging 没有」的根因：主库 `agent_test_*` 由 Atlas 迁移/基线决定，而非业务必需。Atlas Job 此前长期因 `atlasgo.sh` 下架（已在 #746 修复）而失败/跳过，导致各环境是否执行过 `20260806` 这批迁移不一致——有表的环境是曾经成功 apply 过或人工建过，无表的环境从未执行。这反向证明：`agent_test_*` 不是系统真正需要的主库资产，清理后全环境一致（均无）。

## 3. 改动内容

### 3.1 `database/desired_schema.sql`
删除 `agent_test_*` 全段定义（约 line 1603–1830，含表、索引、COMMENT）。删除后 grep `agent_test_` 残留为 0。Atlas `schema apply` 将 diff 出 `DROP TABLE` 并在各环境执行，使主库收敛到真实业务表。

### 3.2 `database/atlas-migrations/20260813000000_drop_orphan_agent_test_tables.sql`（新增）
幂等 `DROP TABLE IF EXISTS ... CASCADE`，列出全部 15 张表。在本架构下作为变更审计记录（CI/生产均不执行 `atlas migrate`，但保留以满足可追溯性）。

### 3.3 `.github/workflows/db-migration-test.yml` Step 3「Schema 完整性检查」同步调整
删除主库 `agent_test_*` 后，Step 3 的 `EXPECTED_TABLES` 仍把 15 张 `agent_test_*` 列为必存在表，会导致 CI 报「15 项异常」直接失败。按第一性原理：这些表已迁出主库（等价定义在独立库 `hci-sim-migrations` 中以 `control_plane/fixture/artifact/audit` schema 存在），主库不再需要它们，**不应**再作为主库期望表。

调整内容：
- 从 `EXPECTED_TABLES` 移除 15 张 `agent_test_*`。
- 将 15 张 `agent_test_*` 加入 `DEPRECATED_TABLES`，使 CI 反向验证「主库不再存在」——与 `kb_chunk`/`raw_cases` 等废弃表同口径。

此改动使 PR #747 的「删表 + 校验收敛」形成闭环，CI 才能通过。

### 3.4 `.github/workflows/ci.yml` paths-filter 加固（前端检查误触发根治）

**问题现象**：PR #747 仅改动 `database/**` + `docs/**` + `.github/workflows/db-migration-test.yml`（GitHub 侧 `gh pr view 747 --json files` 确认无任何 `frontend/**` 文件），但 `CI / 前端检查（单元测试 + 构建）` 仍 `Successful`（真跑了 `pnpm install + test + build`，耗时约 55s）。

**第一性原理根因**：`frontend-check` job 的触发条件为 `needs.changes.outputs.frontend == 'true'`（ci.yml 第 216 行），而 `changes` job 的 `paths-filter` 中 `frontend` 过滤器（第 62-66 行）字面只匹配 `frontend/**`，不含 `database/**`/`docs/**`。PR diff 无 `frontend/**` 文件时该输出应为 `false`，实测却为 `true`——属 `paths-filter` 输出误判（glob 边界模糊或 outputs 串扰），非预期行为。

**对抗性审查修复**（方案 C，从定义层 + 使用层双加固）：

> **前两轮修正（均被推翻）**：
> 1. 第一轮在 `frontend-check` 内用 `dorny/paths-filter` 二次校验——同源 action 同 bug，仍误触发。
> 2. 第二轮在 `frontend-check` 内用 `git diff` + `grep` + `exit 0`——实测 CI 日志确认短路 step 正确输出「未检测到 frontend/ 改动，跳过前端检查」并执行了 `exit 0`，但**后续 step（setup-node/pnpm/test/build）照常跑了 58s**。根因：GitHub Actions 的 step 是独立进程，`run:` 内的 `exit 0` 仅结束当前 step，**不终止整个 job**。这是混淆「shell 退出」与「job 短路」语义的对抗性审查盲区。

**第一性原理正确修复**（第三轮，已落地）：触发权的唯一正确位置是 **job 级 `if`**（不是 step 内 `exit 0`）。

1. **定义层（纵深防御，保留）**：`frontend` 过滤器显式追加负向约束（`!backend/**`、`!database/**`、`!docs/**`、`!deploy/**`、`!scripts/**`、`!.github/**`）。
2. **使用层（根除，上移到 job 级 `if`）**：在 `changes` job（job 名 `docs-governance`）中 `dorny/paths-filter` 之后新增 `确定性前端改动判定` step（`id: fe-real`），用原生 `git diff --name-only ${base} ${head}` 列出 PR 真实改动文件并 `grep -qE '^frontend/'`，将结果写入 `GITHUB_OUTPUT`（true/false）。`changes` job 的 `outputs.frontend` 改为取 `steps.fe-real.outputs.frontend`。`frontend-check` 的 job 级 `if` 已依赖 `needs.changes.outputs.frontend`，当该值为 `false` 时**整个 job 被 `if` 跳过（显示 `Skipped`）**，彻底不再空跑。
   - fork/异常场景（无 base/head sha）fallback 到 `dorny/paths-filter` 结果（宁可多跑一次前端检查，也不漏跑真前端改动）。

**验收**：后续纯 `database/**` / `docs/**` PR 的 `前端检查` 应表现为 **`Skipped`**（job 级 `if` 跳过），而非 `Successful` 或空跑约 49s；真实改了 `frontend/` 的 PR 仍应 `Successful`（不矫枉过正）。

## 4. 验证

- `desired_schema.sql` 删除后 `agent_test_` 残留计数 = 0（已确认）。
- 新建 DROP 迁移语法经 `pg_dump` 风格校验（CASCADE 规避外键）。
- 端到端 Atlas `schema apply` + `schema diff` 幂等验证因本地 Postgres 的 `vector` 扩展 search_path 环境差异无法完整跑通（与本次改动无关，`kb_category.embedding vector(1536)` 在任何基于 `desired_schema.sql` 的 apply 均触发，属既有的本地环境问题；CI 的 pgvector 镜像可正常处理）。本改动为纯减法，不引入新 schema 冲突，风险经静态分析收敛。
- 真实生效以 CI（pgvector 环境）的 `Atlas 声明式 Schema 验证` 为准。

## 5. 运维动作（部署后）

- 各环境 Atlas `schema apply` 会自动 DROP 主库 `agent_test_*`。建议部署后抽查：`SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'agent_test_%'` 应为 0。
- 若某环境因历史外键琐事 DROP 失败，单独执行 `DROP TABLE agent_test_* CASCADE` 兜底。
- 后续 hci-sim 相关变更应只走独立库 `hci-sim-migrations`，不再触碰主库 `desired_schema.sql`。
