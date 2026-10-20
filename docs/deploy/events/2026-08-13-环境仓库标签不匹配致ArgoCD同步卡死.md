# 2026-08-13 ArgoCD 同步卡死：env 仓库 tag 与 ghcr 镜像脱节

- 环境：`dev`（hci-platform-dev）
- 时间：2026-08-13（PR #747 合并后约 8 小时持续 Progressing）
- 现象：`hci-platform-dev` 显示 `Synced / Progressing`，`operationState.phase=Running`，
  message=`waiting for healthy state of apps/Deployment/admin-ui and 9 more resources`。
- 根因 commit：`e444a40`（PR #747 主库 agent_test 僵尸表清理，纯数据库改动）。

## 1. 症状（k3s 第一手事实）

- 10 个自研 Deployment 新建 ReplicaSet，Pod 状态 `ImagePullBackOff`，reason= `Failed`，
  `code = NotFound` / `manifest unknown`：镜像
  `ghcr.io/tomturing/hci-troubleshoot-platform/<svc>:20260813-0244-e444a40` 不存在。
- `ops-agent-service`（外部镜像 `ghcr.io/p3n9w31/...`）正常，证明 ghcr-pull-secret 有效，
  排除鉴权问题 → 确为镜像缺失。
- 旧 Pod（tag `20260812-1159-0158043`/`f72be11`）仍在 Running，整体 `READY 1/1`，
  但 ArgoCD 等待新 ReplicaSet Available，故永久 Progressing。

## 2. 根因（第一性原理）

镜像 tag 由 env 仓库 `environments/<env>/values.yaml` 注入，格式 `YYYYMMDD-HHMM-<sha7>`。
该 tag 由 `env-repo-sync.yml`（workflow_dispatch）→ `scripts/ops/sync-env-repo-tags.sh` 写入。

断裂点：`sync-env-repo-tags.sh` 的 `SERVICES_CSV` 默认是**写死全量 10 个业务服务**，
`env-repo-sync.yml` 的 `services_csv` 默认也是写死全量（8 个）。
当人工/外部以 `image_tag=<某 commit sha>` 触发 `env-repo-sync` 且未传 `services_csv` 时，
会把**全部业务服务 tag 无差别推进到该 commit 标签**——而该标签镜像是否真实存在于 ghcr，
脚本与 workflow 均不校验。

PR #747 是纯 `database/` 改动：
- CI 的 `resolve_image_build_plan.py` 按 Dockerfile COPY 边界判定 `has_deploy_services=false`
  → `build-and-push` 不运行 → ghcr 上**没有 `e444a40` 业务镜像**；
- 但某次 `env-repo-sync` 触发把 env 仓库 dev/staging 的 10 个业务 tag 全部写为 `e444a40`
  → ArgoCD 渲染 `e444a40` → 新 Pod 拉不到镜像 → 永久卡死。

本质：**env 仓库 tag 与“ghcr 实际存在的镜像”脱钩**，且同步流程不校验镜像存在性。

## 3. 修复（方案 C 轻量版：fail-safe）

### 3.1 `scripts/ops/sync-env-repo-tags.sh`
- `SERVICES_CSV` 默认值由写死全量改为**空**（fail-safe）。
- 新增门控：当 `SERVICES_CSV` 为空时，必须显式传 `SKIP_BUSINESS_TAGS=true` 才允许继续，
  否则中止——避免无差别推进全量业务 tag。空清单 + `SKIP_BUSINESS_TAGS=true` 表示
  “本次无业务镜像需同步”，仅更新 dbMigrate 等独立镜像。

### 3.2 `.github/workflows/env-repo-sync.yml`
- `services_csv` 输入默认改为空，新增 `skip_business_tags` 布尔输入（默认 `false`）。
- 校验：空 `services_csv` 且 `skip_business_tags != true` → 直接失败退出。
- 透传 `SKIP_BUSINESS_TAGS` 给脚本。

### 3.3 使用约定（治本）
- 纯 DB / 文档 / CI 配置改动（`has_deploy_services=false`）：触发 `env-repo-sync` 时
  **留空 `services_csv` 并勾选 `skip_business_tags=true`** → 不碰业务服务 tag。
- 有业务镜像构建：必须经由 CI `build-and-push`（确保 ghcr 镜像已存在）后，
  由 CI 用实际 `deploy_services` 调用 `env-repo-sync`，不得人工传任意 commit sha 全量同步。

## 4. 验证

- 单元验证：在本地用 `SERVICES_CSV=` + `SKIP_BUSINESS_TAGS=false` 运行脚本应**立即失败**；
  加 `SKIP_BUSINESS_TAGS=true` 则跳过业务 tag 更新。
- 集成验证：`dev` 环境将 env 仓库 dev values 的误推进 tag（`e444a40`，ghcr 缺失）
  回退到真实存在的 `f72be11` → ArgoCD 重新同步 → 10 个 Deployment 转 Healthy，
  证明“env tag 与 ghcr 一致”时同步链路正常；后续纯 DB PR 不再误推进业务 tag。

## 5. 对抗性审查结论

- 单纯“等 CI 推送 `e444a40`”不可行：PR #747 不构建业务镜像，该 tag 永远不会出现。
- 单纯补 `e444a40` 镜像（B1）能解当前卡死但掩盖根因，下次同类改动重演。
- 只有让同步流程“只动实际构建的服务 / 显式确认无业务镜像”，才能根治。
