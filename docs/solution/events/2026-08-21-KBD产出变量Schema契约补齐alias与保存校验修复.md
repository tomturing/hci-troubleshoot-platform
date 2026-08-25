---
status: active
category: solution
audience: developer
last_updated: 2026-08-21
owner: team
---

# KBD 产出变量 Schema 契约补齐 alias 与保存校验修复

## 背景与现象

在 staging 环境对 KBD41464（以及任意新增/编辑生产者信号产出变量的 KBD）进行编辑保存时，页面弹出错误提示：
`保存失败：关键信号 · sig_002 · 产出变量: 产出变量包含当前版本不支持的字段，请重新选择对应类型后保存。(错误码 SIGNAL_FIELD_UNSUPPORTED; 诊断编号 438f3886d01dfdee9361617e4d0e7158)`。

## 第一性原理根因分析

1. PR #856 引入了 `alias` 字段（用于支持同一 KBD 内多个生产者信号产出同名变量如 `END1`/`END2` 的局部别名）。
2. 前端 `KbdReviewView.vue` 在添加新产出变量时初始化了 `{ name: '', path: '', alias: '' }` 对象，并在表单编辑时允许配置 `alias`。
3. 但在后端信号契约 `signal.v2.schema.json` 及生成源 `backend/scripts/gen-schemas.py` 中，`orchestrate.produces.items` 配置了 `"additionalProperties": false`，却遗漏了 `"alias"` 属性声明。
4. 当保存包含 `alias`（无论是未填写的空字符串还是有效别名）的产出变量时，`kb-service` 校验器触发 `additionalProperties` 拒绝，抛出 `SIGNAL_FIELD_UNSUPPORTED`（422 Unprocessable Entity）。
5. 此外，`backend/shared/schemas/signal_schema.py` 中的 DAG 依赖检查在收集 `produces` 集合时仅提取了 `name`，未将 `alias` 纳入有效产出变量集合，可能导致下游引用别名时被误报为未声明外部变量。

## 对抗性审查与加固

- **Schema 单一真相源防漂移**：必须在 `backend/scripts/gen-schemas.py` 中定义 `alias`（约束格式 `^[A-Z][A-Z0-9_]*$`），并重新生成 `signal.v2.schema.json`，确保 CI `check_signal_schemas.py` 漂移检测通过。
- **前端空值防守**：在 `KbdReviewView.vue` 序列化信号保存（`normalizeSignalProduces`）时，对空字符串或纯空白的 `alias` 执行 `delete produce.alias`，避免空字符串污染数据或触发正则校验不匹配。
- **DAG 依赖闭环**：在 `signal_schema.py` 的变量依赖校验中，将 `alias` 与 `name` 一并纳入产出变量池。

## 涉及文件

- `backend/scripts/gen-schemas.py`：在 `orchestrate.produces.items.properties` 中补充 `alias` 定义。
- `backend/shared/schemas/signals/signal.v2.schema.json`：重新生成并包含 `alias` 契约。
- `backend/shared/schemas/signal_schema.py`：DAG 依赖校验支持 `alias` 产出变量。
- `backend/kb-service/tests/test_kbd_patch_signals.py`：新增产出变量带 `alias` 的保存回归测试用例。
- `frontend/admin/src/views/KbdReviewView.vue`：优化产出变量归一化逻辑，自动清理空 `alias`。
