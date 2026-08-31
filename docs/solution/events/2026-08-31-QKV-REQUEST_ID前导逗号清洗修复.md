# QKV 任务与 QFK 日志 REQUEST_ID 前导逗号清洗与仿真路由修复

## 结论

在工单 `Q2026083165255`（`KBD 41464`）的端到端仿真测试中，步骤 10 `qfk_log` 执行失败并判定为「与预期矛盾」。
直接原因是：上游任务管理（`acli task get`）的原始 JSON 中 `request_id` 字段带有历史前导逗号（`",a678d3fb5fdf2af4e78e6dae896a06e2"`），QKV 变量提取层未予清洗即注入变量池；下游 `qfk_log` 直通拼装出 `acli log get -i ,a678d3fb...`，与 Bundle 预编译的无逗号路由（`RouteKey.argv`）不一致，导致 `hci-sim` 无法命中任何已发布路由并返回退出码 127 及 `fixture_not_found`。

本次修复实现了**生产端归一化清洗 + 消费端防御性清洗**的双层防御机制，彻底消除了由前导逗号引起的变量漂移和仿真未命中问题。

## 第一性原理与根因分析

1. **HCI 历史实现包袱**：
   底层任务管理（如 VTP/QemuServer）记录调用链时常使用逗号追加拼接机制（`$task->{request_id} .= ",$sub_req"`），导致初始任务在数据库中保存的 `request_id` 即带有前导逗号。
2. **QKV 提取漏洗**：
   在 `backend/agent-service/app/tools/qkv/parser.py` 中，`DIALOG` 模式使用正则 `request[_-]?id\s*(?::|=)\s*,?([a-f0-9]{32})` 成功清洗了前导逗号，但在 `TASK` 类型的 `_extract_by_produces` 和 `_extract_hardcoded` 中，只对 `END` 做了标准时间转换，漏掉了对 `REQUEST_ID` 的清洗，导致 `",a678d3fb5fdf2af4e78e6dae896a06e2"` 直接写入变量池。
3. **QFK 消费端直通拼装**：
   `LogHandler.build_commands` 和 `QfkLogAcquisitionResolver.resolve` 直接将 `signal.request_id` 传入 `-i` 参数，未作剥离。
4. **日志现场与仿真路由契约**：
   真实的 `/sf/log/1/sfscp.log` 日志行格式为 `[a678d3fb5fdf2af4e78e6dae896a06e2:113784:990dc1]`，本身不带逗号；Bundle 预编译路由同样为无逗号版本。传入带逗号的 `-i` 不仅使仿真测试以 `fixture_not_found` 失败，在物理实机上也会导致日志过滤失败。

## 对抗性审查（Adversarial Review）

- **质疑：是否应强制限制为 32 位十六进制？**
  - **审查**：不能强制限制。在仿真测试（如 `SIM-REQUEST-41464`）或离线排障中，`request_id` 可能采用语义标识或合成格式。因此，归一化采用通用策略：先剥离前导逗号与空白，多值拼接时取首个有效标识，既兼容真实 32 位 Hex，又不破坏合成环境与非标准场景。
- **质疑：仅在 QKV 提取阶段清洗是否足够？**
  - **审查**：不够。变量可能来自 Agent 外部输入、第三方适配器或手工指定的参数。消费端（`LogHandler` 与 `QfkLogAcquisitionResolver`）必须具备防御性过滤，形成 Fail-Safe 闭环。

## 变更内容

1. **`backend/agent-service/app/tools/qkv/parser.py`**：
   - 新增 `_normalize_request_id(value: Any) -> str` 规范化工具函数；
   - 在 `_extract_by_produces` 中对 `REQUEST_ID` 字段自动清洗（与 `END` 保持同等一等公民规范化）；
   - 在 `_extract_hardcoded` 的 `TASK` 模式中对 `request_id` 应用清洗。
2. **`backend/agent-service/app/tools/qfk/handlers.py`**：
   - 在 `LogHandler.build_commands` 中对 `signal.request_id` 进行前导逗号及空白清洗。
3. **`backend/shared/resolution/resolvers.py`**：
   - 在 `QfkLogAcquisitionResolver.resolve` 中对 `args["request_id"]` 进行防御性清洗。
4. **`backend/agent-service/tests/unit/test_qkv.py` & `test_qfk.py`**：
   - 增加针对前导逗号、多值逗号拼接及合成 ID 格式的单元测试，验证双端防御有效性。

## 验证结论

- `backend/agent-service/tests/unit/test_qkv.py` 全部 26 项测试通过。
- `backend/agent-service/tests/unit/test_qfk.py` 全部 32 项测试通过。
- `backend/shared/tests/` 全部 71 项测试通过。
