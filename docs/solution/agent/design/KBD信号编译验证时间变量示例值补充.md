# KBD 信号编译验证时间变量示例值补充

## 问题

在 `_tool_contract_checker` 函数的 `sample_values` 字典中，缺少 `DATE` 和 `ABSOLUTE_TIME` 变量的示例值，导致：
- `{{DATE}}` 被替换成默认值 `"value"`（不合法的时间格式）
- `{{ABSOLUTE_TIME}}` 被替换成默认值 `"value"`（不合法的时间格式）
- 触发 `validate_absolute_log_time` 验证失败，报错："日志时间必须是绝对时间"

影响案例 26980 及其他使用这些变量的案例在编译验证时报错。

## 解决方案

在 `backend/agent-service/app/adapters/agents/htp/kbd_differential.py` 的 `sample_values` 字典中添加：

```python
"DATE": "2026-07-30",                    # 对应 {{DATE}} 变量
"ABSOLUTE_TIME": "2026-07-30 10:00:00",  # 对应 {{ABSOLUTE_TIME}} 变量
```

## 关联案例

- 案例 26980：【HCI-VT】回收站清空失败，删除回收站残留目录失败
- 信号 ID：`expert_1787100603104_e34f46ae3c4f`

## 修复时间

2026-09-09
