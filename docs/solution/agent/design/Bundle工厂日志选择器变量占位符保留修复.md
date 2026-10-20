# Bundle 工厂日志选择器变量占位符保留修复

## 问题

在 Bundle 工厂编译日志选择器时，`build_log_selector` 函数没有设置 `preserve_placeholders=True`，导致：
- 变量占位符 `{{VM}}` 被 `re.escape()` 转义
- `{{` 变成 `\{\{`，`}}` 变成 `\}\}`
- Bundle 编译后，变量占位符被破坏，无法被 hci-sim 正常渲染
- 案例 26980 执行时，命令中关键词有多余的反斜杠，导致匹配失败

## 根因

根据归档文档 `2026-08-26-Q2026082664908-KBD26980-仿真日志变量路由问题归档.md`，虽然 `build_log_selector` 已经实现了 `preserve_placeholders` 参数，但 Bundle 工厂（`offline_acquisition_compiler.py`）在调用时没有传这个参数。

## 解决方案

在 `backend/diagnosis-service/app/services/offline_acquisition_compiler.py` 第 140-147 行，添加 `preserve_placeholders=True` 参数：

```python
selector, extended_regex, matcher_type = build_log_selector(
    matcher=matcher,
    keywords=keywords,
    filter_keywords=filter_keywords,
    resource_keyword=normalized.get("resource_keyword"),
    request_id=normalized.get("request_id"),
    preserve_placeholders=True,  # ← 添加这行
)
```

## 效果

- ✅ 变量占位符 `{{VM}}` 被保留
- ✅ 只有字面量部分被 `re.escape()` 转义
- ✅ Bundle 编译后，变量占位符可以被 hci-sim 正常渲染
- ✅ 案例 26980 及其他使用变量的案例能够正常匹配

## 关联案例

- 案例 26980：【HCI-VT】回收站清空失败，删除回收站残留目录失败
- 信号 ID：`expert_1787100603104_e34f46ae3c4f`
- 归档文档：`docs/solution/events/2026-08-26-Q2026082664908-KBD26980-仿真日志变量路由问题归档.md`

## 修复时间

2026-09-09
