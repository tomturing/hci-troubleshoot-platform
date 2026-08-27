# Signal 试运行全链路缺陷修复与 Bundle 激活基线优化方案

## 1. 背景与问题定位

在 Signal 试运行（Dry-Run）及 Bundle 验证资产保存链路中，发现了 5 个深层逻辑缺陷：

1. **Bug #1（KbdReviewView.vue）**：`:kbd-revision` 混用 `working_revision_id`（string 类型），导致严格相等 `===` 永远失败，仿真测试（Bundle 资产）来源 100% 无法加载。
2. **Bug #2（SignalDryRunDialog.vue）**：`saveToBundle` 缺少 `kbd_revision` 过滤，跨版本历史 draft 导致误报 `当前 KBD 存在多个 Draft`。
3. **Bug #3（SignalDryRunDialog.vue）**：自动创建 Draft 时未透传 `kbd_revision`，导致资产绑定错误版本。
4. **Bug #4（bundle_registry.go / controlplane.go）**：`ReviseDraft` 在创建新 Draft 后未将父 Draft 降级为 `stale`，导致连续试运行保存时产生多个 Draft，第 2 次保存必定失败。
5. **Bug #5（SignalDryRunDialog.vue）**：数据源读取在存在多个 Published Bundle 时直接报错，且未优先对齐当前运行时实际生效（Active）的 Bundle。

## 2. 核心架构优化

### 2.1 ReviseDraft 父 Draft 降级为 Stale
- 专家试运行保存或修改 Bundle 生成新 Draft 后，将旧 Draft 自动置为 `stale`（`stale_reason = "superseded_by_revision:" + newDigest`）。
- 保证控制面 `List()` 返回的 `draft` 状态 Bundle 始终保持唯一，满足覆盖/迭代写入的闭环。

### 2.2 仿真测试数据源读取优先对齐 Active 指针
- 仿真测试数据源读取不仅按 `kbd_revision` 过滤，还优先匹配 `/v1/control-plane/activations/{support_id}` 中 `active_digest`。
- 在发生线上回滚、新版本未激活或多版本并存时，确保专家调试读取的是当前真正承接流量的仿真基线；在无激活记录时平滑降级为最新 published bundle。

## 3. 验证与回归测试
- `hci_sim/cmd/hci-sim/controlplane_api_test.go`:
  - `TestReviseDraftStalesParentDraft`: 验证连续保存产生新 Draft 时父 Draft 自动 stale 降级。
  - `TestThreeSignalsCompleteBundle`: 验证 3 个信号分别调试、多次试运行保存后，最终 Draft 聚合全部 3 个信号的输出。
