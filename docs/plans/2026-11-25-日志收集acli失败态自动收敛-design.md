---
status: active
category: solution
audience: developer
last_updated: 2026-11-25
owner: hci-dev
---

# 2026-11-25 日志收集 acli 失败态自动收敛

## 变更历史

| 日期 | 版本 | 变更摘要 | 关联 |
|------|------|---------|------|
| 2026-11-25 | v1.0 | 新建：同节点探测确认 acli 恢复后，自动清除失败卡片与常驻红条；复查改为短间隔快速收敛 | 工单 Q2026112501114 |

## 背景与目标

工单 `Q2026112501114` 的 acli 安装后验证失败，根因是节点 `/sf/log/today` 悬空软链导致 `acli --version` 返回 `EEXIST`。节点恢复后，用户点击「重新探测」仍看到失败卡片和顶部常驻红条，产生「探测成功但报错还在」的矛盾。

根因不是探测本身，而是两条状态没有回流：

1. `acliFailure` 是人工确认态，只在重试成功、换单或点「知道了」时清除；`prepare()/probeOnce()` 成功只更新 `probeResult`，不清失败态。
2. 页面 5 秒自动刷新只同步任务/节点进度，不探测；探测是另一套 30 秒 × 3 次的事实复查，且复查成功也不清失败态。

目标：

- 同一节点探测确认 acli 可用后，自动清卡片与对应常驻红条；
- 手动「重新探测」与自动事实复查两条链路都生效；
- 复查改为短间隔多次确认，恢复即停，避免 30 秒等待和长期轮询；
- 不同节点的探测成功不得误清其他节点的失败证据。

非目标：

- 不改后端、不改 acli 安装/验证判定；
- 不把「重新探测」升级为逐台探测集群所有节点；
- 不清理其他类型错误的 ElMessage。

## 详细设计

### 1. 失败态增加节点归属与 toast 句柄

文件：`frontend/customer/src/components/LogCollectPanel.vue`

`acliFailure` 扩展为：

```ts
type AcliFailureState = {
  message: string
  stage: string
  suggestions: AcliRetryStage[]
  facts: AcliSyncFacts
  nodeIp: string
  toast?: { close: () => void } | null
}
```

落卡入口统一走 `recordAcliFailure(data, fallbackMessage, nodeIp)`：

- 节点来源优先级：本次 `acli_sync` 请求发起时记录的 `acliSyncTargetNode` → `data.node_ip` → 当前 `nodeIp.value`；
- `acliSyncTargetNode` 在每次发送 `acli_sync` 前写入，避免迟到回包被误算到新连接的节点；
- 常驻红条改用 `showCustomerFault()` 返回的 message handler，保存到 `acliFailure.toast`，不用 `ElMessage.closeAll()`，避免误关其他错误。

### 2. 探测成功对账

文件：`frontend/customer/src/components/LogCollectPanel.vue`

`probeOnce()` 在 `probeResult.value = data` 之后调用：

```ts
reconcileAcliFailure(data, targetNode)
```

判定条件必须全部满足才清除：

1. 当前存在 `acliFailure`；
2. `acliFailure.nodeIp === targetNode`；
3. `data.acli_installed === true`；
4. `data.acli_version` 非空。

清除动作：

- 调用 `acliFailure.toast?.close()`；
- `acliFailure.value = null`；
- `resetAcliLive()`，避免旧安装失败输出继续留在面板；
- `stopFactRecheck()`，恢复后不再安排后续复查。

### 3. 复查节奏改为短间隔快速收敛

文件：`frontend/customer/src/components/LogCollectPanel.vue`

将固定 `30s × 3` 改为序列：

```ts
const PROBE_RECHECK_DELAYS = [3000, 5000, 8000]
```

- 每次间隔加 ±20% 抖动，避免多客户端同相位打同一节点；
- 第 N 次复查延迟取 `PROBE_RECHECK_DELAYS[count]`，用完后停止；
- 复查仍走 `prepare({ silent: true, retries: false, resetRecheck: false })`；
- 成功后 `reconcileAcliFailure` 会停止后续复查；
- 3 次仍异常则停止，不无限轮询，并在失败卡片上追加「已自动复查 3 次仍未恢复」；
- 手动「重新探测」、换单、换节点会重置计数，重新给 3 次短复查机会；
- 采集中仍记 `recheckPendingAfterRun`，终态补做一次。

### 4. 节点切换语义

按确认口径「只管当前节点」：

- 切换到不同 SSH 节点时**不清**旧节点失败卡片；
- 在新节点探测成功不会清除旧节点卡片（节点归属不匹配）；
- 切回原节点且探测成功才清除。

### 5. 文案

3 次复查均失败时，卡片提示追加：

```text
已自动复查 3 次仍未恢复，请确认节点环境后重试。
```

不改动现有重试建议与错误码展示。

## 数据流

```text
acli_sync 失败
  → recordAcliFailure(data, nodeIp, toast)
  → acliFailure{nodeIp, facts, toast}

用户点「重新探测」/ 30s 事实复查（改为 3/5/8s）
  → prepare()
  → probeOnce(targetNode)
  → probeResult = data
  → reconcileAcliFailure(data, targetNode)
      ├─ 节点不同 / acli 未恢复 → 保留
      └─ 同节点 + acli 可用 → 关 toast + 清卡片 + 停止复查
```

## 边界与对抗性审查

- **迟到回包**：`acli_sync_result` 可能在用户切换节点后到达。用发送时快照 `acliSyncTargetNode`，不拿回包时的 `nodeIp.value`，避免误归属。
- **同节点但只有旧版本**：要求 `acli_installed=true` 且 `acli_version` 非空，不因 `acli_binary` 存在就误判恢复。
- **其他错误 toast**：只关闭 `acliFailure.toast` 句柄，不调用 `closeAll()`。
- **复查风暴**：最多 3 次、短间隔、带抖动；恢复即停；采集中不探测。
- **失败证据丢失**：不同节点不清；3 次复查失败仍保留卡片，并明确提示已自动复查。

## 测试与验收

单测（`frontend/customer/src/components/__tests__/LogCollectPanel.spec.ts`）：

1. 同节点失败后重探成功 → 卡片消失、对应 toast 关闭；
2. 不同节点重探成功 → 原节点卡片保留；
3. 自动事实复查成功 → 卡片与红条自动消失；
4. 复查间隔为 3/5/8 秒且恢复后不再安排下一次；
5. 3 次复查仍失败 → 卡片保留且出现「已自动复查 3 次仍未恢复」；
6. 原有「常驻错误提示不自动关闭」用例保持通过。

验收：

- `pnpm --filter @hci/customer test` 或仓库既有前端测试命令通过；
- `pnpm --filter @hci/customer build` 通过；
- 页面手工验证：同节点重探成功后卡片与红条消失；异节点重探不影响旧卡片；
- docs 门禁要求的文档同步更新。

## 影响文件

- `frontend/customer/src/components/LogCollectPanel.vue`
- `frontend/customer/src/utils/logCollectFault.ts`
- `frontend/customer/src/components/__tests__/LogCollectPanel.spec.ts`
- `docs/solution/events/2026-11-25-日志收集acli失败态自动收敛.md`
