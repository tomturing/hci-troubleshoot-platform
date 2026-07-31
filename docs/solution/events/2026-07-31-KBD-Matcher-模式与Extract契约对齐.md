# KBD Matcher 模式与 Extract 契约对齐

## 触发

KBD27123 重新抽取时，LLM 生成了 `match.mode: "any"`。v2 Schema 正确拒绝该候选：Matcher 的 `mode` 只允许 `or`、`and`、`not`。

候选还遗漏了 `match.extract`；即使将 `any` 改为 `or`，也不能通过当前 Schema。

## 根因

`rows.include_mode` 合法值是 `all|any`，但 Prompt 对它与 `match.mode` 的字段归属隔离不够明确。与此同时，Prompt 的通用结构和 QFK Matcher 示例没有给出 Schema 必填的 `match.extract`，并错误地把 `json_path` 列为 Matcher 类型。

## 修复

- `match.mode` 仅允许 `or|and|not`，并明确禁止 `any|all`；
- `rows.include_mode` 仅允许 `all|any`，并明确禁止 `or|and|not`；
- backend 的每个 `match` 必须携带与 `produces[].extract` 相同的声明式 Extract；
- JSON 路径归属于 `extract.type=json`，取值后使用 `state`、`threshold` 或 `exists` 判定；
- 不新增 `any → or` 或 `all → and` 的运行时兼容。非法 LLM 候选保留在 `rejected_candidates` 供审计，修复 Prompt 后重新抽取生成全新的 Proposal。

## 验证

- Prompt 契约测试断言 Matcher/行选择模式隔离、Matcher 示例包含 `extract`、不存在 `json_path` Matcher 类型；
- 数据迁移 017 以断言阻止旧 Prompt 语义再次进入运行环境。
