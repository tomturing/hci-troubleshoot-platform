# 版本封板与 Staging 和 Prod 手动升级部署方案

> **⚠️ 本文档策略已于 2026-09-02 调整，此处保留为决策记录**
>
> 主干自动晋级已恢复为 **`dev` + `staging` 双环境**，`prod` 仍维持手动触发。
> 现行策略以 [deploy/gitops/README.md](../../deploy/gitops/README.md) 为准，调整说明见第 5 节。

## 1. 背景与目标

当前 `hci-troubleshoot-platform` 与 `hci-platform-env` 系统及业务功能已趋于稳定，现执行版本封板治理：
1. **自动交付收敛**：后续合并到 GitHub main 分支的 PR，CI 流水线仅自动构建并更新到 `dev` 环境。
2. **阻断自动晋级**：彻底解除 `staging` 和 `prod` 环境的自动触发机制，防止日常代码合入导致非生产预发布和生产环境被无预期覆盖。
3. **受控手动升级**：`staging` 和 `prod` 环境的升级部署转为**显式手动触发**（通过 GitHub Actions `workflow_dispatch` 或环境仓库同步流程）。

---

## 2. 第一性原理 (First Principles) 分析与架构设计

### 2.1 物理与逻辑底层事实
- **GitOps 控制流**：ArgoCD 监听 `hci-platform-env` 仓库的 `environments/<env>/values.yaml`。
- **CI 流水线驱动**：主干 PR 合并（`push: main`）触发 CI 流水线，原流水线向 `dev` 和 `staging` 同时推送新的镜像 Tag。
- **解耦推导**：
  - 要实现封板，只需将 `push` 事件的更新目标收敛为 `[dev]`。
  - 保留并规范 `workflow_dispatch` 的 `promote_target` 入口，支持在经过验证后显式升级 `staging`、`prod` 或指定环境集合。

### 2.2 多环境发布矩阵

| 触发场景 | CI 触发事件 | 影响环境 | 晋级方式 |
| :--- | :--- | :--- | :--- |
| **日常 PR 合并** | `push` (branches: main) | `dev` | 自动构建镜像并推送 `dev` values.yaml，ArgoCD 自动同源部署。 |
| **预发布环境升级** | `workflow_dispatch` (`promote_target=staging`) | `staging` | 手动选择目标为 staging，流水线写入 `staging` values.yaml，ArgoCD 自动同步。 |
| **生产环境升级** | `workflow_dispatch` (`promote_target=prod`) | `prod` | 手动选择目标为 prod，流水线写入 `prod` values.yaml，ArgoCD 手动 Sync 审核发布。 |
| **全量环境发布** | `workflow_dispatch` (`promote_target=all`) | `dev`, `staging`, `prod` | 手动批量触发各环境配置更新。 |

---

## 3. 对抗性审查 (Adversarial Review) 与防御加固

### 3.1 审查发现与加固措施
1. **防止 CI 依赖冲突**：
   - *风险*：`scripts/ci/test_resolve_release_baseline.py` 测试断言若绑定旧的 `dev/staging` 自动晋级逻辑，会导致 CI 构建失败。
   - *加固*：全面更新单测用例，验证主干 push 仅晋级 `dev`，且手动 dispatch 完整支持 `staging/prod`。
2. **防止误触生产**：
   - *风险*：操作人员误选 `all` 或 `prod` 导致未审计代码进入生产。
   - *加固*：`hci-platform-prod.yaml` 默认未配置 ArgoCD 自动同步（`syncPolicy.automated` 为空），即使 values.yaml 被写入，仍须在 ArgoCD 控制台进行二次人工核验与 Sync。
3. **仿真服务（hci-sim）协同封板**：
   - *风险*：`hci-sim` 使用独立 promotion PR 机制，若未同步修改，push main 仍会自动提 staging PR。
   - *加固*：将 `promote-hci-sim-dev` 的 `push` 目标精准收敛为 `dev`（`hci-sim-dev.yaml`），消除 staging 自动 PR 噪音。

---

## 4. 手动升级操作手册 (SOP)

### 4.1 Staging 环境手动升级
1. 进入 GitHub 仓库 Actions 页面，选择 **CI** 工作流。
2. 点击 **Run workflow**：
   - **Branch**：选择 `main`。
   - **手动发布目标 (`promote_target`)**：选择 `staging`（或 `both`）。
3. 运行完成后，流水线会自动将最新镜像 Tag 提交至 `hci-platform-env` 的 `environments/staging/values.yaml`，ArgoCD 将自动拉取并完成灰度滚动更新。

### 4.2 Prod 环境手动升级
1. 在 GitHub Actions 中手动运行 **CI** 工作流，`promote_target` 选择 `prod`（或 `all`）。
2. 流水线将镜像 Tag 写入 `environments/prod/values.yaml`。
3. 登录 ArgoCD 控制台，查看 `hci-platform-prod` Application 的 Diff，确认无误后点击 **Sync** 执行生产发布。

---

## 5. 后续调整：2026-09-02 恢复 staging 自动晋级

### 5.1 调整内容

撤销本文档第 2、3 节中「自动交付收敛为仅 `dev`」的部分，恢复主干双环境自动晋级：

| 触发场景 | 调整前（PR #989 封板） | 调整后（本次） |
| :--- | :--- | :--- |
| **日常 PR 合并（`push: main`）** | 仅 `dev` | `dev` + `staging` |
| **`workflow_dispatch` 手动晋级** | `dev` / `staging` / `prod` / `both` / `all` | 保持不变 |
| **`prod` 自动晋级** | 无（手动） | **仍无**（维持手动，ArgoCD 端不做自动 Sync） |

### 5.2 调整理由

1. **封板成本高于收益**：`staging` 的定位是预发布验证环境，自动跟随主干才能持续暴露主干回归；
   改为手动后，验证滞后且依赖人工记忆触发，`staging` 逐步与主干脱节。
2. **风险边界未被削弱**：`prod` 的防护不依赖 CI 侧的晋级收敛，而由 `hci-platform-prod.yaml`
   不配置 `syncPolicy.automated` 保证——即使 tag 被写入，仍需 ArgoCD 人工 Sync。因此放开
   `staging` 不引入生产风险。
3. **封板目标可按需恢复**：如需临时止血，仍可通过 `workflow_dispatch` 精确控制晋级范围，
   无需长期牺牲 `staging` 的自动验证能力。

### 5.3 防回归加固

`scripts/ci/test_resolve_release_baseline.py` 新增
`test_main_push_auto_promotes_dev_and_staging`，按 **push 分支代码块**（而非子串）断言
`ENVIRONMENTS=(dev staging)` 与 hci-sim 双 ArgoCD Application 晋级，
避免再次被单环境收敛时因命中手动 `case` 分支而漏过。
