
# live E2E 真仿真端到端验证 runbook (live-e2e-real-sim)

本技能沉淀「为某故障场景跑真正的仿真端到端测试」的可复用流程：**命令真经 terminal_bridge 下发到隔离 hci-sim fixture 节点、真采集 stdout 回灌进 SOP 变量池、驱动到根因结论**，直到 harness 给出多数表决 `GREEN ✅`。区别于 stub/单元/集成测试——它验证的是「各层拼装后的真实采集闭环」，是发布前 L3 契约缝隙 gate。

## 何时用本技能

- 需要证明「SOP 里的 tool_call 命令真的被下发到节点并采回真输出」（不变量 `V017_tool_call_collected`），而非静态答案回放。
- 改了 `terminal_bridge` 的命令包装逻辑，需在真链路里验证派发形态。
- 为新场景搭建可复用的端到端判据（多数表决越过 LLM/S0 非确定性）。
- 用户说「先补齐端到端全绿再合 PR」。

## 第一性原理：链路的不可约约束

端到端只认证据，不接受「应该能跑通」的推断。整条链路由以下**硬事实**串起，任一断裂即 RED：

1. **hci-sim 按 argv 字符串匹配 fixture，从不 exec 真实二进制**（`hci_sim/internal/fixture/fixture.go`）。其受限 lexer `Lex()` 拒绝一切引号外的 shell 操作符 `;|&<>()`。→ 任何把命令包成复合串的尝试都会破坏匹配。
2. **lease 的 container 恒为 `host`**（签 lease 时 `--container host`）；hci-sim 无真实容器。→ 仿真会话下发给它的必须是**裸用户命令**，绝不该有 `container_exec` 包裹、绝不该有 `export PATH=...;` 前缀。
3. **bridge 的仿真分支**（`wrapContainerCommand`，`simulation==true`）必须：跳过 PATH 前缀（否则 `;` 触发 `policy_denied exit126`）+ 解出服务端容器派发壳 `HCI_USER_COMMAND` 的内层裸命令（否则整段派发壳被当 tool=container_exec 的畸形 argv → `fixture_not_found exit127`）。
4. **SOP 变量取值若走 skill_call（LLM）在 staging 外部模型上会 MaaS `AI_TIMEOUT`**（flaky）。→ 用「预种变量」把易超时的派生变量确定性写入变量池，让链路稳态推进。
5. **S0 意图分类非确定**（qwen 时而吐候选卡、时而直接确认、时而候选不含目标码）。→ 靠 `--attempts` 多数表决越过，绝不因单次 RED 判定失败。

## 对抗性审查：判绿前必须证伪的失败模式

| 症状 | 根因 | 定位手段 | 处置 |
|------|------|----------|------|
| `exit 126` policy_denied | PATH 前缀把裸命令包成含 `;` 复合串 | bridge pod 日志 `exec.request` 看下发命令 | 确认 `simulation==true` 分支不加前缀 |
| `exit 127` fixture_not_found | 容器派发壳被 bridge **二次 container_exec 包裹** | `kubectl logs live-e2e-bridge` 抓 exec.request，看是否 `container_exec -n ... -c '...派发壳...'` | bridge `extractContainerUserCommand` 解内层裸命令 |
| 命令带双 `/dev/`（如 `/dev//dev/sdb`） | seed 变量喂了 `/dev/sdb`，但 SOP 模板硬编码 `/dev/{disk_dev}` | 查场景 `seed_variables` | seed 喂**裸盘符** `sdb` |
| `S0_category_confirmed observed=None`（明明点选了正确码） | harness 观测缺陷：终态事件不带 category_code，`_scan_terminal_signals` 返回 None 把已确证值冲掉 | driver.py 观测合并段 | `merge_observed_signals`：已确证值非空优先保留 |
| attempt 偶发 S1_sop_hit=None | S0 候选不含目标码（模型非确定性） | 看该 attempt 是否「候选不含期望分类」 | 属预期随机源，靠多数表决，不改代码 |

**绝不在仅"跑到结论但未判票"时合并**——必须拿到 harness 明确的 `→ GREEN ✅`。

## 前置条件

- staging/live k3s 单节点，`node` 用户 `kubectl` 可用（**无需 sudo/root/docker**，用集群内 Pod 构建）。
- `main` 已含 `evaluation/live_e2e/`（driver/smoke/asserts + fixtures）。
- 有承载 bridge 修复的 **worktree**（特性必须 worktree 开发）；其 `terminal_bridge/` 与 `evaluation/live_e2e/fixtures/<scenario>/bin/` 供 hostPath 挂载。
- 宿主机 `~/go/pkg/mod` 已缓存 Go 依赖（离线构建 `GOPROXY=off` 的前提）。
- 平台服务（case/conversation/agent）已在集群某 ns 运行，能取到 ClusterIP。

## 执行流程（推荐用附带的 runbook）

`.agents/skills/live-e2e-real-sim/scripts/live_e2e_real_sim.sh` 把下述步骤参数化编排（自动发现服务 ClusterIP、渲染 manifest、trap 清理）。手动等价步骤如下，理解原理后用脚本或直接手敲皆可：

```bash
# 0) 自动发现平台服务 ClusterIP（staging 上每次部署可能变，勿硬编码）
kubectl get svc -A | grep -E 'case-service|conversation-service|agent-service'

# 1) 建隔离 ns
kubectl create ns hci-sim-live-e2e

# 2) 起 hci-sim fixture server（先建 lease HMAC secret + bundle configmap，再 apply sandbox-pod.yaml）
#    端口：SSH :2222、HTTP :8080；service ClusterIP 即 lease host

# 3) 集群内门禁：gofmt -w 回写 → gofmt -l(须空) → go vet → go test（参考 bridge-gate-verify-pod.yaml）
kubectl apply -f evaluation/live_e2e/fixtures/<scenario>/bridge-gate-verify-pod.yaml
kubectl -n hci-sim-live-e2e wait --for=jsonpath='{.status.phase}'=Succeeded pod/bridge-gate-verify --timeout=5m

# 4) 集群内离线构建含修复的静态 bridge 二进制（golang:1.26.6-alpine + hostPath ~/go/pkg/mod，
#    GOPROXY=off CGO_ENABLED=0 → 产出静态 ELF 到 hostPath bin/，参考 bridge-fix-build-pod.yaml）

# 5) 热替换隔离 bridge Pod：mounts hostPath bin → /opt/bridge，command=/opt/bridge/terminal_bridge
#    ⚠ delete+apply 会遇 terminating 竞争（apply 报 configured 但 pod NotFound），用 `kubectl create -f` 内联清单拉起
kubectl -n hci-sim-live-e2e get pod live-e2e-bridge -o jsonpath='{.status.phase}'  # Running
kubectl -n hci-sim-live-e2e logs live-e2e-bridge | grep listen   # v*-dev listen=ws://0.0.0.0:9999

# 6) 现签 lease（TTL 4h；隔时再跑务必重签）：在 hci-sim pod 内用其自有 key 签，写 connection.json
python3 evaluation/live_e2e/fixtures/<scenario>/make_connection.py

# 7) 驱动 live E2E smoke（真采集闭环）
PYTHONUNBUFFERED=1 PYTHONPATH=backend INTERNAL_API_TOKEN=<内部令牌> \
uv run --frozen --python 3.12 -m evaluation.live_e2e.smoke \
  --mode real --sim --attempts 3 --max-turns 12 \
  --case-base  http://<caseIP>:8001 \
  --conv-base  http://<convIP>:8002 \
  --agent-base http://<agentIP>:8005 \
  --bridge-url ws://<bridgeIP>:9999 --bridge-origin http://<bridgeIP>:9999 \
  --connection evaluation/live_e2e/fixtures/<scenario>/connection.json \
  --dump-dir .local/live_e2e/simrunN
```

### 判票
观察 `--dump-dir` 对应日志末尾：`[live-e2e] 多数表决: 2/3 通过（判绿需 ≥2）→ GREEN ✅`。关键不变量全 ✓：`S0_category_confirmed / S1_sop_hit / VarSubmit_persisted / UserQuestions_grounded / V017_tool_call_collected`。`collected via bridge: ... exit=0 out[N]` 是真采集铁证。

### 清理（判绿后）
```bash
kubectl delete ns hci-sim-live-e2e --wait=false   # 销毁 build/verify/bridge/hci-sim/svc 全套隔离资源
# 删除 hostPath bin/ 里的 root 私有二进制（其目录属当前用户可 unlink）
# 删 worktree + 本地/远程分支；同步 main
```

## 场景可移植性（通用框架 vs 场景夹具）

- **通用可复用**：`driver.py`（SSE 驱动/交互提交/预种变量/观测合并）、`smoke.py`（多数表决 CLI）、`asserts.py`（5 条不变量）、runbook 脚本、集群内构建+热替换+重签 lease 的方法论——任何场景共用。
- **场景专有**：`scenarios/<x>.json`（ticket/seed_environment/seed_variables/expect）、fixtures 里的 bridge/hci-sim manifest 与 fixture-manifest（route_key/argv 匹配表）。新场景需按 §第一性原理 提供对应 fixture，并把 manifest 里的裸 argv 与 SOP 渲染出的命令精确对齐。

## 交付门禁义务（改动落地时）

- 触及 `terminal_bridge/`/`scripts/`/`docs/` 等：**同一 commit/PR 同步** `AGENTS.md` 文档门禁条目（记录根因/修复/测试守护/端到端实证）。
- 测试守护随改动进 CI：Go 侧 `wrap_container_test.go`、Python 侧 `tests/unit/test_live_e2e_driver.py`（观测合并纯函数）。
- commit 尾标 `[env:<role>:<hostname>][agent:Qoder]`（env 来自 `kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}'`）；PR labels `env:...` + `agent:Qoder`；禁直推 main，走 PR + 监控 CI 全绿 + 合并后删分支/worktree。

## 关键源码坐标（本项目）

- `terminal_bridge/main.go::wrapContainerCommand / extractContainerUserCommand / scanFirstShellToken`
- `hci_sim/internal/fixture/fixture.go::Lex / Match`；`server.go` execute
- `backend/agent-service/app/tools/acli/container_exec.py`（生成派发壳）/ `executor.py`（下发 built_command）
- `backend/conversation-service/app/routes/agent_exec.py`（`command = body.built_command or body.command` 转发派发壳给 bridge）
- `evaluation/live_e2e/driver.py`（`_seed_sop_variables` / `merge_observed_signals` / `_relay_exec`）
- `evaluation/live_e2e/fixtures/<scenario>/{make_connection.py,sandbox-pod.yaml,sandbox-bridge-fixed.yaml,bridge-fix-build-pod.yaml,bridge-gate-verify-pod.yaml,kbd-*-fixture-manifest.json}`
