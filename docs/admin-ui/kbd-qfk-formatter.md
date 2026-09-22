# KBD QFK 输出格式选择

KBD 关键信号编辑器为 `qfk_system`、`qfk_vm`、`qfk_network`、`qfk_storage`、`qfk_hardware` 和 `qfk_platform` 提供统一的输出格式选择。

- 可选值为 `json`、`keyvalue`、`csv`、`xml`，以四个平铺按钮展示，任意时刻最多选择一个。
- 再次点击当前已选按钮会清除 `formatter` 字段；未选择时沿用 aCLI 的默认文本输出，并在查看态显示“默认文本”。
- 前端仅写入 schema 允许的枚举值，后端继续负责最终契约校验和命令编译。

六类工具的执行命令也共用安全管道转换入口。命令包含 `|` 时，管理台显示同一个“安全转换管道”按钮；转换器仅把受控的 `grep`、字段投影 `awk`、`cut`、`sed -n` 表达为声明式 `extract`，从不执行 Shell 管道。无法无损表达的管道仍会被拒绝。

## 变量采集原语 qfk_var 的差异

`qfk_var` 是专家维护的通用变量采集原语，与上述领域 QFK 有三点本质差异：

1. **输出格式**：不支持 `formatter` 选择（命令输出默认按全文取值，产出变量写入变量池）。
2. **管道语义**：`command` 是完整 shell 命令模板，**允许**管道/重定向（如 `nvme list | grep -c nvme`），不适用“安全转换管道”门禁；`acli` 命令亦按原样执行，无需前缀约束。
3. **执行结果处理**：没有“匹配/产出”模式 radio——**默认必产出变量**（`produces` 必填，stdout 全文写入），匹配判定降级为可选开关，可与产出共存（豁免其他 `qfk_*` 的二选一门禁）。

配套约定：

- 【产出变量】输入框与 `produces[0].name` 双向绑定；变量名仅限大写字母、数字、下划线且不以数字开头，重名将被拒绝。
- 命令中的 `{{VAR}}` 输入变量经变量池渲染后统一 `shlex.quote` 注入 shell 上下文；`stdin` 可选注入标准输入。
- 默认命令非零退出即停止（不取值、不落池）；可开启 `keep_on_failure` 使 `stderr`/`exit_code` 取值落变量（stdout 保持禁写），并走终端故障哨兵二次安全门。
- 命令经 `bash_exec` 受控执行并全量审计；破坏性/写动作命令会在发布审查（`VarResolver` 写动作扫描）中被拦截。
- **AI 抽取禁止生成 `qfk_var`**：该工具仅供专家在管理端维护，LLM 抽取产物中的 `qfk_var` 会被服务端强制剥离进 rejected_candidates（拒绝码 `tool_restricted`）。
