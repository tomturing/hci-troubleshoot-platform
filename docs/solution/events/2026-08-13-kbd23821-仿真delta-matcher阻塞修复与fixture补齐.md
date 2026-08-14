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

## 二次复发根因：第三层 AI 通道豁免取错 filter 源字段（#749 部署后仍报 metric 错误）

PR #749 合入并部署后，实际诊断仍报
`sig_002: runtime command compile failed: qfk_log delta matcher 必须提供 metric`，
与修复前现象一致。第一性原理复盘如下：

### 根因（对抗性审查揭示）
#749 的第三层豁免逻辑在 `handlers.py` 的 `_matcher_selector` 中，
delta matcher 缺 `metric` 时取
`ai_filter = signal.filter_keywords or signal.keyword`
作为 AI 通道的粗筛关键字。但 **`signal.filter_keywords` 是派生字段**，
在诊断运行时（kbd_differential 构造 BackendSignal）该派生链为空；
而 sig_002 的关键字事实真正存放在
`match.extract.rows.include = ["info block-jobs","Completed"]`。
取错字段 → `ai_filter` 为空 → 豁免分支不进入 → 仍抛 `CommandBuildError`。

现场复现（`build_acli_command` 单测）：
- `BackendSignal(filter_keywords=[])` → `FAIL: qfk_log delta matcher 必须提供 metric`
- `BackendSignal(filter_keywords=["info block-jobs","Completed"])` → `OK`

### 修复
`_matcher_selector` 的 AI 通道豁免**直接从权威字段
`matcher.extract.rows.include` 取关键字**（而非派生字段 `signal.filter_keywords`），
仅在 include 为空时回退到 `signal.filter_keywords/keyword`。
这样无论 filter_keywords 派生是否成功，AI 通道都能拿到粗筛关键字。

### 复现验证
```python
# filter_keywords 为空（复现运行环境现状）
BackendSignal(filter_keywords=[]).build_acli_command()
# -> OK: acli log get -E -k 'Completed|info\ block\-jobs' -f sfvt_vtpdaemon.log ...
```

### 关于 `expert publish tool contract revision is stale`
经运行容器（kb-service）实测 `current_tool_contract_revision()` = `cf416e14...`，
与 DB `publish_validation.tool_contract_revision` 完全一致，**该 stale 判断在运行环境为假**；
用户诊断输出中的 stale 属历史快照或他处缓存，非 23821 当前阻塞项，无需重新发布刷新盖章。

## 方案 A 根治：catalog 直接补齐 vtpdaemon 的 delta/trend predicates（2026-08-14）

### 问题确认（用户对抗性审查）
- 仿真测试日志的步骤编号与真实 KBD 顺序不一致。真实 KBD 23821 顺序为：
  - 第 2 步 = 检查 **vtpdaemon 日志**中块作业状态输出，matcher `type: delta`
    （delta==0 判定迁移完成）→ **仍 BLOCKED**；
  - 第 3 步 = 在 **vt 容器**内 `acli system qmpcmd {{VM}} info block-jobs`
    （用户已加入运行态 catalog）→ 已修复。
- 第 2 步 BLOCKED 的**第一性原理根因**：`log_source_catalog.py` 中 `vtpdaemon` 的
  `predicates` 硬编码为 `("keyword","regex","state","threshold","exists")`，
  **缺 `delta`/`trend`**；而同类周期采样/白盒日志源（如 `vn_ethtool_statistics`、
  `process_snapshot`、`host_blackbox`）的 predicates 均含 `delta`/`trend`。
  vtpdaemon 的块迁移作业进度是周期采样数值（已迁移字节/总字节），天然需要 delta/trend 判定。

### 修复（代码层单一事实源）
`backend/shared/schemas/log_source_catalog.py` 的 `vtpdaemon` 条目：
```python
predicates=("keyword", "regex", "state", "threshold", "delta", "trend", "exists"),
```
- resolver 层 `resolvers.py:194` 的 fail closed 判定
  `matcher_type not in source["predicates"]` 不再触发，delta 成为 catalog 一等公民；
- `log_selector.py:53` 与 `matcher.py:7` 已原生支持 `delta`/`trend`，parser=`timestamped_lines`
  下由 AI 从有界日志行提取类型化数值，无需额外 parser 改造；
- 与 2026-08-13 handler 豁免路线**互补不冲突**：catalog 补齐后，即使未配置
  `ai_extract.instruction`，delta 也能走确定性判定通道，比依赖 AI 配置的豁免更彻底。

### 范围界定（对抗性审查）
- 本次**只改 vtpdaemon**，不改 `qemu_vm`（同为 `timestamped_lines` 白盒，predicates 也缺 delta）。
  原因：KBD 23821 第 3 步已用 `acli system qmpcmd` 直接查 Qemu 块作业，不经 `qemu_vm`
  日志源 predicate；qemu_vm 的 delta 缺口不在本次故障链路内，避免越界改动。如后续其他
  KBD 确需对 `sfvt_qemu_*.log` 做 delta 判定，再单独评估补齐。

### 测试影响
- `backend/kb-service/tests/test_log_source_catalog.py` 对 vtpdaemon 仅断言
  `family/path/date_subpath/parser`，未断言 predicates 具体集合，本改动不引入回归。
- `test_signal_text_extract_schema.py::test_qfk_log_unsupported_predicate_points_to_match_type`
  原用例用 `sfvt_vtpdaemon.log` + delta 且**期望被拒绝**；方案 A 后 vtpdaemon 直接支持
  delta，该用例前提失效。已将其 source 改为 `sfvt_qemu_vm.log`（仍不支持 delta 的对照源），
  **保留**“unsupported predicate 应报错并指向 match.type”的测试意图。
- `test_qfk_log_numeric_ai_extract_allows_delta_on_source_without_direct_delta`
  （vtpdaemon + delta + ai_extract.instruction，期望通过）不受影响，且因 catalog 直接支持
  delta 更稳健。

