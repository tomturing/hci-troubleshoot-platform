---
status: in_progress
category: solution
audience: architect, developer, tester, operator, expert, security
last_updated: 2026-08-10
owner: team
---

# hci-sim 阶段 C：Fixture 编译与注册控制面方案

## 背景与需求

阶段 B 的 Runtime 只能安全、确定地执行已经发布的 Bundle，不能回答“指定 support ID 应使用哪一个 KBD revision、输出从何而来、是否经过审核”。当前 KBD 27123 的静态 Manifest revision 已与 active 动态资源漂移，继续人工复制会形成不可追溯的第二事实源。

本阶段建立离线控制面：把不可变 KBD revision、生产 Tool Contract、Policy 和经批准的真实 Artifact 编译成内容寻址、可审查、可失效的 Manifest v2 Bundle。需求见[阶段 C 需求](../../../requirement/hci-sim/events/hci-sim阶段C-Fixture编译与注册控制面需求.md)。

## 方案（WHAT）

### 1. 控制面与数据面边界

```text
KBD Registry ─┐
Tool/Policy ──┼→ Resolver → Fixture Compiler → Lint/Mutation/Scan
Artifact Store┘                         ↓
                                  Human Approval
                                        ↓
PostgreSQL metadata ← Registry → OCI/Object Storage Bundle
                                        ↓ 只读、按 digest
                                 hci-sim Runtime
```

- Compiler/Registry 是控制面，可以读取 KBD、Tool、Policy 元数据和已批准 Artifact，但不能访问真实 HCI。
- Runtime 是数据面，只能按可信 Registry 返回的 digest 读取 `published` Bundle，没有草稿读取、审批或发布权限。
- 真实 Artifact 由独立、授权、只读、可审计的采集流程产生；本阶段不把真实环境凭据交给 Compiler 或 Runtime。
- 阶段 C 不创建 TestRun、不签发执行 Lease；这些能力属于阶段 D。

### 2. 不可变编译输入和输入指纹

编译请求允许用户输入 `support_id` 和 revision 策略，但 Resolver 必须在开始编译前把它解析为不可变快照：

```json
{
  "support_id": "27123",
  "kbd_id": "...",
  "kbd_revision": 24,
  "kbd_checksum": "sha256:...",
  "signals_digest": "sha256:...",
  "tool_contract_revision": "...",
  "policy_revision": "...",
  "compiler_revision": "git:...",
  "artifact_set": [
    {"artifact_id": "...", "digest": "sha256:...", "approval_id": "..."}
  ]
}
```

输入指纹由上述字段的 canonical serialization 计算。编译过程中 `active` 指针发生变化时，本次编译仍基于已锁定快照，结束后由 stale engine 判断是否已经过期。同一输入指纹和 Compiler 版本的重复请求必须返回同一编译作业或相同 Bundle digest。

Resolver 在下列情况 fail closed，并输出结构化 capability gap：KBD 不存在/未发布、signals 为空、revision/checksum 不可追溯、Tool/Policy revision 缺失、Artifact 未批准或 schema 不兼容。

### 3. Compiler Pipeline

```text
Resolve immutable inputs
→ compile production command contracts
→ build Signal/producer/consumer graph
→ extract observations from approved Artifacts
→ parameterize identities and volatile fields
→ generate routes/variants/oracles/provenance
→ schema + semantic lint
→ secret/PII/license scan
→ matcher dry-run + mutation testing
→ Runtime compatibility test
→ draft/validated
→ expert approval
→ content-addressed publish
```

各阶段产生独立、可审计的中间 Artifact；失败不得覆盖上一份 published Bundle。

### 4. 生产契约复用与变量图

Compiler 调用生产 Agent 使用的 Tool/Command Compiler，将每个 Signal 的工具、参数类型、采集键、节点/容器约束和 Matcher 编译为中间表示，不维护第二套命令规则。

对中间表示构建有向图：

- producer：从输出提取变量；
- consumer：在后续命令、路径或 Matcher 中消费变量；
- root input：由测试 Scenario 显式提供的虚拟节点、容器等变量；
- edge：变量名、类型、基数、编码和作用域。

发布门禁拒绝循环依赖、未定义变量、类型不一致、跨节点越权消费和不确定多 producer；合法的多值变量必须有明确排序与选择规则。

### 5. Observation 提取与参数化

Compiler 从已批准的真实 Artifact 读取 stdout、stderr、exit、采集上下文和 hash。参数化顺序固定：

1. 验证 Artifact 与 KBD/Tool/节点来源一致；
2. 根据显式提取规则定位 observation；
3. 将客户名、真实 IP、主机名、路径片段等映射为类型化变量；
4. 保留换行、列宽、编码、stdout/stderr 和 exit 等影响 Matcher 的 output shape；
5. 对参数化前后做 Matcher dry-run 和结构 diff；
6. 记录每段输出的 provenance，而不在 Bundle 内保存客户身份。

OCR/图片只可辅助人工草稿，不能直接作为机器 observation；无法可靠结构化时产生 capability gap。

### 6. Variant 和 Oracle 生成

每个可测试 Signal 至少评估以下 variant：

| Variant | 数据来源与预期 |
|---|---|
| `positive-minimal` | 满足 Matcher 的最小受控样本，仅用于单元边界。 |
| `positive-realistic` | 参数化的批准真实 Artifact，是 Golden 主证据。 |
| `negative` | 同结构但明确不满足条件，预期 Signal false。 |
| `near-miss` | 仅差一个关键语义，验证 Matcher 不过宽。 |
| `timeout` | Runtime fault，预期 ERROR/INCONCLUSIVE 而非 false。 |
| `permission` | 明确权限失败，验证错误分类和 Gate。 |
| `unknown` | 缺失或无法判定，预期 UNKNOWN/INCONCLUSIVE。 |

Oracle 同时描述命令路由、变量提取、Signal outcome、Conclusion Gate 和允许差异。执行成功、Matcher false、ERROR、UNKNOWN、BLOCKED 必须是不同状态。

### 7. 反自洽和 Mutation 门禁

禁止仅把 Matcher 反向生成的字符串作为 `positive-realistic`。至少满足一项独立性证据：真实批准 Artifact、独立协议规范或专家手工证据；真实变体必须优先采用 Artifact。

机器门禁对 Fixture 施加以下 mutation：关键字删除/替换、阈值上下边界、stdout/stderr 互换、节点/路径改变、producer 缺失/多值/类型错误、参数变化、命令与 Signal 顺序变化。若 mutation 后错误实现仍得到相同业务结论，则标记 `insufficient_oracle`，不得进入 `validated`。

### 8. 生命周期和状态机

```text
draft → validated → approved → published
  │         │           │          ├→ stale → retired
  └─────────┴─────────── reject     └→ retired
```

- `draft`：编译产物，可重复生成，不可被 Runtime 使用。
- `validated`：schema、lint、scan、mutation、兼容性门禁通过，但尚无人审。
- `approved`：指定 KBD 专家和安全/测试角色完成职责分离审批。
- `published`：不可修改，已签名并上传内容寻址存储。
- `stale`：依赖变化或校准失败；历史 Run 可读，新 Run 禁止。
- `retired`：运营下线，保留审计和引用完整性。

拒绝不是可执行状态，记录在 approval/audit 中。任何“修订”都生成新 bundle revision/digest，不更新已发布字节。

### 9. 数据模型

建议 desired schema：

| 表 | 关键字段 | 责任 |
|---|---|---|
| `agent_test_scenario` | `id, support_id, kbd_revision, variant, status, input_fingerprint` | 描述可编译场景和能力缺口。 |
| `agent_test_fixture_bundle` | `id, scenario_id, revision, digest, schema_version, object_uri, size, status, signature` | Bundle 元数据与生命周期。 |
| `agent_test_fixture_dependency` | `bundle_id, dependency_type, dependency_id, revision, digest` | stale 反向依赖图。 |
| `agent_test_fixture_provenance` | `bundle_id, route_id, artifact_id, artifact_digest, transform_digest` | 输出来源和参数化链。 |
| `agent_test_fixture_approval` | `bundle_id, stage, actor_id, decision, comment, decided_at` | 机器/人工审批。 |
| `agent_test_fixture_audit` | `entity_type, entity_id, action, actor_id, trace_id, before, after, created_at` | 追加式审计。 |

表名最终需与现有 migration 命名规范复核；迁移必须幂等、先 desired schema 后增量，并提供向前修复而不是生产 down migration。`agent_test_run` 留给阶段 D。

### 10. Registry、对象存储与完整性

PostgreSQL 保存可查询 metadata；Bundle 和大 Artifact 保存至 OCI Registry 或具备 versioning/WORM 能力的对象存储。对象 URI 只能由服务端生成，客户端不能提交任意 URL。

发布事务采用 prepare/commit：上传临时对象并校验大小/digest/schema/signature，数据库以唯一 digest 提交 published metadata，再将对象标记不可变。读路径再次验证 digest、大小、schema、签名和状态；孤儿临时对象由定时任务回收。

### 11. Stale 依赖引擎

KBD revision、signals checksum、Tool/Policy/Compiler revision、Artifact 撤销、Runtime schema 支持和 differential 结果均是依赖节点。变更事件按反向索引把相关 Bundle 标为 stale；定时 reconciliation 扫描用于修复漏事件。

stale 操作需要原因码、依赖差异和 trace，且必须撤销“可创建新 Run”的资格，但不删除历史证据。重新编译、审批和发布后才恢复。

### 12. API、权限和人工审查

最小 API/CLI：resolve capability、compile、get preview/diff、validate、approve/reject、publish、mark stale/retire、list by support ID。所有 mutation 使用 idempotency key 和乐观锁。

RBAC 分为 compiler service、KBD expert、test/security approver、publisher、Runtime reader；同一 actor 不得同时生成并完成所有强制审批。审批页必须展示 provenance、参数化 diff、variant、mutation、secret/PII 扫描、capability gap 和 Bundle digest。

## 决策依据（WHY：为什么选此方案，为什么不选其他方案）

### 为什么采用离线编译和不可变 Bundle

运行时查询编辑中的 KBD/数据库会让同一命令随时间产生不同输出，也扩大 Runtime 权限和故障域。不可变 Bundle 把“决定测试语义”和“执行测试”分离，使回放、审计和回滚可重复。

### 为什么 metadata 与大对象分离

PostgreSQL 适合关系、状态和事务，不适合重复保存大量输出。内容寻址存储提供去重、完整性和不可变发布，同时数据库仍可高效查询 support ID、revision、状态和 owner。

### 为什么不能只由 Matcher 生成 Fixture

Matcher 和 witness 同源会产生循环论证：无论生产行为是否正确，测试都容易“自证通过”。真实 Artifact、near-miss 和 mutation 提供独立反证能力。

### 为什么 LLM/Compiler 只能自动生成 draft

参数化可能泄露客户身份，复杂 Matcher 也可能被错误解释。机器自动化负责规模和一致性，专家审批负责语义、安全与业务风险；未经审批不得发布。

### 为什么不在本阶段实现 Scheduler

Fixture 的可信性必须先独立闭环。如果编译、执行和调度一次引入，失败无法区分是数据、路由、Runtime 还是编排问题，也无法形成清晰的阶段门禁。

## 影响范围（哪些现行全量文档需要更新）

- `requirement/需求说明.md`：登记按 support ID 编译和能力缺口语义。
- `solution/架构设计.md`：增加 Compiler/Registry/Storage 控制面。
- `solution/接口设计.md`：增加 Bundle、审批、stale 和 API 契约。
- `solution/数据库设计.md`：增加 desired schema、索引和审计模型。
- `solution/安全设计.md`：增加 Artifact 脱敏、扫描、RBAC 和签名边界。
- `deploy/部署设计.md`：增加控制面、Registry/Object Storage 权限。
- `verify/测试指南.md`、`task/架构任务.md`：增加阶段 C 门禁。

## 验收标准

- KBD 27123 active revision 和至少 2 个不同结构 KBD 可生成可审查草稿；
- 输入指纹、依赖、provenance 和 Bundle digest 全链路可追溯；
- matcher-only 自洽样本、secret/PII 和不充分 oracle 无法发布；
- 状态机、RBAC、审批、不可变发布和 stale 传播通过；
- 相同输入幂等，失败不影响已有 published Bundle；
- 阶段 B Runtime 可只读加载已发布 Bundle 并验证完整性；
- 能力不支持时返回结构化 gap，而不是伪造可执行 Fixture；
- 阶段 D 可以仅凭 Registry/API 创建 TestRun，无需读取 KBD 编辑态数据。

## 关联任务与验证

- [阶段 C 任务](../../../task/hci-sim/events/hci-sim阶段C-Fixture编译与注册控制面任务.md)
- [阶段 C 验证](../../../verify/hci-sim/events/hci-sim阶段C-Fixture编译与注册控制面验证方案.md)

## 当前状态（2026-08-10）

Manifest/Bundle digest 和 Resolver 参考实现已落地，synthetic 23821 仅用于开发契约。生产 CAS、签名/批准工作流、完整 Artifact 和 capability owner 仍未闭环。
