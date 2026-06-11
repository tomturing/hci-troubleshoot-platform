# Checklist for Scheme B Dual-Channel implementation

- [x] 阶段 1：Go 端 `terminal_bridge` 底层重构
  - [x] 1.1 新建出入站消息类型 `ssh_exec_process` 声明
  - [x] 1.2 在 `SSHSession` 下实现 `execProcess(command, execID)` 协程逻辑，使用 `NewSession` 隔离 SSH，禁用 PTY
  - [x] 1.3 实现 `exec_stdout` 和 `exec_stderr` 流式分片推送给前端
  - [x] 1.4 通过 `session.Wait()` 获取退出码并发送 `exec_result` 给前端，回收 session
- [x] 阶段 2：前端浏览器 (Vue / TS) 中转与渲染优化
  - [x] 2.1 升级前端 `terminal.ts` 以支持 `buildAgentExecProcessMessage`
  - [x] 2.2 扩展 `chat.ts` 状态机以支持 `exec_stdout` 与 `exec_stderr` 接收缓冲与 `ssh_exec_process` 发送
  - [x] 2.3 优化 `MessageBubble.vue` 工具结果渲染逻辑，不作 JSON 二次转义
- [x] 阶段 3：云端后端服务 API 契约与 AI 数据对齐
  - [x] 3.1 扩展 `/api/conversations/{conversation_id}/exec-result` HTTP 接口接收结构化的 `stdout` 与 `stderr`
  - [x] 3.2 优化 `executor.py` 内部结果提取逻辑，去掉 `2>&1`
  - [x] 3.3 调整 `ReactEngine` 对 `ToolResultEnvelope` 消息的生成机制
- [x] 阶段 4：测试与验证
  - [x] 4.1 执行单元测试，保证 355 个测试通过
  - [x] 4.2 验证手动与自动诊断流程正确性
