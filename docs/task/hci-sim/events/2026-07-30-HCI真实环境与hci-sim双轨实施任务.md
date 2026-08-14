---
status: active
category: task
audience: architect, developer, tester, operator
last_updated: 2026-07-30
owner: team
---

# HCI 真实环境与 hci-sim 双轨实施任务

> 对应需求：[HCI 真实环境与仿真环境双轨测试需求](../../requirement/hci-sim/events/HCI真实环境与仿真环境双轨测试需求.md)
>
> 对应方案：[HCI 真实环境与 hci-sim 双轨运行时设计](../../solution/hci-sim/events/HCI真实环境与hci-sim双轨运行时设计.md)

## 1. 当前事实状态

- [x] Terminal Bridge P0 真实案例链路和可观测性已验证；
- [x] real/sim 双轨职责、一次运行互斥和禁止 fallback 已形成方案；
- [x] Go SSH runtime、K3s 独立 namespace、Scenario Lease 和 100+ 逻辑隔离已形成设计；
- [x] dev 已有 KBD 27123 和可作为 Golden provenance 的真实 Trace；
- [x] 已创建 `hci_sim/` Go module、镜像和真实 SSH runtime；
- [x] 已创建 `hci-sim-dev` namespace 和 `hci-sim` Helm release；
- [x] 已冻结 KBD 27123 Scenario Lease/Fixture Manifest/错误 envelope/Trace over SSH P0 契约；
- [x] 已通过工单 `Q2026073088434` 的 KBD 27123 单轮 Golden Agent E2E；
- [ ] 尚未完成 real/sim differential test；
- [ ] 尚未完成 100+ 并发验收；
- [ ] 当前状态只能宣称 KBD 27123 单场景 P0 Golden E2E 已通过，不得宣称 Agent 100+ E2E 已通过。

## 2. P0-0：Golden Contract 与 Oracle Spike（1～2 人日）

### 2.1 冻结真实基线

- [x] 锁定 KBD `support_id=27123` 的 revision 1、revision checksum 和三段信号 schema；
- [x] 读取已验收 Trace `c6acbb7bdf5faffaedf5a6faa04eeb97` 对应的受控 Artifact；
- [x] 记录 task、node list、lsof、ps 四类 canonical command；
- [x] 记录 stdout/stderr、exit code、PTY、chunk、delay、timeout 和 output filter 行为；
- [x] 记录 sig_001 → VM/HOST/END、sig_002 → PID、sig_003 → CMD 的 produces/requires；
- [x] 对 Fixture 执行 VM、IP、主机名、PID、路径和输出参数化，不包含 token/password/private key；
- [x] 生成 revision checksum 和 Fixture Manifest hash；
- [x] 真实 HCI 凭据未进入文档、终端回显、fixture 或 Git。

### 2.2 冻结协议 v1

- [x] 实现 `ScenarioLease v1` signed claims；
- [x] 实现 `FixtureManifest v1` 静态 schema；
- [x] 定义 ScenarioContext、variant 和 canonical command 复合路由；
- [x] 定义 `fixture_not_found`、`policy_denied`、`sim_overloaded`、timeout 和 permission envelope；
- [x] 定义 stdout/stderr/exit-status/chunk/delay 语义；
- [x] 定义 `TRACEPARENT/TRACESTATE/HTP_EXEC_ID/HTP_TEST_RUN_ID/HTP_NODE_IP/HTP_CONTAINER` SSH env allowlist；
- [x] 通过 Lease 固定 `execution_mode=sim-ssh`；
- [x] unknown Fixture fail closed，不触发真实 HCI fallback；
- [ ] 定义 real/sim differential comparison 的允许差异和禁止差异。

### 2.3 Golden Fixture

- [x] 生成 positive-realistic fixture；
- [ ] 生成 positive-minimal fixture；
- [x] 生成 negative fixture；
- [x] 生成 near-miss fixture；
- [x] 生成 timeout、permission 和 unknown 变体；
- [ ] 固化随机 chunk/delay seed，确保可重复；
- [x] 计算并在 Lease/Status/Span 中绑定 fixture manifest hash；
- [ ] 由 KBD/Agent 专家审核 fixture，不允许 Compiler 自动发布。

### 2.4 P0-0 Go/No-Go

- [ ] 能从真实 Artifact 得到可审查且脱敏的 Fixture Manifest；
- [ ] Matcher 对 positive/negative/near-miss 的结果符合预期；
- [ ] producer/consumer 变量链可以在 manifest 中完整表达；
- [ ] 错误和超时语义不会被 CDD 误归约为 PASS；
- [ ] 不满足任一项时停止 Runtime 扩展，先修正 KBD/工具/Matcher 契约。

## 3. P0：hci-sim Runtime Spike（3～5 人日）

### 3.1 Go 模块

- [x] 创建根级 `hci_sim/` Go module；
- [x] 实现 SSH auth/session/exec/exit-status；
- [x] 实现受控 PTY 和 env request；
- [x] 实现 signed lease 校验、过期、session/command quota；
- [x] 禁止 shell operator、多行命令和 `/bin/sh -c`；
- [x] 实现 canonical command fingerprint；
- [x] 实现静态 Fixture Manifest loader；
- [x] 实现 bounded queue 和固定 worker pool；
- [x] 实现 stdout/stderr 分流、chunk/delay 和 context cancel；
- [x] 实现 unknown/policy/overload fail-closed；
- [x] 单元/SSH 集成测试覆盖认证、Lease、路由、variant 隔离、错误和配额。

### 3.2 可观测性

- [x] 接收并校验 W3C `traceparent`；
- [x] 创建 hci-sim command/fixture 子 Span；
- [x] 结构化日志记录 trace/exec/scenario/fixture/fingerprint 和错误分类；
- [x] 禁止日志记录 lease token、SSH credential 和完整敏感输出；
- [x] 暴露 health/ready/status/metrics；
- [ ] 指标包含 SSH session、lease reject、inflight、queue、fixture hit/miss、bytes、timeout/cancel；
- [x] Trace、Loki、Artifact、Evaluation 和数据库可通过 trace_id/exec_id 互查。

### 3.3 Terminal Bridge 最小通用改动

- [x] 通过 SSH env request 传播 traceparent/exec/test-run；
- [x] 支持 sim 短时 lease credential，但不解析 KBD；
- [x] 日志只记录非敏感 lease id 和 execution mode；
- [x] Windows EXE 与 K3s Pod 使用同一代码和测试；
- [x] `ssh` 生产默认行为保持向后兼容；
- [x] Bridge Smoke 证明 `fixture_not_found` 返回非零且不触发真实 HCI fallback。

### 3.4 K3s/Helm

- [x] 创建独立 `deploy/helm/hci-sim/`；
- [x] 创建 `hci-sim-dev` namespace；
- [x] P0 Deployment 1 replica，Service TCP/2222 和 HTTP/8080；
- [x] 配置 readiness/liveness/startup probe；
- [x] 配置 non-root、read-only rootfs、drop ALL、seccomp RuntimeDefault；
- [x] 禁止 privileged、hostPath、runtime socket 和真实 HCI key；
- [x] 添加 ResourceQuota、LimitRange 和可选 PDB；
- [x] 添加 default-deny NetworkPolicy；
- [x] 只允许指定 Terminal Bridge 入站以及 DNS/OTLP 出站；
- [x] 验证 agent-service 不能直连 2222，hci-sim 非必要出站被阻断；
- [x] 未恢复当前 dev GitOps self-heal；
- [ ] prod values 默认 disabled，且不渲染 Service/Secret。

### 3.5 P0 端到端验收

- [x] cluster Terminal Bridge → hci-sim 完成 KBD 27123 Agent E2E；
- [ ] Windows Terminal Bridge → 同一 hci-sim 完成兼容性 smoke；
- [ ] positive/negative/near-miss/timeout/permission/unknown 均通过；
- [x] `trace_id -> exec_id -> hci-sim span -> fixture/artifact -> evaluation -> conclusion` 完整；
- [ ] 独立 real run 与独立 sim run 完成 contract diff；
- [ ] 重复运行 sim 至少 20 次结果稳定；
- [ ] 断网、Pod 重启、lease 过期时明确失败；
- [x] P0 验证报告记录限制，不宣称已支持 100+。

## 4. P1：Fixture 控制面

- [ ] Python/FastAPI Scenario API；
- [ ] KBD revision/signal/tool/matcher Compiler；
- [ ] `agent_test_scenario`、`agent_test_fixture`、`agent_test_run` 数据模型；
- [ ] 数据库改动附 desired schema、幂等迁移和迁移说明；
- [ ] 对象存储/OCI fixture bundle；
- [ ] manifest hash、签名、发布、撤销和 stale；
- [ ] positive/negative/near-miss/error 自动草稿；
- [ ] complex matcher 转人工补充任务；
- [ ] Artifact 脱敏和 secret scan；
- [ ] draft/validated/published/retired 审批状态机；
- [ ] 真实 Oracle 复采和版本漂移作业。

## 5. P1：Scheduler 与 100+ 并发

### 5.1 调度和隔离

- [ ] TestRun 批量创建和 scenario selection；
- [ ] signed lease 签发、续期、撤销和审计；
- [ ] per-run/per-scenario/session/command/output quota；
- [ ] bounded queue/backpressure；
- [ ] retry limit、scenario deadline、run deadline；
- [ ] Stateless 多副本；
- [ ] Redis/CAS state overlay + TTL + CAS；
- [ ] 强状态 shard assignment；
- [ ] fixture LRU cache、checksum 和总字节上限；
- [ ] 资源自动清理和 orphan lease 回收。

### 5.2 Headless Runner 与浏览器

- [ ] headless runner 实现与 Custom UI 相同的 command/exec-result 协议；
- [ ] Bulk Agent E2E 仍经过 cluster Terminal Bridge；
- [ ] 1～10 Playwright context 做 Golden UI E2E；
- [ ] 如验收 100 浏览器并发，单独建立浏览器负载 job；
- [ ] 报告明确区分 hci-sim SSH 容量、Bulk Agent 容量和 Browser UI 容量。

### 5.3 容量梯度

- [ ] 1 场景 correctness baseline；
- [ ] 10 场景初始并发；
- [ ] 50 场景资源曲线；
- [ ] 100 场景目标验收；
- [ ] 200 场景压力/过载验收；
- [ ] 记录 p50/p95/p99、queue、reject、RSS、GC、session、throughput；
- [ ] 串线计数为 0；
- [ ] 一个 Scenario timeout/miss/断连不影响其他 Scenario；
- [ ] 过载时明确 `sim_overloaded`，无 OOM 和无界等待；
- [ ] 根据压测更新 worker、replica、CPU/memory 和输出预算，不把初始假设写成事实。

## 6. P1：可观测性和效果评估

- [ ] 所有模拟 Span/日志/指标携带 execution mode 和 scenario/fixture metadata；
- [ ] 模拟与生产 Grafana dashboard/成功率分区；
- [ ] fixture hit/miss、lease reject、queue wait、worker、cache、timeout 可查；
- [ ] 命令选择正确率；
- [ ] acquisition/producer-consumer 完整率；
- [ ] Matcher positive/negative/near-miss 准确率；
- [ ] ERROR/BLOCKED 被误判 PASS 数量为 0；
- [ ] Conclusion Gate 正确率；
- [ ] 报告证据引用完整率；
- [ ] mutation detection rate；
- [ ] real/sim differential drift rate；
- [ ] 10～20 个核心 KBD validated 后才宣称 MVP。

## 7. P2：产品化

- [ ] KBD 详情生成 Scenario 草稿；
- [ ] Fixture stdout/stderr、chunk/delay/error 编辑和预览；
- [ ] provenance、脱敏、lint 和审批 UI；
- [ ] stale 提示和一键复采/重编译；
- [ ] 分类/版本/variant 批量测试；
- [ ] KBD 可测试性覆盖率；
- [ ] mutation 测试；
- [ ] 100～1000 场景持续回归和趋势报表；
- [ ] 强状态/多 HCI profile；
- [ ] 成本、容量和 flaky 率治理。

## 8. 回滚与停止条件

- hci-sim 使用独立 namespace、release 和 values，删除不影响 hci-real；
- Terminal Bridge `ssh` 模式保持默认，sim 能力可通过配置完全关闭；
- hci-sim 不可用时测试明确失败或跳过，绝不切换真实 HCI；
- fixture schema 回滚不改写不可变 KBD revision；
- P0 若 real/sim contract 不等价、Trace 断链或错误被误 PASS，停止 P1；
- 100 场景若出现任何 cross-scenario contamination，属于 P0 级阻断；
- 安全测试发现 hci-sim 可访问真实 HCI、执行任意 shell或泄露凭据，立即停止部署。

## 9. 预计工作量

| 阶段 | 范围 | 估算 |
|---|---|---:|
| P0-0 | Golden Contract、Artifact 脱敏、Lease/Manifest/错误/Trace 协议 | 1～2 人日 |
| P0 | Go SSH runtime、K3s、Bridge 通用传播、Golden E2E | 3～5 人日 |
| P1 | Compiler、Registry、Scheduler、100+、10～20 KBD、CI/观测 | 30～45 人日 |
| P2 | UI、审批、stale、mutation、100～1000、运营报表 | 累计 55～80 人日 |

工作量以现有 Terminal Bridge P0、KBD v2、QKV/QFK、CDD、Tempo/Loki/Artifact 能力可复用为前提。若现有协议无法携带 lease/trace 或真实 Artifact 不可复用，需要重新评估。
