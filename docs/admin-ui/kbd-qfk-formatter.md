# KBD QFK 输出格式选择

KBD 关键信号编辑器为 `qfk_system`、`qfk_vm`、`qfk_network`、`qfk_storage`、`qfk_hardware` 和 `qfk_platform` 提供统一的输出格式选择。

- 可选值为 `json`、`keyvalue`、`csv`、`xml`，以四个平铺按钮展示，任意时刻最多选择一个。
- 再次点击当前已选按钮会清除 `formatter` 字段；未选择时沿用 aCLI 的默认文本输出，并在查看态显示“默认文本”。
- 前端仅写入 schema 允许的枚举值，后端继续负责最终契约校验和命令编译。
