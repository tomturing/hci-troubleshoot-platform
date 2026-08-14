---
status: accepted
category: solution
audience: product, architect, developer, qa, sre
last_updated: 2026-08-11
owner: team
---

# hci-sim 生产化差距审查与 Bundle 工厂化重构基线

## 1. 最终结论

当前实现已经完成 KBD 27123 revision 25 的真实纵向样板，但尚未形成可规模化交付的 hci-sim 产品。

```text
KBD 27123 纵向样板：PASS
hci-sim 面向终端用户生产交付：BLOCKED
```

这不是单一浏览器、K3s 或 GitHub 故障。总体方向正确，但 KBD 扩展模型、权威事实管理、跨服务契约、发布可复现性和规模化验收仍不完整；具体实现中的多项缺陷是这些系统性缺口的外显结果。后续不得把一个 KBD 的成功外推为全部 KBD 可测。

## 2. 已证明的纵向基线

27123 已真实通过以下链路：

```text
Admin UI 环境构建
  → API Gateway
  → hci_sim Scenario + published Bundle + Run
  → 真实 Case / Conversation
  → Agent 权威 sim-ssh context
  → K3s terminal_bridge
  → K3s hci-sim
  → Agent 发起 task / lsof / ps
  → passed Result
```

现场证据：

| 项目 | 结果 |
|---|---|
| Revision/Bundle | revision 25；published；Runtime、Run、数据库 digest 一致 |
| Run | `run-27123-2527ff1a376851c56e0b3e11`，passed |
| Case/Conversation | 真实 `Q...` Case 与 Conversation，可按 trace/exec_id 关联 |
| Agent 执行 | 3 条 Agent 命令，task/lsof/ps，失败数 0 |
| Result | 页面、Result API、Run、RunResult 均为 passed |
| 运行依赖 | 不依赖 Custom UI、Windows/Linux Docker Bridge 或真实 HCI |

该证据只证明一条 production-like vertical slice，不证明 Bundle 批量生产、全 KBD 覆盖、容量、灾备或真实 HCI 等价性。

## 3. 问题归因

| 层级 | 判断 | 证据 |
|---|---|---|
| 总体边界 | 基本正确 | 独立数据库、受管 Bridge、TestRun context、Admin 两阶段均解决真实问题 |
| KBD 扩展模型 | 严重设计缺口 | 新 KBD 仍可能牵涉 Manifest、Catalog、服务镜像、Helm、Argo 和数据库多处修改 |
| 权威事实模型 | 原设计不完整，已修 27123 路径 | Runtime 曾可执行 Bundle，但 `fixture.bundle` 无记录 |
| 结果状态机 | 原设计存在假阳性，已修 | SSH Ready、exit code 0、固定 smoke 曾可能被误当 Agent 通过 |
| 跨服务契约 | 实现与门禁薄弱 | `exit_code=0` 省略、DNS 误判、argv 漂移、Result 语义不一致 |
| 容器与发布 | 未生产化 | 源码有 Catalog 但镜像缺失；现场曾需临时 revision/digest/branch |
| 规模化验收 | 尚未建立 | 当前 capability matrix 中除 27123 外的 published KBD 仍无 published Bundle |

因此不能把问题归结为“小 Bug 太多”，也不能据此否定 K3s Bridge 或数据库隔离。真正需要重构的是 KBD 从知识数据到可运行 Bundle 的生产、注册、分发和验收模型。

## 4. 为什么问题集中出现

1. 首次真实 Agent E2E 建立得晚，过去的 HTTP 200、Pod Ready、SSH Ready 和固定 smoke 只证明局部组件。
2. Bundle 曾同时以镜像 Manifest、Helm revision、Runtime 内存和数据库 metadata 表示，缺少强制单一事实源。
3. Admin、Gateway、Conversation、Agent、Bridge 和 Runtime 都使用独立契约，但缺少容器级 consumer-driven contract gate。
4. 新 KBD 仍接近一次应用发布，而不是一次受控数据发布，人工步骤过多且无法原子化。
5. 开发源码、构建上下文、容器文件和 GitOps 环境之间缺少统一兼容性清单和干净环境重建门禁。

## 5. 必须保留与必须重构

### 5.1 保留

- K3s 受管 `terminal_bridge`；
- 独立数据库 `hci_sim`；
- TestRun 与 Agent `sim-ssh` context 显式绑定；
- Admin UI“环境构建 → 开始测试”两阶段；
- `passed/failed/inconclusive` 显式结果；
- 禁止回退 Custom UI、本地 Bridge 或真实 HCI。

### 5.2 重构

- 将 KBD onboarding 从“修改并发布应用”改为“编译并发布不可变 Bundle”；
- 建立唯一 Bundle Registry：数据库保存 metadata，对象存储保存不可变内容，Runtime 按 digest 加载；
- 建立统一 Bundle Compiler，验证 revision、Signal/Route、Catalog、argv、timeout、输出、变体、digest、provenance 和审批；
- 把 Build、Bundle 注册、调度、Lease、Case/context 绑定和 Result 关闭收敛为服务端工作流；
- 建立真实容器和干净 GitOps 环境中的自动 E2E，不再依赖人工 Argo patch；
- 新增 KBD 时禁止修改 Agent/hci-sim 代码、Dockerfile、Helm、Argo Application 或服务镜像 digest。

## 6. 下一阶段目标架构

```text
KBD published revision
  → Bundle Compiler
  → completeness / policy / contract gates
  → immutable Artifact + metadata transaction
  → Bundle Registry
  → hci-sim digest loader/cache
  → ephemeral TestRun
  → Agent E2E oracle
  → publish or reject
```

核心不变量：

1. 同一 Bundle digest 是编译、Registry、Runtime、Run 和审计的唯一身份。
2. Bundle metadata 与发布状态必须先持久化，Runtime 不从镜像内隐藏事实推断 capability。
3. 新 KBD 是数据发布，不触发业务服务镜像构建。
4. 任一数据库、对象、契约或 E2E 校验失败均 fail closed，不输出伪 capability gap 或伪 passed。
5. 浏览器只提交 KBD ID、标题、描述和用户消息；基础设施细节由服务端自动处理。

## 7. 下一会话实施入口

### P0：生产契约

- 定义 Bundle v1 schema、Registry API、状态机和兼容性策略；
- 明确 KBD revision → Bundle digest 的唯一映射与审批/撤销规则；
- 定义 Compiler 输入、三类结果变体和错误分类；
- 将当前镜像内 27123 Bundle 转为首个 Registry gold artifact；
- 增加“不改代码接入第二个 KBD”的架构验收测试。

### P1：动态分发与自动验收

- 实现对象存储/Registry、Runtime digest loader、缓存和完整性校验；
- 实现服务端 Build/TestRun 编排和幂等恢复；
- 建立容器级 Contract/E2E：positive、negative、inconclusive；
- 在干净 K3s 环境从 `main + env main` 自动部署并运行。

### P2：规模化与生产门禁

- 所有 published KBD 自动进入 capability matrix；
- 选取至少 3 个结构不同的 gold KBD 做三变体 E2E；
- 补充并发、重启、Lease 过期、数据库/对象存储故障和回滚验证；
- 建立构建时延、成功率、错误分类、Bundle age 和回归结果 SLO；
- 完成主库空 `agent_test_*` 表的独立 contract/drop 与恢复演练。

## 8. 生产放行标准

以下条件全部满足前，产品状态保持 `BLOCKED`：

- 新 KBD 无需代码、镜像、Helm 或 Argo 变更；
- Bundle 只有一个权威 digest，所有消费者精确一致；
- 至少 3 个差异化 KBD 的三类结果自动 E2E 通过；
- published KBD capability matrix 可自动执行且错误不被吞没；
- 干净集群部署、重复运行、重启和回滚不依赖人工修数据或 patch；
- 用户只感知“环境构建”和“开始测试”，失败可直接定位到 KBD、Bundle、Agent、Bridge、Runtime 或 Result 层。

## 9. 决策记录

- 接受 27123 作为纵向基线和回归 gold case；
- 不接受“27123 通过”等价于“hci-sim 可生产交付”；
- 当前 PR 只收口纵向样板、系统性假阳性和证据文档；
- Bundle 工厂化作为下一阶段独立重构，由本文件提供事实、边界和完成定义。
