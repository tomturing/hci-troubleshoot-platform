# 2026-08-13 KBD 23821 仿真 delta Matcher 阻塞修复与 Fixture 补齐

## 背景

仿真环境 KBD 23821（虚拟机-015 跨存储热迁移失败/迁移卡 9%）诊断始终 INCONCLUSIVE，
即使 PR743-PR746 已合入。诊断结论为"证据不足 / 无可执行证据"。

## 根因（分层取证）

1. **代码层（本次修复）**：DB 23821 信号 `sig_002` 的 matcher 为 `type: delta`
   （`delta == 0` 判定迁移完成），但 `log_source_catalog` 中
   `sfvt_vtpdaemon.log`（vtpdaemon）的 `predicates` 仅含
   `keyword/regex/state/threshold/exists`，**不含 `delta`**。
   `LogKeywordHandler.build_commands` 直接抛 `CommandBuildError`
   （日志源不支持 delta predicate），即使 fixture 正确也必然失败。

   - 2026-08-07 已定案《QFK 取值判断产出统一执行契约与 AI 数值提取方案》：
     "当且仅当第一步配置非空 `ai_extract.instruction` 且属于普通数值 Matcher 时，
     允许走 AI 类型化取值 → 确定性判断通道"。handlers.py 未落地该豁免，属契约不一致 bug。
   - 本次修复：`threshold/delta/trend` 数值 Matcher 配置了 `ai_extract.instruction`
     时放行（走 AI 数值提取通道）；未配置 AI 时仍按 catalog 单一事实源 fail closed。

2. **配置层（本次补齐）**：运行中 `hci-sim-fixture` configmap 仅含 27123 fixture，
   无 23821 的任何路由。DB 23821 三信号工具为 `qkv_task / qfk_log / qfk_system`，
   实际发往 hci-sim 的命令（argv[0]=acli）无匹配路由 → `fixture_not_found`。

3. **部署层（历史事实）**：运行镜像 agent-service=`20260811-1313-f72be11`、
   hci-sim=`9307b597` 均早于 PR743 合入，PR743-746 从未实际部署。

## 修复内容

1. `backend/agent-service/app/tools/qfk/handlers.py`：数值 Matcher（threshold/delta/trend）
   + 非空 `ai_extract.instruction` 时跳过 catalog predicates 检查（对齐 2026-08-07 契约）。
2. `deploy/helm/hci-sim/files/kbd-23821-fixture-manifest.json`：新增正式 fixture
   （非 synthetic，`kbd.revision=25` 匹配运行时 `HCI_SIM_ACTIVE_REVISION`），
   含三条路由：
   - `sig_001` → `acli --formatter json task get -k 迁移虚拟机 -s failed -l 1`
     （qkv_task，返回含 vm/host/END 的 JSON）
   - `sig_002` → `acli log get -E -k 'Completed|info\ block-jobs'
     -f sfvt_vtpdaemon.log -p /sf/log/18/vt -t '2023-09-18 17:19:07'`
     （qfk_log，输出两行 Completed SIZE of SIZE，delta==0）
   - `expert_1786499837113_brbf6r6fivk` → `acli --timeout 60 --container asv-con
     system qmpcmd 271230001 info block-jobs`（qfk_system，输出不含 "ready"）
3. 部署：ArgoCD `hci-sim-dev` helm values 增加
   `fixture.manifestFile: files/kbd-23821-fixture-manifest.json`；
   agent-service 镜像重建（含 handlers.py 修复）。

## 验证

仿真环境重跑虚拟机-015 场景，三信号 `sig_001/sig_002/expert_*` 均 SATISFIED，
KBD 23821 判定 DEFINITIVE。

## 构建链路修复（部署门禁）

重建 agent-service 镜像后新 Pod 启动即 `PermissionError: [Errno 13] Permission denied:
'/app/app/main.py'`，CrashLoopBackOff。

- **根因**：`backend/agent-service/Dockerfile` 用 `COPY backend/agent-service /app`
  直接复制源码，保留宿主机文件 mode。宿主机 `main.py` 为 `-rw-------`（600），
  运行时 securityContext `runAsUser:1000, runAsNonRoot:true` 的非 root 用户无法读取
  600 文件 → uvicorn 在 `import_from_string` 阶段崩溃。
- **修复**：Dockerfile 在 COPY 后增加 `RUN chmod -R u+rwX,g+rX,o+rX /app`，
  使构建产物权限确定、不依赖宿主机 umask，与运行时非 root 用户解耦。
- **连带修复**：`scripts/ops/local-deploy.sh` 的 `sync-env-repo-tags.sh` 误将
  `dbMigrate.tag` 同步成业务镜像 tag（该镜像未构建 → PreSync hook ImagePullBackOff
  → ArgoCD 永久卡在 hook）。已回退 env repo `dbMigrate.tag` 至已存在的
  `20260813-0244-e444a40`，并清理卡住的旧 Job（移除 ArgoCD `hook-finalizer`）。
- **部署顺序经验**：ArgoCD PreSync hook（db-migrate Job）失败时，需同时删除 Job 的
  `argocd.argoproj.io/hook-finalizer` finalizer 才能真正解除，否则 `operation:null`
  与 `refresh=hard` 都无法推进。
