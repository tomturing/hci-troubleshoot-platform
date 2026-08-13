# kb-service Catalog 基线持久化与版本漂移修复（2026-08-13）

## 1. 背景

两类故障，根因不同但都落在 kb-service：

### 1.1 Catalog 基线被 pod 重建回滚

手动在 Admin 页面新增/修改的 Catalog 基线（`acli_command_catalog.json` /
`resolution_catalog.json`），在 kb-service Pod 滚动更新或节点重启后丢失，回滚到镜像内原始内容。

**第一性原理根因**：基线是 JSON 数据文件，打包在镜像层
`/app/shared/resolution/catalogs/`。页面修改经 `PUT /api/kb/catalogs/{filename}` 调用
`path.write_text()`，只写入容器可写层（内存/临时磁盘）。Pod 重建以镜像层重新挂载，
可写层丢弃 → 修改丢失。这是"有状态数据写在无状态容器文件系统"的反模式。

### 1.2 KBD 23821 诊断报 tool_contract_revision stale

参考案例 23821 在仿真测试中持续报 `expert publish tool contract revision is stale`，
根因为 agent-service 与 kb-service 的 shared 依赖（signals schema，PR #745）指纹分叉：
agent-service 已升级到含 #745 的镜像，而 kb-service 镜像停留在 #739（`4bfc2c5`），
两者 `tool_contract_revision` 不一致。

## 2. 约束与对抗性审查

- **运行时代码完整性防护（D-015/D-020）**：集群 `ValidatingAdmissionPolicy` 禁止任何
  `volumeMount` 覆盖 `/app/app/`、`/app/shared/` 或 `*.py`，写入前以 `Fail + Deny` 拒绝。
  → 不能把 PVC 直接挂载到 `/app/shared/resolution/catalogs/`，否则 Pod 创建被拒。
- **emptyDir 否决**：pod 重建即丢，必须用 PVC。
- **initContainer 覆盖用户修改否决**：必须"仅当目标文件不存在才拷贝"。

## 3. 修复方案

### 3.1 Catalog 持久化（数据/代码分离）

- 新增 `kb-service/pvc.yaml`：`kb-service-catalog-data`（RWO，默认 1Gi，`storageClass` 可配）。
- 新增 initContainer `init-catalog-baseline`：从镜像 `/app/shared/resolution/catalogs`
  拷贝基线到 PVC 挂载点 `/data/catalogs`，仅缺失时拷贝，保护已有页面修改。
- 主容器 `volumeMounts` 将 PVC 挂到**非受保护路径** `/data/catalogs`。
- `backend/shared/resolution/catalog.py` 支持环境变量覆盖路径：
  `ACLI_CATALOG_PATH` / `RESOLUTION_CATALOG_PATH` 设置时作为权威路径，否则回退镜像内置目录。
- kb-service deployment 在 `persistence.enabled` 时注入上述两个环境变量指向 `/data/catalogs`。
- `values.yaml` 新增 `kbService.persistence`（默认 `enabled: true`）。

### 3.2 版本漂移修复（触发 kb-service 重建）

- `backend/kb-service/app/routes/resolution_catalogs.py` 写路径增加调用链 `trace_id`、
  Prometheus 指标（`hci_catalog_save_total` / `hci_catalog_save_errors_total`）与结构化日志。
  该改动触达 `backend/kb-service/`，CI 将重建并推进 kb-service 镜像到含 #745 的版本，
  消除与 agent-service 的 signals 指纹分叉。
- `backend/shared/observability/metrics.py` 新增 Catalog 写路径业务指标。
- `scripts/ci/resolve_image_build_plan.py` 新增回归测试
  `test_resolve_image_build_plan.py`，断言 `backend/shared/` 变更必选全部后端服务（含 kb-service），
  防止 shared 依赖漂移类故障复发。

## 4. 验证

- `helm template` 渲染确认 PVC、initContainer、主容器挂载与 env 正确，且挂载点不在 `/app/shared/`。
- `pytest scripts/ci/test_resolve_image_build_plan.py` 通过（含 shared→全部后端服务的断言）。

## 5. 已知权衡

存量 Pod 内已有的页面修改（从未持久化）在本 PR 部署后会被重置为当前镜像基线，
属一次性迁移；此后所有新增/修改均持久化到 PVC。
