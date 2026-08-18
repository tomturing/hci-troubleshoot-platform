---
status: active
category: verify
audience: architect, developer, tester, operator
last_updated: 2026-08-14
owner: team
---

# HCI 仿真系统 (hci_sim) 架构设计评估与构建机制分析报告

> **显性使用声明**：本报告在对 `hci-troubleshoot-platform` 的 `hci_sim` 模块进行梳理与分析时，**统一且显性地使用了第一性原理 (First Principles) 与对抗性审查 (Adversarial Review)**。

---

## 1. 核心诊断与架构设计评估

### 1.1 第一性原理拆解 (First Principles)
排障仿真系统 (Simulator) 的物理与逻辑底层事实包含四个核心维度：
1. **适配性 (Fidelity)**：能够真实反应 HCI 系统的交互行为，让 Agent 像面对真实集群一样执行工具。
2. **泛化性 (Generativity)**：大语言模型 (LLM) 的探索行为具备概率性，系统必须容忍命令行空格、同义参数顺序或动态 PID 变体。
3. **维护性 (Maintainability)**：生成 100+ 场景的成本必须尽量低，不能陷入穷举命令字符串的泥潭。
4. **技术一致性 (Alignment)**：仿真逻辑必须与平台主栈（Python/FastAPI/`uv`）的契约实现无缝共享。

### 1.2 对抗性审查 (Adversarial Review)
- **异构技术栈漂移 (Go vs Python)**：主框架全为 Python，而 `hci_sim` 采用 Go 重写了 SSH Server、Lexer 与查表算法。这导致 Python 侧改动工具契约时，Go 端必须手动同步，极易发生隐蔽的契约漂移 (Contract Drift)。
- **“死板查表器”与 LLM 概率探索的冲突**：现行逻辑要求 `RouteKey` 完全精确对齐（`variant + tool + acquisition_key + argv + node + container`）。Agent 一旦产生命令变体，系统直接 Fail Closed (`fixture_not_found`)，导致排障测试退化为“测试 Agent 能否死记硬背出固定的命令文本”。
- **工程重心错位 (Over-Engineering)**：在非核心矛盾（Go SSH Server、独立 PostgreSQL 15 张表、HMAC Lease 签名、Strict JSON EOF）上过度工程化，却在“如何高保真、低成本地生成仿真环境”上缺乏突破。

---

## 2. 现行 `hci_sim` 源码目录与代码分工全景

在 [hci_sim](file:///aihci/hci-troubleshoot-platform/hci_sim) 目录下，源码及组件结构如下：

### 2.1 `cmd/` (二进制程序入口)
- **[cmd/hci-sim/](file:///aihci/hci-troubleshoot-platform/hci_sim/cmd/hci-sim)**：
  - `main.go` / `bootstrap.go`：主服务入口。同时启动 **SSH 端口 (`:2222`)**（数据面）与 **HTTP 端口 (`:8080`)**（控制面 REST API）。负责数据库连接与调和任务初始化。
  - `offline_manifest.go`：CLI 命令行工具，用于计算 Manifest Digest (`sha256:...`) 或在线下签发测试租约。
- **[cmd/hci-sim-smoke/](file:///aihci/hci-troubleshoot-platform/hci_sim/cmd/hci-sim-smoke)**：
  - 无浏览器三段链路 (Bridge WebSocket -> SSH -> `hci-sim`) 冒烟测试 CLI 工具。

### 2.2 `internal/` (核心内核逻辑包)
- **[internal/server/](file:///aihci/hci-troubleshoot-platform/hci_sim/internal/server/server.go)**：数据面 Custom Go SSH 服务器，处理端口 2222 的 `exec` 请求、PTY 仿真、环境透传（如 `TRACEPARENT`）。
- **[internal/controlplane/](file:///aihci/hci-troubleshoot-platform/hci_sim/internal/controlplane/controlplane.go)**：控制面内核。管理 TestRun 生命周期、Bundle 离线编译与双角色审批。
- **[internal/fixture/](file:///aihci/hci-troubleshoot-platform/hci_sim/internal/fixture/fixture.go)**：加载 JSON Manifest v2，执行精确 `RouteKey` 查表与响应渲染、Fault 注入。
- **[internal/lease/](file:///aihci/hci-troubleshoot-platform/hci_sim/internal/lease/lease.go)**：`htp2` HMAC-SHA256 短时租约生成与配额强校验。
- **[internal/database/](file:///aihci/hci-troubleshoot-platform/hci_sim/internal/database/run_repository.go)**：PostgreSQL 独立数据库持久化层。
- **[internal/reconciler/](file:///aihci/hci-troubleshoot-platform/hci_sim/internal/reconciler/reconciler.go)**：Durable Outbox 后台异步事件调和器。
- **[internal/metrics/](file:///aihci/hci-troubleshoot-platform/hci_sim/internal/metrics)** & **[telemetry/](file:///aihci/hci-troubleshoot-platform/hci_sim/internal/telemetry)**：导出 Prometheus 指标与 OpenTelemetry Span 追踪。

---

## 3. 独立数据库 `hci_sim` 15 张表全景盘点

基于迁移脚本 [000001_control_plane.sql](file:///aihci/hci-troubleshoot-platform/database/hci-sim-migrations/000001_control_plane.sql)，独立数据库 `hci_sim` 划分为 4 大 Schema、共 15 张数据表：

### 3.1 `control_plane` Schema（控制面运行与状态跟踪，7 张表）
1. **`control_plane.scenario`**：测试场景表。绑定工单 `support_id` 与 KBD Revision。
2. **`control_plane.run`**：仿真 TestRun 实例表。保存外部追踪 ID、Bundle 摘要与执行状态。
3. **`control_plane.run_attempt`**：重试尝试表。记录多轮连接尝试与 JTI Hash。
4. **`control_plane.run_event`**：运行过程事件时间线表。
5. **`control_plane.run_result`**：最终测试结论归档表。
6. **`control_plane.run_outbox`**：Transactional Outbox 异步通知表。
7. **`control_plane.runtime_instance`**：Pod 运行节点注册与心跳容量管理表。

### 3.2 `fixture` Schema（编译产物与版本依赖管理，5 张表）
8. **`fixture.bundle`**：不可变 Bundle 元数据与对象存储 URI 映射表。
9. **`fixture.dependency`**：Bundle 依赖的 Tool/Policy/Snapshot 版本追踪表。
10. **`fixture.provenance`**：路由数据与真实原始 Artifact 的血统映射表。
11. **`fixture.approval`**：Bundle 发布前的双角色审批记录表。
12. **`fixture.stale_outbox`**：当底层的依赖发生变化时自动发出的失效通知 Outbox 表。

### 3.3 `artifact` Schema（原始数据与脱敏审计，3 张表）
13. **`artifact.metadata`**：原始测试包的元数据与脱敏 Hash 记录表。
14. **`artifact.scan`**：自动安全扫描（Secret/PII/License/Schema）校验记录表。
15. **`artifact.approval`**：原始数据上线前的安全/专家审批记录表。

### 3.4 `audit` Schema（审计日志，1 张表）
16. **`audit.entity_event`**：实体变更的前后 Snapshot 审计日志表。

---

## 4. 运行机制实例：指定 KBD40061 构建仿真环境

以真实案例 **KBD40061**（检查 `/sf/log` 分区磁盘使用率 `df`）为例，说明整套机制的运转流程与数据落盘位置。

### 4.1 构建流转生命周期
1. **阶段 0（知识发布，手动）**：专家在后台发布 KBD40061，写入主库 `hci_troubleshoot.dynamic_resource_active` 表作为不可变事实源。
2. **阶段 1（权威解析，全自动）**：控制面自动调用 [hci_sim_resolver.py](file:///aihci/hci-troubleshoot-platform/backend/kb-service/app/services/hci_sim_resolver.py)，从 active 快照解析出合成路由 `synthetic_routes`（`tool="qfk_system"`, `argv=["df", "/sf/log"]`）。
3. **阶段 2（Bundle 编译与签名，全自动）**：[bootstrap.go](file:///aihci/hci-troubleshoot-platform/hci_sim/cmd/hci-sim/bootstrap.go) 编译生成 `positive-minimal` 级别的 `fixture-manifest.json`，并计算 HMAC-SHA256 签名。
4. **阶段 3（租约签发，全自动）**：签发 TTL=15m 的 `htp2` HMAC 签名 Token，生成 `connection.json`（密码自动脱敏），并在数据库 `hci_sim.control_plane.run` 中插入记录 (状态为 `leased`)。
5. **阶段 4（容器拉起，全自动）**：自动拉起 `hci-sim` Pod，挂载 `fixture-manifest.json`，在 `:2222` 端口暴露 SSH 数据面。
6. **阶段 5（诊断执行与结果归档，全自动）**：Agent 连入并执行 `df` 命令，系统输出伪造日志；数据回传至 `control_plane.run_result` 表。

### 4.2 全景数据流转与存储映射表

| 阶段 | 产生的数据项 | 物理存储位置 | 自动化程度 |
|---|---|---|---|
| **0. 知识准备** | KBD40061 契约快照 | PostgreSQL 主库 (`hci_troubleshoot.dynamic_resource_active`) | 手动审核发布 |
| **1. 权威解析** | 合成路由 `synthetic_routes` | 内存 API JSON 响应 | 全自动化 |
| **2. Bundle 编译** | `fixture-manifest.json` | ① 磁盘 `/.hci-sim-run/40061-*/`<br>② S3/MinIO 对象存储<br>③ 独立库 `hci_sim.fixture.bundle` | 全自动化 |
| **3. 租约签发** | `connection.json` & `htp2` Token | 本地 `/.hci-sim-run/40061-*/connection.json` (权限 0600) | 全自动化 |
| **3. 运行记录** | TestRun 实例数据 | 独立库 `hci_sim.control_plane.run` (状态 `leased`) | 全自动化 |
| **4. 容器拉起** | SSH 2222 端口与实例心跳 | K3s 节点容器 / 独立库 `hci_sim.control_plane.runtime_instance` | 全自动化 |
| **5. 诊断执行** | OpenTelemetry 追踪 Span | OTel Collector / Tempo (`trace_id`) | 全自动化 |
| **6. 结果归档** | Run 判定结论与最终报告 | 独立库 `hci_sim.control_plane.run_result` | 全自动化 |
