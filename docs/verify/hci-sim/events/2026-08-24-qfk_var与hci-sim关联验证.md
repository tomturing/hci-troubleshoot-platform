---
status: active
category: verify
audience: developer
last_updated: 2026-08-24
version: v1.0
owner: team
---

# qfk_var 与 hci-sim 关联验证

## 1. 验证结论

`qfk_var` 不编译成 hci-sim SSH Route。hci-sim 只提供外部事实，Resolver 将 qfk_var 传递为 `local_operations`，由 Agent 的共享 `variable_processor` 执行。

```text
hci-sim qkv/qfk Route
  -> stdout / structured result
  -> Agent variable_pool
  -> qfk_var local_operation
  -> derive / assert
```

这样可以证明真实 Agent 执行路径，同时避免 Go hci-sim 复制 Python qfk_var 算法。

## 2. 确定性样例

hci-sim 外部采集 Route 应返回以下受控 Synthetic description：

```text
主机（SVR_aCloud_670）的计算内存使用量（92.35 GB）超过阈值（92.34 GB），剩余：7.99 GB，使用率：92%
```

Agent 变量池先保存：

```text
DESCRIPTION = 上述 description
```

随后 qfk_var 配置：

```json
{
  "mode": "derive",
  "operation": "feature_extract",
  "input": "{{DESCRIPTION}}",
  "target_variable": "percent.current",
  "value_type": "percentage",
  "cardinality": "exactly_one"
}
```

预期结果：

```text
CURRENT_PERCENT = 92
```

hci-sim 只验证该 local operation 的 `requires`、`produces`、Schema 和 Bundle 完整性，不计算 92，也不调用 AI。

## 3. 必须覆盖的对抗场景

| 场景 | 预期结果 |
|---|---|
| `requires` 与 `{{VAR}}` 不一致 | C1 capability gap，禁止编译 |
| qfk_var derive 无 `produces` 或多个输出 | Bundle 编译阻断 |
| qfk_var assert 声明输出 | Bundle 编译阻断 |
| local operation 任意层级带 `command/shell/exec/argv` 字段 | fail-closed |
| 稳定多个 VM 候选 | Agent 返回 `QFK_VAR_CARDINALITY_MISMATCH`，hci-sim 不代选 |
| description 无稳定边界 | 只有 Agent 显式配置 fallback 才进入 AI |
| AI 返回不在 evidence 的值 | Agent 拒绝写池 |
| 上游 Route 未产出 DESCRIPTION | Agent `BLOCKED`，不能被 Synthetic 默认值掩盖 |

## 4. 边界

- Synthetic description 是合约样本，不代表真实 HCI 格式覆盖率。
- qfk_var 确定性内核的正确性由 Agent 共享内核测试和关联集成测试证明。
- AI fallback 使用固定 mock 或离线评估集，不作为 hci-sim 基础 CI 的真实模型依赖。
- 全变量池遍历、`{{*}}`、动态变量名和多个隐式输出仍不属于第一版。
