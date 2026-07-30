-- 工具定义表的定位由“数据库即能力”收敛为“数据库是可读投影，代码契约证明可执行性”。
-- 仅更新元数据注释，不新增流程表或状态字段，符合轻治理、自动化优先原则。
COMMENT ON TABLE tool_definition IS '工具定义表 — LLM 可读说明、示例和参数投影。acli/SCP 通用工具可直接注入 Prompt；QKV/QFK 的可执行语义必须同时存在于代码 Capability Descriptor、Agent Handler 和 Validator，数据库记录不能单独证明能力已部署';

COMMENT ON COLUMN tool_definition.category IS '工具类别：scp（SCP REST API）/ acli（HCI 节点执行）/ sop（SOP 导航）/ qkv（前端变量生产）/ qfk（后端确定性消费）';

COMMENT ON COLUMN tool_definition.description IS '可人工维护的工具功能描述，供 Prompt 和 Admin 展示；不得覆盖代码 Capability Descriptor 中的可执行边界';

COMMENT ON COLUMN tool_definition.parameters_schema IS '参数 JSON Schema。通用工具用于 function calling；QKV/QFK 为代码共享 Schema 的可读投影，发布门禁和运行时始终以 shared schema + Handler/Validator 为准，防止热编辑造成契约漂移';
