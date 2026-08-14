---
status: in_progress
category: verify
audience: developer, tester, operator, expert, security
last_updated: 2026-08-10
owner: team
---

# hci-sim 阶段 C：Fixture 编译与注册控制面验证方案

> **证据状态：pending。** 本文是 Fixture Compiler/Registry 的验收计划，不是完成报告。当前对应表和服务尚不存在；在阶段 B 通过且本方案获得真实证据前，所有结论均为 pending。

关联[需求](../../requirement/hci-sim/events/hci-sim阶段C-Fixture编译与注册控制面需求.md)、[方案](../../solution/hci-sim/events/hci-sim阶段C-Fixture编译与注册控制面方案.md)和[任务](../../task/hci-sim/events/hci-sim阶段C-Fixture编译与注册控制面任务.md)。

## 验证目标

证明指定 support ID 能解析为不可变、可追溯输入，并在独立证据、Mutation、安全扫描、专家审批和完整性门禁下生成 published Bundle；不支持的 KBD 必须返回能力缺口，不能生成“自洽但无效”的 Fixture。

## 验证范围与样本

必选 KBD 27123 当前 active revision，再选择至少 2 个在 Tool、Matcher、变量依赖或输出形态上不同的 published KBD。样本选择、revision 和选择理由在执行前冻结；不能在失败后替换为更容易 KBD。范围包括 Resolver、Compiler、Registry、DB/storage、RBAC/stale 和阶段 B Runtime compatibility，不包括 Scheduler/TestRun。

## 环境矩阵

| 环境 | 配置 | 用途 |
|---|---|---|
| Compiler unit | 固定 KBD/Tool/Artifact fixtures | 可复现、mutation、property tests |
| Integration | PostgreSQL + OCI/Object Store + signer/scanner | 状态、事务、完整性、RBAC |
| KBD staging snapshot | 精确 revision 的只读副本 | active/指定 revision 解析和 stale |
| Runtime compatibility | 阶段 B 镜像 + published Bundle | schema/digest/route 执行 |
| Security corpus | 合成 secret/PII/客户身份样本 | 发布阻断和脱敏 |

记录 DB migration version、Compiler/Runtime SHA、scanner规则、KBD/Tool/Policy/Artifact digests 和存储版本。

## 用例矩阵

| ID | 前置/步骤 | 期望 | 必需证据 |
|---|---|---|---|
| VC-01 | 用 KBD 27123 `active` 和精确 revision resolve | active 被锁定为精确 revision/checksum；结果可重放 | resolve JSON/hash |
| VC-02 | 测不存在、未发布、空 signals、缺 Tool/Policy、未批 Artifact | 结构化 capability gap，未启动可发布编译 | API/error/audit |
| VC-03 | resolve 后改变 active 指针再完成编译 | 本作业仍用锁定输入；旧结果按依赖规则 stale | job/dependency trace |
| VC-04 | 同输入指纹并发/重复提交 | 复用作业或相同 digest；无重复/冲突 revision | DB/query/concurrency log |
| VC-05 | 执行生产 Tool Compiler 与 Fixture Compiler contract 对照 | command/argv/acquisition/revision 一致，无第二套规则 | contract report |
| VC-06 | 构造正常、循环、缺失、多 producer、类型/基数冲突变量图 | 正常图稳定；非法图形成明确 gap/validation error | graph artifacts |
| VC-07 | 对真实批准 Artifact 参数化主机/IP/路径并 dry-run | 客户身份消失，output shape/Matcher 语义保持 | before/after diff + hashes |
| VC-08 | 仅提供图片/OCR 或无 provenance 文本 | 只能成为草稿辅助/能力缺口，不能机器发布 | lifecycle/audit |
| VC-09 | 为三个 KBD 生成适用 variants 与 Oracle | positive/negative/near-miss/error/unknown 语义区分 | variant matrix |
| VC-10 | 用 Matcher 反向生成唯一 positive，不提供独立证据 | `insufficient_oracle`，不能 validated/published | validator report |
| VC-11 | 运行关键字/阈值/stream/node/path/producer/参数/顺序 mutation | 错误等价或过宽 Matcher 被检出并阻断 | mutation report |
| VC-12 | 注入 token、私钥、密码、客户 IP/主机/标识 | scanner/参数化阻断发布，事件可审计 | scan result（脱敏） |
| VC-13 | 尝试自动 publish、同人自审自批、越权读草稿/审批 | RBAC/职责分离拒绝；无状态副作用 | auth/audit logs |
| VC-14 | 遍历合法/非法生命周期转换并并发 publish | 仅合法 CAS 转换成功；published 不可改 | state/DB constraints |
| VC-15 | 篡改已发布对象、URI、size、schema、signature、digest | Registry/Runtime fail closed 并隔离对象 | tamper evidence |
| VC-16 | 让上传或 DB commit 在 prepare/commit 各点失败 | published 指针不损坏；临时孤儿可回收 | fault injection logs |
| VC-17 | 修改 KBD/Signal/Tool/Policy/Artifact/Runtime compatibility | 相关 Bundle stale，原因精确，新 Run 资格移除 | dependency graph/events |
| VC-18 | 丢弃 stale 事件后运行 reconciliation | 漏标被修复，重复执行幂等 | reconciliation report |
| VC-19 | 空库/已有库/重复执行 migration，并模拟失败恢复 | desired schema 一致，无数据丢失，向前修复可用 | schema diff/backup |
| VC-20 | 阶段 B Runtime 只读加载 published、draft、stale Bundle | 只允许 published 且完整性通过；正确 route 命中 | Runtime logs/trace |
| VC-21 | 编译 27123 与两个异构 KBD，专家预览全部信息 | provenance、diff、变量图、variants、scan、mutation、gap 可审 | review sign-off |
| VC-22 | Compiler 失败后查询既有 published Bundle | 旧 Bundle 字节/状态/引用不变 | before/after digest |

## 对抗性审查

- 在字符串分片、base64、stderr、多行和 Unicode 中隐藏 secret/PII，验证扫描和人工预览。
- 制造 checksum 相同但 revision 元数据不同、对象 URI 重定向、TOCTOU active pointer 和并发审批。
- 让 Matcher 对所有输出返回 true，确认 negative/near-miss/mutation 能拒绝。
- 撤销 Artifact 审批但阻断事件投递，确认 reconciliation 最终 stale。
- 用 Runtime reader 尝试 list draft、写 Registry、读取原始 Artifact，必须由身份/网络/存储策略共同拒绝。

## 证据模板

```yaml
phase: C
verdict: pending|pass|fail|blocked
compiler_sha: ""
runtime_image: ""
db_schema_version: ""
samples:
  - support_id: "27123"
    kbd_revision: null
    input_fingerprint: ""
    bundle_digest: ""
    result: pending
cases:
  VC-01: {result: pending, evidence: []}
approvals: {kbd_expert: "", qa: "", security: "", operator: ""}
```

真实 Artifact 不进入普通测试报告；报告只保存批准 ID、脱敏差异、digest 和受控对象引用。

## 退出标准

- VC-01～VC-22 全部 PASS；三 KBD 样本不可在执行后选择性替换。
- matcher-only、secret/PII、越权、篡改和非法状态转换零绕过。
- published Bundle 不可变、stale 最终一致、失败不覆盖已有发布。
- 阶段 B Runtime compatibility 通过，Runtime 权限最小化。
- KBD expert、QA、Security、DB/Operator 签署阶段 D Go。

## 当前状态（2026-08-10）

Manifest digest、synthetic bootstrap 和 capability 参考契约为 `passed`；生产 CAS/Registry、approved Artifact、批量 capability report 和迁移运行证据为 `pending/capability_gap`。

## 失败分类与文档更新

失败分类：`resolve_gap`、`contract_drift`、`graph_invalid`、`parameterization_loss`、`oracle_tautology`、`mutation_escape`、`data_leak`、`rbac_bypass`、`lifecycle_race`、`storage_integrity`、`stale_miss`、`migration_failure`、`runtime_incompatible`。完成后同步数据库、接口、安全、部署、架构和测试指南；能力 gap 必须进入现行产品事实，不得仅藏在本事件报告。
