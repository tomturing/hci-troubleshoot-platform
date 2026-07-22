"""信号数据模型的共享契约（schema）定义。

本包是 RFC《关键信号数据模型分层重构》§4.4 / §6.1 的代码落地：
- `acquirer_args`：producer/consumer 同构的 `acquire.args` 注册表（单一事实来源）
- `signal_migration`：扁平 v1 → 嵌套 v2（数组级 schema_version）迁移纯函数

设计原则（第一性原理，见 RFC §4.4.1）：
1. 公共字段全局只定义一次（COMMON_ARGS），禁止各工具另造同名。
2. 专属字段各工具单独注册（ACQUIRER_ARGS_SCHEMA），带注释区分。
3. additionalProperties:false —— 杜绝幽灵字段（如再次冒出顶层 keyword）。
4. producer 写、consumer 读，键名/嵌套/类型完全一致（CQRS 同构契约）。

校验器为纯 Python 实现（不依赖 jsonschema 即可运行）；§6.1 的 JSON Schema 文件
是 CI 机器强制版本，二者语义对齐。
"""
