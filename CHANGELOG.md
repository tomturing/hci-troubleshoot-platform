# Changelog

## [Unreleased]

### ♻️ 重构

* **[PR-B] agent-service + eval-service 服务拆分**：将 conversation-service 中的 AI 推理引擎提取为独立 `agent-service`（端口 8005），将评分/统计逻辑提取为独立 `eval-service`（端口 8007）。conversation-service 通过 `AgentClient` HTTP/SSE 委托推理任务，不再直接依赖 AgentRouter/GLMClient/ReactExecutor 等组件。新增 Helm 模板（agent-service、eval-service）和 CI 构建矩阵。

## [2.11.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.10.0...v2.11.0) (2026-05-16)


### ✨ 新功能

* **custom-ui:** 命令自动执行功能 v0.23 ([#276](https://github.com/tomturing/hci-troubleshoot-platform/issues/276)) ([acd1d16](https://github.com/tomturing/hci-troubleshoot-platform/commit/acd1d16673d78896ae8ef74035d6dadef71911af))
* **frontend:** 工具栏重排、终端历史按钮移入 TerminalPanel、采集命令更新 ([#286](https://github.com/tomturing/hci-troubleshoot-platform/issues/286)) ([87cc85f](https://github.com/tomturing/hci-troubleshoot-platform/commit/87cc85fd2ce8fde500dd165464d47b8c03a9fd1a))
* **ops-agent:** 新增 ops-web NodePort Service ([#271](https://github.com/tomturing/hci-troubleshoot-platform/issues/271)) ([5a1e580](https://github.com/tomturing/hci-troubleshoot-platform/commit/5a1e580a45f9aa52f2acdaca6f2b7d33f2fd238e))
* **ops-agent:** 新增 ops-web sidecar 容器自动启动 Streamlit ([#272](https://github.com/tomturing/hci-troubleshoot-platform/issues/272)) ([fcd3edf](https://github.com/tomturing/hci-troubleshoot-platform/commit/fcd3edfa9ffd5691e63513a00014b76b1e5e7d6a))
* 添加意图识别评估脚本 ([#289](https://github.com/tomturing/hci-troubleshoot-platform/issues/289)) ([b8ab91c](https://github.com/tomturing/hci-troubleshoot-platform/commit/b8ab91ca746315a1e91f147c921feac0e1b2eb52))


### 🐛 Bug 修复

* **api-gateway:** 新增 GET resume-stream 代理路由，修复交互选项点击后无续写（404） ([#280](https://github.com/tomturing/hci-troubleshoot-platform/issues/280)) ([3a3bd06](https://github.com/tomturing/hci-troubleshoot-platform/commit/3a3bd069b9acd8f41fe6cc1923afac8cc74f4725))
* **conversation:** ops-agent 会话刷新后 409 自动终止旧 prompt 并重试，防止降级到备用助手 ([#278](https://github.com/tomturing/hci-troubleshoot-platform/issues/278)) ([359e2e3](https://github.com/tomturing/hci-troubleshoot-platform/commit/359e2e3c15eb012efc46d86122d9c1c4f821e9ca))
* **environment:** 修正 acli 环境采集字段映射与前端展示 ([#285](https://github.com/tomturing/hci-troubleshoot-platform/issues/285)) ([1862c28](https://github.com/tomturing/hci-troubleshoot-platform/commit/1862c2845b1a2938096846f3841c7126905a8e68))
* **frontend:** loadConversationHistory 补充场景A——ops-agent 生成中途刷新自动 resume ([#283](https://github.com/tomturing/hci-troubleshoot-platform/issues/283)) ([be5674e](https://github.com/tomturing/hci-troubleshoot-platform/commit/be5674e3088af05b25002bc7a4175c6cde9ccefc))
* **frontend:** 自由文本提交后同步禁用选项按钮（4+1模式） ([#268](https://github.com/tomturing/hci-troubleshoot-platform/issues/268)) ([e5ffa1c](https://github.com/tomturing/hci-troubleshoot-platform/commit/e5ffa1ce7743da2a2af85096693b349781cf438e))
* ops-agent 刷新后上下文丢失完整修复（前端+后端+部署） ([#284](https://github.com/tomturing/hci-troubleshoot-platform/issues/284)) ([fb66c8b](https://github.com/tomturing/hci-troubleshoot-platform/commit/fb66c8bad9e52128a39c205e230ef86b62237d7b))
* **ops-agent:** 启动脚本用 Python 代替 curl 检查健康 ([#275](https://github.com/tomturing/hci-troubleshoot-platform/issues/275)) ([6ec6d97](https://github.com/tomturing/hci-troubleshoot-platform/commit/6ec6d974e11dbb2d1f0d6c16787ab99baca161f1))
* **ops-agent:** 设置 HOME=/app 解决 Streamlit 权限问题 ([#270](https://github.com/tomturing/hci-troubleshoot-platform/issues/270)) ([0a02497](https://github.com/tomturing/hci-troubleshoot-platform/commit/0a02497066c987f7cd1e5fbf17dd35aa55129b7b))
* **resume-stream:** 修复 Copilot 审查发现的5个代码质量问题 ([#281](https://github.com/tomturing/hci-troubleshoot-platform/issues/281)) ([7789a5f](https://github.com/tomturing/hci-troubleshoot-platform/commit/7789a5fe686be245a47acb8c983f5ea6c5ffd664))
* 修复 interactive_request 历史记录刷新后气泡内容为空的问题 ([#277](https://github.com/tomturing/hci-troubleshoot-platform/issues/277)) ([71181df](https://github.com/tomturing/hci-troubleshoot-platform/commit/71181df0afad1c2e521edf069a179cc16d2a0f93))
* 修复4个独立Bug：resume竞态/interactive落库竞态/delete-json参数/glm-5降级 ([#282](https://github.com/tomturing/hci-troubleshoot-platform/issues/282)) ([897ce8b](https://github.com/tomturing/hci-troubleshoot-platform/commit/897ce8b8ad10bb024f25ccc45e4ed34e358b0550))
* 修复交互卡片响应后无续写内容及发消息降级备用助手的双 Bug ([#279](https://github.com/tomturing/hci-troubleshoot-platform/issues/279)) ([090d48d](https://github.com/tomturing/hci-troubleshoot-platform/commit/090d48d571c07bcfc93f22138b91e3956c4f42f8))


### ♻️ 代码重构

* **ops-agent:** 回滚 sidecar 方案，改用单容器方案 ([#274](https://github.com/tomturing/hci-troubleshoot-platform/issues/274)) ([ee3fa99](https://github.com/tomturing/hci-troubleshoot-platform/commit/ee3fa99ca9928bb11d80ae304eafa6548ff3e53e))


### 📝 文档

* 添加 sop-agent 完整阶段分析文档 ([#290](https://github.com/tomturing/hci-troubleshoot-platform/issues/290)) ([f8ddb17](https://github.com/tomturing/hci-troubleshoot-platform/commit/f8ddb1710d924128baaa3f0f41482413f29d6b6b))
* 补充部署事件、知识库与任务文档 ([#288](https://github.com/tomturing/hci-troubleshoot-platform/issues/288)) ([e362c51](https://github.com/tomturing/hci-troubleshoot-platform/commit/e362c515ea09de31cf2aa7f56b408ddad60f9e04))
* 重命名 ops-agent-internals 目录为 ops-agent，文件名中文规范化 ([#291](https://github.com/tomturing/hci-troubleshoot-platform/issues/291)) ([744698b](https://github.com/tomturing/hci-troubleshoot-platform/commit/744698bffde06487de78d774265e079a44c5ed04))

## [2.10.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.9.0...v2.10.0) (2026-05-10)


### ✨ 新功能

* **custom-ui:** interactive_request 弹框改为对话气泡，命令块正常渲染 ([#264](https://github.com/tomturing/hci-troubleshoot-platform/issues/264)) ([25d0fb0](https://github.com/tomturing/hci-troubleshoot-platform/commit/25d0fb0e0f231fc9ece8559bd521f93081658dea))
* **frontend:** 交互选项提交后保持显示，提取 InteractiveOptions 共用组件 ([#267](https://github.com/tomturing/hci-troubleshoot-platform/issues/267)) ([f8ef9f6](https://github.com/tomturing/hci-troubleshoot-platform/commit/f8ef9f65278767e18e7add34ca6d92584281dc6b))
* **helm:** ops-agent Deployment 支持自定义滚动更新策略 ([#265](https://github.com/tomturing/hci-troubleshoot-platform/issues/265)) ([db99f72](https://github.com/tomturing/hci-troubleshoot-platform/commit/db99f7247f6a4f0bedcbcc34648b20fb7c356491))
* **ops-agent:** interactive_request/response 全链路落库到 message 表 ([#262](https://github.com/tomturing/hci-troubleshoot-platform/issues/262)) ([515c05b](https://github.com/tomturing/hci-troubleshoot-platform/commit/515c05b1701889d00b40c1b606c529d989d013da))


### 🐛 Bug 修复

* 修复 OTLP gRPC 端点 scheme 处理问题 ([#266](https://github.com/tomturing/hci-troubleshoot-platform/issues/266)) ([f70dde0](https://github.com/tomturing/hci-troubleshoot-platform/commit/f70dde0e0337137315709fea701040f92855b73e))

## [2.9.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.8.0...v2.9.0) (2026-05-09)


### ✨ 新功能

* **brain:** ops-agent ACP REST 全链路交互集成（T-E1～T-E7 + BUG-1 修复） ([#254](https://github.com/tomturing/hci-troubleshoot-platform/issues/254)) ([ec654c8](https://github.com/tomturing/hci-troubleshoot-platform/commit/ec654c86f383279db91daa2041043b149cba804d))


### 🐛 Bug 修复

* **api-gateway:** 新增 interactive-response 路由代理（RC-5） ([#259](https://github.com/tomturing/hci-troubleshoot-platform/issues/259)) ([fc592b5](https://github.com/tomturing/hci-troubleshoot-platform/commit/fc592b57bcd017a10cf447a1d07b57f201880122))
* **brain:** ops-agent 未启用时返回友好提示而非报错 ([#253](https://github.com/tomturing/hci-troubleshoot-platform/issues/253)) ([fcb83e7](https://github.com/tomturing/hci-troubleshoot-platform/commit/fcb83e73b03a3fbcde37880a9406f168d6e51f5f))
* **ci:** 用 sync-env-repo-tags.sh 统一同步逻辑，修复 opsAgent tag 被覆盖问题 ([#260](https://github.com/tomturing/hci-troubleshoot-platform/issues/260)) ([f876372](https://github.com/tomturing/hci-troubleshoot-platform/commit/f876372678632625c810c16d8a0fa010d8d339c1))
* **conversation-service:** ops-agent 超时/fallback/glm-5 三项修复 ([#257](https://github.com/tomturing/hci-troubleshoot-platform/issues/257)) ([5013956](https://github.com/tomturing/hci-troubleshoot-platform/commit/50139563b2b476dabfbc93b4add6dda9955a7489))
* **frontend:** pnpm install 添加 --ignore-scripts=false 解决构建失败 ([#258](https://github.com/tomturing/hci-troubleshoot-platform/issues/258)) ([744e743](https://github.com/tomturing/hci-troubleshoot-platform/commit/744e74350f6b8a0f13a6baa52fb016a8c58c5739))
* **ops-agent:** 修复空响应根因 + 前端单元测试 CI ([#251](https://github.com/tomturing/hci-troubleshoot-platform/issues/251)) ([a7116df](https://github.com/tomturing/hci-troubleshoot-platform/commit/a7116dfee7363eaed7262f305a1185352d87d57a))


### 📝 文档

* **ops-agent:** 新增模块设计文档，基于源码深度分析 ([#255](https://github.com/tomturing/hci-troubleshoot-platform/issues/255)) ([56d661f](https://github.com/tomturing/hci-troubleshoot-platform/commit/56d661ff58cc99f265f13fd7b07f20d70f9ccbf7))
* **ops-agent:** 添加手动测试方案文档 ([#256](https://github.com/tomturing/hci-troubleshoot-platform/issues/256)) ([26d6388](https://github.com/tomturing/hci-troubleshoot-platform/commit/26d63880041a22b331bad64b462eafd829ec68bd))

## [Unreleased]

### ✨ 新功能

* **conversation-service/frontend:** ops-agent 弹框交互内容（interactive_request/interactive_response）落库到 message 表，支持对话历史查看；前端 `ChatMessage` 新增 `metadata` 字段映射，`InteractiveRequestCard` 提交选项时携带 `optionLabel` 便于可读落库

### 🧪 测试

* **conversation-service:** 新增 10 个单元测试，覆盖 `_format_interactive_request_content`、`_format_interactive_response_content`、`submit_interactive_response` 落库全路径（含适配器不可用时不落库的负向场景）

### 🐛 Bug 修复

* **ci:** 修复 `auto-deploy-non-prod` job 内联 `sed` 绕过 `BLOCKED_SERVICES` 保护导致 `opsAgent.tag` 被 htp CI tag 错误覆盖的问题 ([#260](https://github.com/tomturing/hci-troubleshoot-platform/pull/260))
* **ci/scripts:** 修复 `update_db_migrate_image` 使用错误结构（单行 URL）匹配，实际 values.yaml 为嵌套 `image.tag` 结构，导致 dbMigrate 从未被正确更新 ([#260](https://github.com/tomturing/hci-troubleshoot-platform/pull/260))

### ♻️ 重构

* **ci:** `auto-deploy-non-prod` job 改为调用 `scripts/ops/sync-env-repo-tags.sh`，消除内联重复逻辑；`env-repo-sync.yml`（手动触发）与 CI 自动触发共用同一脚本 ([#260](https://github.com/tomturing/hci-troubleshoot-platform/pull/260))
* **scripts:** `sync-env-repo-tags.sh` 新增 `SKIP_DB_MIGRATE` 环境变量、`set -euo pipefail` 严格模式 ([#260](https://github.com/tomturing/hci-troubleshoot-platform/pull/260))

——session/done 无文本内容时抛出 `BrainUnavailableError`，触发 HTP fallback，消除空白气泡 ([#251](https://github.com/tomturing/hci-troubleshoot-platform/pull/251))
* **conversation-service:** 修正 `/interactive-response` 接口注释 404→503，日志文案"已丢就"→"已丢弃/已降级" ([#251](https://github.com/tomturing/hci-troubleshoot-platform/pull/251))
* **frontend:** 修正 `InteractiveRequestCard` metadata 字段类型为 `InteractiveRequestMetadata`，消除 vue-tsc strict 模式类型错误；`@pinia/testing` 降级至 `^0.1.7` 与 pinia 2.x 兼容 ([#251](https://github.com/tomturing/hci-troubleshoot-platform/pull/251))

### 🧪 测试

* **conversation-service:** 新增 `test_ops_agent_adapter.py`（6个单元测试），覆盖 `_consume_events` 所有关键路径 ([#251](https://github.com/tomturing/hci-troubleshoot-platform/pull/251))
* **ci:** 新增 `frontend-unit-test` job，`unit-tests` job 依赖前端测试通过 ([#251](https://github.com/tomturing/hci-troubleshoot-platform/pull/251))

## [2.8.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.7.0...v2.8.0) (2026-05-08)


### ✨ 新功能

* **brain:** ops-agent ACP REST 客户端集成 + OOMKilled 修复 + BrainInteractiveRequest ([#249](https://github.com/tomturing/hci-troubleshoot-platform/issues/249)) ([0a9e02d](https://github.com/tomturing/hci-troubleshoot-platform/commit/0a9e02d86838d0e5f723e71a938ceae1c05f53f3))
* **brain:** T-E6/T-E7 ops-agent 交互卡片全链路实现 ([#250](https://github.com/tomturing/hci-troubleshoot-platform/issues/250)) ([84777fb](https://github.com/tomturing/hci-troubleshoot-platform/commit/84777fbc85ee5861a8dcb7f12439799ebfb2486e))


### 🐛 Bug 修复

* htp 助手选择恢复 + 大脑诊断检出质量 P0-P3 修复 ([#248](https://github.com/tomturing/hci-troubleshoot-platform/issues/248)) ([1b7bccb](https://github.com/tomturing/hci-troubleshoot-platform/commit/1b7bccbfae5c01000ad06098f73108ae83605a20))
* ops-agent 使用 imagePullPolicy: Always ([#242](https://github.com/tomturing/hci-troubleshoot-platform/issues/242)) ([6acb188](https://github.com/tomturing/hci-troubleshoot-platform/commit/6acb18840a494d2634f39933fd7c6742287babd7))
* ops-agent 镜像使用独立 registry 不拼接 global.imageRegistry ([#240](https://github.com/tomturing/hci-troubleshoot-platform/issues/240)) ([b059f4e](https://github.com/tomturing/hci-troubleshoot-platform/commit/b059f4e94812456fb8165a281671677c37e531c5))
* **ops-agent:** SOP 数据 HostPath 挂载方案 ([#245](https://github.com/tomturing/hci-troubleshoot-platform/issues/245)) ([5637782](https://github.com/tomturing/hci-troubleshoot-platform/commit/5637782bb4fb8a16c5629a542fde3208831014a2))
* **ops-agent:** 修复 Helm template imagePullPolicy YAML parse 错误 ([#246](https://github.com/tomturing/hci-troubleshoot-platform/issues/246)) ([bead216](https://github.com/tomturing/hci-troubleshoot-platform/commit/bead216f8bb6039920edb8ee2c863802629970d9))
* **scheduler:** 修复助手选择器可用性判断 Bug + ops-agent 注册 ([#243](https://github.com/tomturing/hci-troubleshoot-platform/issues/243)) ([f48677d](https://github.com/tomturing/hci-troubleshoot-platform/commit/f48677d873779e186d7f9629e519f49e928f984b))


### 📝 文档

* ops-agent 手动更新 Skill + 跨仓库联动方案设计 ([#244](https://github.com/tomturing/hci-troubleshoot-platform/issues/244)) ([e213225](https://github.com/tomturing/hci-troubleshoot-platform/commit/e21322563a4725e5773d4b2a14478d3e304de6e9))

## [2.7.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.6.0...v2.7.0) (2026-04-29)


### ✨ 新功能

* **conversation:** Phase 1 大脑可选——BrainPort/Adapter/Router 架构 ([#238](https://github.com/tomturing/hci-troubleshoot-platform/issues/238)) ([8ccbc67](https://github.com/tomturing/hci-troubleshoot-platform/commit/8ccbc67d5bb3ee9ec0bbb4a1206b2a6bee973586))
* **helm:** ops-agent 大脑服务 Helm 部署配置（dashscope glm-5） ([#237](https://github.com/tomturing/hci-troubleshoot-platform/issues/237)) ([7a747e5](https://github.com/tomturing/hci-troubleshoot-platform/commit/7a747e56f38266fb14b463db12cdeac64d0fbf82))


### 🐛 Bug 修复

* **conversation-service:** 探针改用分级健康检查端点 ([#230](https://github.com/tomturing/hci-troubleshoot-platform/issues/230)) ([d4ca503](https://github.com/tomturing/hci-troubleshoot-platform/commit/d4ca503a70474b4dd6fc6508b23bef30e66991bf))
* **data:** 彻底删除 Alembic 熔断器，解决配置冲突 ([#231](https://github.com/tomturing/hci-troubleshoot-platform/issues/231)) ([4e6ae44](https://github.com/tomturing/hci-troubleshoot-platform/commit/4e6ae443380c77feb3faf46f8662fa5b7239c98c))
* **deploy:** 修复 ArgoCD Hook 并统一 prod/staging HTTPS 基线 ([#234](https://github.com/tomturing/hci-troubleshoot-platform/issues/234)) ([9f6fbc9](https://github.com/tomturing/hci-troubleshoot-platform/commit/9f6fbc9f30a36df7289c733f461e449d5ff2ab8e))
* **frontend:** no-SSH 流程 completeCaseCreationFlow 因 currentCase 为 null 导致 createConversation 失败 ([#239](https://github.com/tomturing/hci-troubleshoot-platform/issues/239)) ([446c4a4](https://github.com/tomturing/hci-troubleshoot-platform/commit/446c4a4be6f36057f6166ae612a50356f6c5fb1a))
* **ingress:** Grafana Ingress 支持 TLS 时自动切换到 websecure 入口 ([#229](https://github.com/tomturing/hci-troubleshoot-platform/issues/229)) ([4c45d74](https://github.com/tomturing/hci-troubleshoot-platform/commit/4c45d74820578dc01059c7df79a9d740d66da784))
* **ingress:** 支持 TLS 时自动切换到 websecure 入口 ([#227](https://github.com/tomturing/hci-troubleshoot-platform/issues/227)) ([c78e3ac](https://github.com/tomturing/hci-troubleshoot-platform/commit/c78e3ac70d85a9fb9fc64c209a23777d38d652ac))
* **terminal_bridge:** 修正 CORS 头使用请求 Origin 回填 ([#226](https://github.com/tomturing/hci-troubleshoot-platform/issues/226)) ([97c6c2e](https://github.com/tomturing/hci-troubleshoot-platform/commit/97c6c2eae13157e3893e33cfc926d89421066d44))
* **terminal_bridge:** 添加 CORS 头支持 Chrome Private Network Access ([#225](https://github.com/tomturing/hci-troubleshoot-platform/issues/225)) ([2a59b5c](https://github.com/tomturing/hci-troubleshoot-platform/commit/2a59b5cb7b269dd9a324f4a1d150a15c2183103d))


### 📝 文档

* Phase 1 大脑可选集成规范化文档 ([#236](https://github.com/tomturing/hci-troubleshoot-platform/issues/236)) ([6a39488](https://github.com/tomturing/hci-troubleshoot-platform/commit/6a39488b8f0bf2e4b5aff674328e3ef026e1792e))
* 更新部署架构文档，补充完整端口映射和环境说明 ([#233](https://github.com/tomturing/hci-troubleshoot-platform/issues/233)) ([c501d6e](https://github.com/tomturing/hci-troubleshoot-platform/commit/c501d6e52188b8d428290e91e66c1e69a0104021))
* 添加 D-006 GitHub PAT 失效导致镜像拉取失败避坑指南 ([#223](https://github.com/tomturing/hci-troubleshoot-platform/issues/223)) ([bb19d9e](https://github.com/tomturing/hci-troubleshoot-platform/commit/bb19d9e2ce90693d5097e249a00b6a64b1278f92))
* 补充 D-007 易踩坑说明，明确 CORS 头仅在 HTTPS 场景有效 ([#228](https://github.com/tomturing/hci-troubleshoot-platform/issues/228)) ([1dcd368](https://github.com/tomturing/hci-troubleshoot-platform/commit/1dcd368d23cf6509ba8c4872929c9545ce629b85))

## [2.6.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.5.0...v2.6.0) (2026-04-26)


### ✨ 新功能

* **argocd:** 统一dev/staging ArgoCD App of Apps架构 ([#221](https://github.com/tomturing/hci-troubleshoot-platform/issues/221)) ([569010e](https://github.com/tomturing/hci-troubleshoot-platform/commit/569010e46ed3b40a7c9c0fb5c99ebff9865fe93d))
* SSH UX 全场景重构 - CaseCreateDialog 6视图状态机 ([#204](https://github.com/tomturing/hci-troubleshoot-platform/issues/204)) ([0a8bdc6](https://github.com/tomturing/hci-troubleshoot-platform/commit/0a8bdc61ba334695f3760f04a641ffed73cec2b3))
* SSH UX 全场景重构 - CaseCreateDialog 6视图状态机 ([#205](https://github.com/tomturing/hci-troubleshoot-platform/issues/205)) ([39558d5](https://github.com/tomturing/hci-troubleshoot-platform/commit/39558d520ce0616b99271ac7b9dbd6197dbc5b15))
* 新增 Prompt 审计 Dashboard 和上下文可观测性文档 ([#210](https://github.com/tomturing/hci-troubleshoot-platform/issues/210)) ([85f78c4](https://github.com/tomturing/hci-troubleshoot-platform/commit/85f78c4f39b5fef498016f5dffa0b96324cdf5b6))
* 添加 k8s-routing-bypass 脚本修复 Clash TUN 下 Pod DNS 问题 ([#222](https://github.com/tomturing/hci-troubleshoot-platform/issues/222)) ([5a4be53](https://github.com/tomturing/hci-troubleshoot-platform/commit/5a4be5312592b5af82cd58e1278368fe0e47a0af))
* 终端操作录制功能 (Task 42) [WIP] ([#211](https://github.com/tomturing/hci-troubleshoot-platform/issues/211)) ([6d9c3fc](https://github.com/tomturing/hci-troubleshoot-platform/commit/6d9c3fca88c3f79b14fcd2611679ee36eb7b2cfd))


### 🐛 Bug 修复

* **admin:** 修复分类详情页 UI 三处问题（对齐/命中次数/Markdown渲染） ([#200](https://github.com/tomturing/hci-troubleshoot-platform/issues/200)) ([2dafd46](https://github.com/tomturing/hci-troubleshoot-platform/commit/2dafd465e1e25efc24f95860082562b5ecf0c1bf))
* **argocd:** PreSync Hook 镜像改为 bitnami/kubectl:1.31 ([#195](https://github.com/tomturing/hci-troubleshoot-platform/issues/195)) ([6311d95](https://github.com/tomturing/hci-troubleshoot-platform/commit/6311d9574c413148b90f53b85ae2a5b77c26c0d2))
* **cluster+s0:** Issue A 集群数据解析错误 + Issue B S0 提前 confirm ([#217](https://github.com/tomturing/hci-troubleshoot-platform/issues/217)) ([f5d60ee](https://github.com/tomturing/hci-troubleshoot-platform/commit/f5d60ee1b6b8032f5217225384b0a087f6bfaaea))
* **frontend:** 终端历史强制刷新 + 选项置灰 + 以上都不是输入框 ([#220](https://github.com/tomturing/hci-troubleshoot-platform/issues/220)) ([825850d](https://github.com/tomturing/hci-troubleshoot-platform/commit/825850d508e8f886498997ea98f8097ca8821181))
* **s0+terminal-replay:** S0→S1 AI流程断开 + 终端回放导航失效 ([#219](https://github.com/tomturing/hci-troubleshoot-platform/issues/219)) ([7afdf19](https://github.com/tomturing/hci-troubleshoot-platform/commit/7afdf19649cb6af3aae7cdb7880896cd2f3b018c))
* SSH 连接日志增强，用于排查前端无输出问题 ([#194](https://github.com/tomturing/hci-troubleshoot-platform/issues/194)) ([5a1f8e5](https://github.com/tomturing/hci-troubleshoot-platform/commit/5a1f8e5059e7495cd4346a06c84c85eae7e11183))
* SSH 采集超时和 AI 对话无响应问题 ([#207](https://github.com/tomturing/hci-troubleshoot-platform/issues/207)) ([c808e68](https://github.com/tomturing/hci-troubleshoot-platform/commit/c808e68a124c99ad06ddf1f386a3eac944c463d4))
* SSH 集成体验修复 - upsert机制/统一入口/SshFlowPanel重构 ([#203](https://github.com/tomturing/hci-troubleshoot-platform/issues/203)) ([c22a9e0](https://github.com/tomturing/hci-troubleshoot-platform/commit/c22a9e0fb3fb12876a78d2c91d68de3645e25357))
* SSH 集成创建工单流程增强 - 解决卡住问题并新增排查日志 ([#202](https://github.com/tomturing/hci-troubleshoot-platform/issues/202)) ([2b684ca](https://github.com/tomturing/hci-troubleshoot-platform/commit/2b684cadda0d884cebaf09d93ed739fd96de4795))
* 修复 ArgoCD PreSync Hook 镜像版本无效问题 ([#196](https://github.com/tomturing/hci-troubleshoot-platform/issues/196)) ([109064b](https://github.com/tomturing/hci-troubleshoot-platform/commit/109064b2e3e12afccac21188dd073f78d03b8940))
* 修复 Copilot Review 提出的 15 个问题 ([#206](https://github.com/tomturing/hci-troubleshoot-platform/issues/206)) ([2e6a3d9](https://github.com/tomturing/hci-troubleshoot-platform/commit/2e6a3d9ffac1a7a4c2224176acff4b8b37e52e87))
* 修复 PreSync Hook 镜像不含 shell 问题 ([#197](https://github.com/tomturing/hci-troubleshoot-platform/issues/197)) ([7aa2663](https://github.com/tomturing/hci-troubleshoot-platform/commit/7aa266366a03b08d4d84b2acf23a5d0a86e10cab))
* 修复 S0 Prompt Segment 4 为空问题 ([#209](https://github.com/tomturing/hci-troubleshoot-platform/issues/209)) ([8303fad](https://github.com/tomturing/hci-troubleshoot-platform/commit/8303fadede918b9f61d369bcbc5dfe9bc888cf68))
* 修复分类管理页面三处 Bug（API路径/排序/对齐） ([#193](https://github.com/tomturing/hci-troubleshoot-platform/issues/193)) ([e85cf0d](https://github.com/tomturing/hci-troubleshoot-platform/commit/e85cf0d6341958c407ce57e0bdddedefd71a72f8))
* 修复前端 UX 五个问题（SSH采集/工单badge/AI无响应/环境数据） ([#208](https://github.com/tomturing/hci-troubleshoot-platform/issues/208)) ([d1d20d1](https://github.com/tomturing/hci-troubleshoot-platform/commit/d1d20d1265ec61033d3969ef09f3f99bff306774))
* 修复工单状态流转、S0→S1 诊断卡死及终端录制无法写库四处缺陷 ([#215](https://github.com/tomturing/hci-troubleshoot-platform/issues/215)) ([893268f](https://github.com/tomturing/hci-troubleshoot-platform/commit/893268fadbaf8c60f8d079568906ba5eeedb1efa))
* 修复环境数据全链路字段映射错误及SSH终端显示异常（B1-B5） ([#212](https://github.com/tomturing/hci-troubleshoot-platform/issues/212)) ([c65c8a4](https://github.com/tomturing/hci-troubleshoot-platform/commit/c65c8a48644f711f4c3d09c0c0ad6e0cd8ecfee3))
* 修复环境数据统计和显示字段不一致问题 ([#214](https://github.com/tomturing/hci-troubleshoot-platform/issues/214)) ([bbb6dc5](https://github.com/tomturing/hci-troubleshoot-platform/commit/bbb6dc524b56b7e332366615843ae2139485c888))
* 修复终端操作接口500、集群信息未知、SSH配置恢复及S0选项渲染 ([#216](https://github.com/tomturing/hci-troubleshoot-platform/issues/216)) ([41bee8c](https://github.com/tomturing/hci-troubleshoot-platform/commit/41bee8c74a81be57493c856e4cbaf2d0349eb44a))
* 恢复全局SSH连接方法，修复合并冲突丢失的代码 ([#191](https://github.com/tomturing/hci-troubleshoot-platform/issues/191)) ([93164ea](https://github.com/tomturing/hci-troubleshoot-platform/commit/93164ea6ac8d61a607c98b4de4836027241088dc))
* 根治 Issue1/2/3 三个生产 Bug ([#218](https://github.com/tomturing/hci-troubleshoot-platform/issues/218)) ([3ac78af](https://github.com/tomturing/hci-troubleshoot-platform/commit/3ac78af4570fafba5ff274fe656b41b7b5318f4d))
* 补充 api-gateway 缺失的 sqlalchemy 和 asyncpg 依赖 ([#213](https://github.com/tomturing/hci-troubleshoot-platform/issues/213)) ([74ef599](https://github.com/tomturing/hci-troubleshoot-platform/commit/74ef5991fea6216a986700e64b21940e55c30be1))


### ♻️ 代码重构

* ArgoCD 架构重构 - 分离 Root Application 解决循环引用问题 ([#199](https://github.com/tomturing/hci-troubleshoot-platform/issues/199)) ([89c3213](https://github.com/tomturing/hci-troubleshoot-platform/commit/89c32130f3b0e278f447527745abb1ae7cff6b80))
* SSH 连接架构重构 - 全局统一管理 + Bridge刷新 + UX优化 ([#189](https://github.com/tomturing/hci-troubleshoot-platform/issues/189)) ([d07b161](https://github.com/tomturing/hci-troubleshoot-platform/commit/d07b16100dd262c2cb21455c90be9760e6ab46db))
* 拆分 argocd-ops RBAC 至独立 Application 解决同步顺序依赖 ([#198](https://github.com/tomturing/hci-troubleshoot-platform/issues/198)) ([1c18cff](https://github.com/tomturing/hci-troubleshoot-platform/commit/1c18cff68d33f4aa99694ac2ba56169602037c31))


### 📝 文档

* 客户端设计.md v1.4、对话设计.md v1.5 同步变更历史 ([3ac78af](https://github.com/tomturing/hci-troubleshoot-platform/commit/3ac78af4570fafba5ff274fe656b41b7b5318f4d))
* 对话任务.md 追加 BUG 修复记录 ([825850d](https://github.com/tomturing/hci-troubleshoot-platform/commit/825850d508e8f886498997ea98f8097ca8821181))
* 对话任务.md 追加 BUG 修复记录 ([7afdf19](https://github.com/tomturing/hci-troubleshoot-platform/commit/7afdf19649cb6af3aae7cdb7880896cd2f3b018c))
* 整理分类管理KBD统计显示方案与任务文档 ([#192](https://github.com/tomturing/hci-troubleshoot-platform/issues/192)) ([8e9205b](https://github.com/tomturing/hci-troubleshoot-platform/commit/8e9205b81f93625bbd35ef452daf2d8bcaa72e0b))

## [2.5.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.4.1...v2.5.0) (2026-04-21)


### ✨ 新功能

* AI 助手选择器交互优化 — 支持对话中随时切换 ([#172](https://github.com/tomturing/hci-troubleshoot-platform/issues/172)) ([bdc32cc](https://github.com/tomturing/hci-troubleshoot-platform/commit/bdc32cc08ae75dcd24e29e2e8b75d9378d6b25a6))
* AI 助手选择器智能显示重构（v2.1） ([#171](https://github.com/tomturing/hci-troubleshoot-platform/issues/171)) ([8276f30](https://github.com/tomturing/hci-troubleshoot-platform/commit/8276f300d9b7d1f2b718cb370f63df6c1c196ca5))
* **kbd:** Pipeline 日志持久化与进度追踪改进 ([#180](https://github.com/tomturing/hci-troubleshoot-platform/issues/180)) ([bbb607a](https://github.com/tomturing/hci-troubleshoot-platform/commit/bbb607acf42396b6bdd70d8e23993ccc4c38cecb))
* **kbd:** 截图语义化重构 v3 — Vision LLM 单次调用+文档上下文注入 ([#175](https://github.com/tomturing/hci-troubleshoot-platform/issues/175)) ([5c19e22](https://github.com/tomturing/hci-troubleshoot-platform/commit/5c19e22a75c3339e1a675d6d6f02bb041def6691))
* **kbd:** 新增 override + override_status 参数支持 ([#174](https://github.com/tomturing/hci-troubleshoot-platform/issues/174)) ([7b2aa66](https://github.com/tomturing/hci-troubleshoot-platform/commit/7b2aa66523ad33251ae94f18c729d7f66f959a15))
* SSH 集成创建工单 — Environment 数据采集 UI 入口 ([#187](https://github.com/tomturing/hci-troubleshoot-platform/issues/187)) ([ace5f0f](https://github.com/tomturing/hci-troubleshoot-platform/commit/ace5f0f0d755ae08590c9ba210aca830bda227b3))
* 分类管理增强 — KBD/SOP 统计显示与多项问题修复 ([#179](https://github.com/tomturing/hci-troubleshoot-platform/issues/179)) ([dd95cdd](https://github.com/tomturing/hci-troubleshoot-platform/commit/dd95cdd0ba0bbbe7830e3980979c57b51f697515))
* 实现 Environment API — alert/task/environment 数据存库 ([#181](https://github.com/tomturing/hci-troubleshoot-platform/issues/181)) ([f7b7f1e](https://github.com/tomturing/hci-troubleshoot-platform/commit/f7b7f1ecd8cdf42f77acf84ef85e644774f70d53))
* 知识命中统计（hit_count）重设计 - T1~T10 完整实现 ([#184](https://github.com/tomturing/hci-troubleshoot-platform/issues/184)) ([02674da](https://github.com/tomturing/hci-troubleshoot-platform/commit/02674da2d8175f78201c00301a063e9b06a78ffe))


### 🐛 Bug 修复

* ArgoCD v3.3.6 升级问题复盘 - 补充避坑指南并优化升级脚本 ([#188](https://github.com/tomturing/hci-troubleshoot-platform/issues/188)) ([6436c84](https://github.com/tomturing/hci-troubleshoot-platform/commit/6436c847e0c653bf0fed554e80070b4d49d7e59d))
* **argocd:** StrategicMergePatch 改为 PreSync Hook Job ([#170](https://github.com/tomturing/hci-troubleshoot-platform/issues/170)) ([732610f](https://github.com/tomturing/hci-troubleshoot-platform/commit/732610f77f363f2caf8d2a66f775b45fc204b0aa))
* **argocd:** 优化 repo-server probe 策略，解决 CrashLoopBackOff ([#168](https://github.com/tomturing/hci-troubleshoot-platform/issues/168)) ([86da16e](https://github.com/tomturing/hci-troubleshoot-platform/commit/86da16e808d2561ab61ff8ce623650c5eef83f21))
* Environment API 类型与状态修复 ([#182](https://github.com/tomturing/hci-troubleshoot-platform/issues/182)) ([7a97118](https://github.com/tomturing/hci-troubleshoot-platform/commit/7a97118f7bd180415cc4be081e739543d8792290))
* Environment API 补充修复 ([#183](https://github.com/tomturing/hci-troubleshoot-platform/issues/183)) ([364009e](https://github.com/tomturing/hci-troubleshoot-platform/commit/364009e8cdb9f057ef59031ec06ec6510f498ec2))
* **kbd:** 修复 KBD Pipeline 配置问题 + 自动 port-forward 检测 ([#173](https://github.com/tomturing/hci-troubleshoot-platform/issues/173)) ([7553928](https://github.com/tomturing/hci-troubleshoot-platform/commit/755392890472332da3548fed1f1f06e2822b0ff3))
* 恢复知识库设计.md 文档内容（PR [#182](https://github.com/tomturing/hci-troubleshoot-platform/issues/182)误删1265行） ([#185](https://github.com/tomturing/hci-troubleshoot-platform/issues/185)) ([07c5dca](https://github.com/tomturing/hci-troubleshoot-platform/commit/07c5dca62b316fc95779f1ab3085d90541a2fdf8))
* 捕获 httpx.RemoteProtocolError 异常 ([#176](https://github.com/tomturing/hci-troubleshoot-platform/issues/176)) ([843eb14](https://github.com/tomturing/hci-troubleshoot-platform/commit/843eb1476a6bc9afeaf25ae5cfca1bf6b96063a7))


### ♻️ 代码重构

* **kbd:** 抽取公共图片 URL 提取函数 ([#177](https://github.com/tomturing/hci-troubleshoot-platform/issues/177)) ([1bb8fa9](https://github.com/tomturing/hci-troubleshoot-platform/commit/1bb8fa95ca5bdd4a0887c316e53bdb84cff7e17d))


### 📝 文档

* 文档治理合规审计 + SSH 集成创建工单方案文档 ([#186](https://github.com/tomturing/hci-troubleshoot-platform/issues/186)) ([851faf8](https://github.com/tomturing/hci-troubleshoot-platform/commit/851faf84aa389ef952c166aff591959cb04b042d))

## [Unreleased] - 知识命中统计（hit_count）重设计

### ✨ 新功能

* **kb-service**: SOP/KBD hit_count 统计接口（POST /api/kb/sop/{id}/hit，POST /api/kb/kbd/{id}/hit[/decrement]）
* **conversation-service**: S1 阶段写入 conversation.sop_document_id，S4 根因确认写入 resolved_kbd_entry_id
* **admin-UI (CaseDetailView)**: 工单详情页展示关联 KBD，支持管理员手动修正并自动调整 hit_count
* **admin-UI (CategoryManageView)**: SOP/KBD 列表展示真实 hit_count 数据

### 🐛 Bug 修复

* **conversation-service**: 修复 kb_category.hit_count 断线重连虚增问题（case 级去重）
* **kb-service**: KBD/SOP 列表 API 补充 hit_count 字段返回

### 🗄️ Schema 变更（需执行 Atlas migrate diff）

* `conversation` 表新增 `sop_document_id`、`resolved_kbd_entry_id` 字段
* `kbd_entry` / `sop_document` 表新增 `hit_count` 字段

## [2.4.1](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.4.0...v2.4.1) (2026-04-17)


### 🐛 Bug 修复

* **db-password-check:** 修复 Secret 名称依赖错误，采用业界最佳实践 ([#167](https://github.com/tomturing/hci-troubleshoot-platform/issues/167)) ([ac60f71](https://github.com/tomturing/hci-troubleshoot-platform/commit/ac60f712c18daaf80055dac128f0227f12c5663e))
* **helm:** 修复 regexReplaceAll pipeline 参数顺序错误导致 Job 名为空 ([#162](https://github.com/tomturing/hci-troubleshoot-platform/issues/162)) ([e6075c5](https://github.com/tomturing/hci-troubleshoot-platform/commit/e6075c5c5cb75f686d62e74daa2c16b995aea1d9))
* **helm:** 同步 ai_client.py 到 ConfigMap 版本，修复 provider_api_key 参数缺失 ([#165](https://github.com/tomturing/hci-troubleshoot-platform/issues/165)) ([33bb311](https://github.com/tomturing/hci-troubleshoot-platform/commit/33bb3113ac5a8a2a21d649850f31da655e42eaa8))
* **helm:** 彻底移除 aiClientPatch 双重维护机制 ([#166](https://github.com/tomturing/hci-troubleshoot-platform/issues/166)) ([28b0ed7](https://github.com/tomturing/hci-troubleshoot-platform/commit/28b0ed722644d2cde29f3e137664e33541ccfda1))
* **helm:** 移除 HookSucceeded delete-policy 避免 ArgoCD sync 卡住 ([#164](https://github.com/tomturing/hci-troubleshoot-platform/issues/164)) ([ba437f2](https://github.com/tomturing/hci-troubleshoot-platform/commit/ba437f22605b38348236b4be57d6ab21c6c0fc0a))

## [2.4.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.3.3...v2.4.0) (2026-04-17)


### ✨ 新功能

* dashscope 多模型直连 ([#158](https://github.com/tomturing/hci-troubleshoot-platform/issues/158)) ([d0dd51a](https://github.com/tomturing/hci-troubleshoot-platform/commit/d0dd51a9942d871836575aec5e77508b9f1acf1f))
* **gitops:** 引入 App of Apps 分层架构，统一命名规范（D-001） ([#159](https://github.com/tomturing/hci-troubleshoot-platform/issues/159)) ([778eae7](https://github.com/tomturing/hci-troubleshoot-platform/commit/778eae7c3ec1d3437534ff746ef88b84f4b2f700))


### 🐛 Bug 修复

* **helm:** 修复 db-migrate hook 死锁缺陷（Revision 命名 + delete-policy） ([#157](https://github.com/tomturing/hci-troubleshoot-platform/issues/157)) ([7b7fc6a](https://github.com/tomturing/hci-troubleshoot-platform/commit/7b7fc6ad61edc51a96df5afb588aebd297f97169))
* **kb-client:** 修复 categories API 三层 Bug 导致 S0 分类注入为空 ([#156](https://github.com/tomturing/hci-troubleshoot-platform/issues/156)) ([472ba55](https://github.com/tomturing/hci-troubleshoot-platform/commit/472ba55f63d52baf9aa1b5625f1e265299809c43))
* nginx 动态 DNS 解析解决启动时 upstream 未就绪问题 ([#160](https://github.com/tomturing/hci-troubleshoot-platform/issues/160)) ([1aa014f](https://github.com/tomturing/hci-troubleshoot-platform/commit/1aa014f4b3925f0c3f10bd856383022f0709c617))
* **obs:** 修复 Tempo→Loki 跳转按钮不显示；固定观测组件镜像版本 ([#154](https://github.com/tomturing/hci-troubleshoot-platform/issues/154)) ([857b0eb](https://github.com/tomturing/hci-troubleshoot-platform/commit/857b0eb2127a5752e071cff9dc5527fbd324da9c))


### 📝 文档

* 全量文档同步更新 — App of Apps架构/dashscope多模型/nginx动态DNS (PR [#157](https://github.com/tomturing/hci-troubleshoot-platform/issues/157)-160) ([#161](https://github.com/tomturing/hci-troubleshoot-platform/issues/161)) ([9785846](https://github.com/tomturing/hci-troubleshoot-platform/commit/97858469b55630cf5c3b7e3110ea3b2948b3f137))

## [2.3.3](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.3.2...v2.3.3) (2026-04-16)


### 🐛 Bug 修复

* **case-service:** 补充 prometheus-client 依赖，修复新镜像启动崩溃 ([#151](https://github.com/tomturing/hci-troubleshoot-platform/issues/151)) ([36155b5](https://github.com/tomturing/hci-troubleshoot-platform/commit/36155b5bb3097544c683b989c9522330130b5544))
* **ci:** 绕过 hci-platform-env workflow dispatch，改为直接 git push 同步镜像标签 ([#153](https://github.com/tomturing/hci-troubleshoot-platform/issues/153)) ([a34c19d](https://github.com/tomturing/hci-troubleshoot-platform/commit/a34c19d6130dee72bbe6a3bb2a897cc760e94448))

## [2.3.2](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.3.1...v2.3.2) (2026-04-16)


### 🐛 Bug 修复

* **obs:** 全面修复可观测性 14 项缺陷 ([#149](https://github.com/tomturing/hci-troubleshoot-platform/issues/149)) ([eb4e387](https://github.com/tomturing/hci-troubleshoot-platform/commit/eb4e3874603b946590dee8045476d60530211a78))

## [2.3.1](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.3.0...v2.3.1) (2026-04-15)


### 🐛 Bug 修复

* **db:** 清理 Alembic 遗留触发器，修复 message_count 双倍计数 bug ([#147](https://github.com/tomturing/hci-troubleshoot-platform/issues/147)) ([fc3f171](https://github.com/tomturing/hci-troubleshoot-platform/commit/fc3f171704daca7ee51b5bd73c572bbfc15400d2))
* SOP 发布 HTTP 500 改为带原因的错误提示 + 管理台按钮布局修复 ([#146](https://github.com/tomturing/hci-troubleshoot-platform/issues/146)) ([750d23b](https://github.com/tomturing/hci-troubleshoot-platform/commit/750d23ba84e3e33c058a77283fb0f10892d2c883))

## [2.3.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.2.0...v2.3.0) (2026-04-14)


### ✨ 新功能

* add terminal_bridge.exe to downloads for customer UI ([4b78d82](https://github.com/tomturing/hci-troubleshoot-platform/commit/4b78d829f9b3bff4fd3a241489dda7d8a771eab5))
* add terminal_bridge.exe to downloads for customer UI ([6f0844a](https://github.com/tomturing/hci-troubleshoot-platform/commit/6f0844a0c6757391d56df95aa1e7f8ec4f7d2918))
* **admin-ui:** KBD 条目详情 UI 重构（分类 select + 截图 accordion + 内联编辑） ([c97cbf3](https://github.com/tomturing/hci-troubleshoot-platform/commit/c97cbf3d937adad12692756df9942907561e2440))
* **admin:** 调整侧边栏菜单顺序，按业务优先级排列 ([#103](https://github.com/tomturing/hci-troubleshoot-platform/issues/103)) ([d999647](https://github.com/tomturing/hci-troubleshoot-platform/commit/d999647abc593dee49c35ed639c58c9ede0a9ecc))
* **api-gateway:** Task 37 SSH 代理与终端交互后端能力 [model: opus] ([96b4212](https://github.com/tomturing/hci-troubleshoot-platform/commit/96b42128a64289f479c97ed2dc9c112460d2ba68))
* ArgoCD 升级脚本增强 + Git commit 标识规范 ([#76](https://github.com/tomturing/hci-troubleshoot-platform/issues/76)) ([4db398a](https://github.com/tomturing/hci-troubleshoot-platform/commit/4db398a9d5e510e34ce29d1a72aae712287deea8))
* **case-service:** 实现 QualityScoreService (Task 30-C) [model: opus] ([2e1d412](https://github.com/tomturing/hci-troubleshoot-platform/commit/2e1d4121639fee7d88399493668b489422fc8fbd))
* **case-service:** 实现工单关闭逻辑 (Task 30-B) [model: sonnet] ([d8abffe](https://github.com/tomturing/hci-troubleshoot-platform/commit/d8abffe65df120195ee8173b5854a31561f91f76))
* **ci:** dev/staging 环境自动晋级，prod 需手动审批 ([#69](https://github.com/tomturing/hci-troubleshoot-platform/issues/69)) ([c55820f](https://github.com/tomturing/hci-troubleshoot-platform/commit/c55820f5c28175bbe0c2e63c5200e81a17b7efe6))
* **ci:** 解耦 db-migrate 镜像构建，仅 schema 变更时触发（方案 A1） ([#139](https://github.com/tomturing/hci-troubleshoot-platform/issues/139)) ([1e6dcfd](https://github.com/tomturing/hci-troubleshoot-platform/commit/1e6dcfd2e42dd8e83eda4230359650aaaa14c9c8))
* **conversation:** 实现用户评分API (Task 30-E) ([17d4928](https://github.com/tomturing/hci-troubleshoot-platform/commit/17d492841beb8e3c58c4f016f16c1f98ec9c571c))
* **conversation:** 实现重复提问检测功能 ([3325e8b](https://github.com/tomturing/hci-troubleshoot-platform/commit/3325e8b113cf726ae186d1e1fc84fac3a4fc5cdb))
* **customer:** Markdown 渲染改造，支持标题、列表、引用、代码块 ([c0c5f1c](https://github.com/tomturing/hci-troubleshoot-platform/commit/c0c5f1cf34b5c937e03e57546f15a54301e6155e))
* **customer:** Task 35 命令卡片化与一键发送到终端 ([662e351](https://github.com/tomturing/hci-troubleshoot-platform/commit/662e3514a5a6d20177966e306fc19f6b307b7281))
* **customer:** Task 36 侧边栏 SSH 登录与交互终端页面 ([dfe65cd](https://github.com/tomturing/hci-troubleshoot-platform/commit/dfe65cdea27d3db9681236577db1df622e3e8e6e))
* **database:** 数据库表清理迁移脚本 ([#91](https://github.com/tomturing/hci-troubleshoot-platform/issues/91)) ([0eb1535](https://github.com/tomturing/hci-troubleshoot-platform/commit/0eb1535da03a439cb49954f5defb91a8de5f1713))
* **data:** postgres 服务支持 NodePort 配置 ([#43](https://github.com/tomturing/hci-troubleshoot-platform/issues/43)) ([a4d5104](https://github.com/tomturing/hci-troubleshoot-platform/commit/a4d51044767207841764fe5ab74bc4ddfed76c6f))
* **db:** 引入 Atlas 声明式 Schema 管理，替换 dbmate + ConfigMap 手动同步方案 ([#124](https://github.com/tomturing/hci-troubleshoot-platform/issues/124)) ([a62fce5](https://github.com/tomturing/hci-troubleshoot-platform/commit/a62fce59a7de584d7c446deeeb14a917c8a3b2ae))
* **db:** 新增评分评价体系迁移脚本 (migrate_evaluation_v1) ([4ae2659](https://github.com/tomturing/hci-troubleshoot-platform/commit/4ae2659f96d9d9be814034f18feedcc18fd1308b))
* **db:** 添加数据库迁移同步自动化机制 ([#116](https://github.com/tomturing/hci-troubleshoot-platform/issues/116)) ([3320dd0](https://github.com/tomturing/hci-troubleshoot-platform/commit/3320dd03f62b82ce1c563fc7360c4dc55d26cdc4))
* **etl:** Task 25 P0 产品案例ETL数据管道 - fetcher/converter/enricher/reviewer_cli/ingestor/pipeline 六脚本 + ETL依赖 ([f12d8ff](https://github.com/tomturing/hci-troubleshoot-platform/commit/f12d8ff95f8de083268af285f14c1c656b611fd2))
* **frontend:** 实现工单关闭后的评分卡组件 ([6b67221](https://github.com/tomturing/hci-troubleshoot-platform/commit/6b67221e15bbd8d0928c05ae438b1f01f5f924ce))
* Git 标识规则完善（增加 hostname + gpr 函数） ([#79](https://github.com/tomturing/hci-troubleshoot-platform/issues/79)) ([9d0c7aa](https://github.com/tomturing/hci-troubleshoot-platform/commit/9d0c7aa9d1887513fa4c0c1775918a2331c47461))
* **gitops:** 完成 hub-spoke prod 集群应用与发布手册修复 ([#61](https://github.com/tomturing/hci-troubleshoot-platform/issues/61))（aihci_copilot） ([8f6bb81](https://github.com/tomturing/hci-troubleshoot-platform/commit/8f6bb81cc63d8a9506d11d27cc105ee246a376df))
* **helm:** 支持 ghcr.io imagePullSecret 认证拉取镜像 ([#10](https://github.com/tomturing/hci-troubleshoot-platform/issues/10)) ([5513657](https://github.com/tomturing/hci-troubleshoot-platform/commit/5513657fa726091144cf22e04c464cde950ff61d))
* **helm:** 支持 ghcr.io imagePullSecret 认证拉取镜像 ([#31](https://github.com/tomturing/hci-troubleshoot-platform/issues/31)) ([c0cadf0](https://github.com/tomturing/hci-troubleshoot-platform/commit/c0cadf083229095b1a54e4cb6cc7273ee25c31b1))
* K3s集群健壮性改进计划 Sprint1/2/3 全量实现 ([#62](https://github.com/tomturing/hci-troubleshoot-platform/issues/62))（ubuntu_sz_copilot） ([2172354](https://github.com/tomturing/hci-troubleshoot-platform/commit/2172354530ecb4fb7a75fb0f0536ce4e19d52c77))
* **kb-service:** Helm template 支持 llmBaseUrl/llmModel 覆盖 ZAI 配置 ([89dfa2d](https://github.com/tomturing/hci-troubleshoot-platform/commit/89dfa2d106932a5b24ea1b42017e45b5c2c3d832))
* **kb-service:** 实现知识库模块核心功能 ([#87](https://github.com/tomturing/hci-troubleshoot-platform/issues/87)) ([eb5265f](https://github.com/tomturing/hci-troubleshoot-platform/commit/eb5265f29386667254437b8967e6db3970d924f6))
* **kb-service:** 扩展知识库 API + 代码质量修复 ([#89](https://github.com/tomturing/hci-troubleshoot-platform/issues/89)) ([c9185e0](https://github.com/tomturing/hci-troubleshoot-platform/commit/c9185e020de5a2140559e6d609ea652f3369f294))
* **kbd:** KBD知识生产管道v1 + 审核UI + 59个单元测试 ([#83](https://github.com/tomturing/hci-troubleshoot-platform/issues/83)) ([33dcca3](https://github.com/tomturing/hci-troubleshoot-platform/commit/33dcca341fa2d9307f5b963412f761d2ea7c2844))
* KBD截图识别重构——Vision+分析双LLM+v2格式+前端类型差异化展示 ([#143](https://github.com/tomturing/hci-troubleshoot-platform/issues/143)) ([1e38e5d](https://github.com/tomturing/hci-troubleshoot-platform/commit/1e38e5d9d95a04351bc815567f9b3fd9f9606ed6))
* **kb:** 完成 SOP 批量摄入 + 修复大文件 Embedding 超时问题 ([0231897](https://github.com/tomturing/hci-troubleshoot-platform/commit/02318976f25ccb336b90e0cb8393cbaa756871a2))
* **observability:** Task 28 Prometheus进K3s - Helm模板+RBAC+Grafana数据源+5服务/metrics端点 ([969b02c](https://github.com/tomturing/hci-troubleshoot-platform/commit/969b02c002e15f4220725a148f1ac8b404fab08b))
* **observability:** 增加Pod异常监测与飞书告警能力 ([d61f6c5](https://github.com/tomturing/hci-troubleshoot-platform/commit/d61f6c58a6de09ab7dd9725f138979716e6f061f))
* **obs:** hci-platform-obs 引入 env 仓库 values，Grafana 密码由环境仓库统一管理 ([#8](https://github.com/tomturing/hci-troubleshoot-platform/issues/8)) ([aec7b98](https://github.com/tomturing/hci-troubleshoot-platform/commit/aec7b9821c43532ed37e1abc9ddf9ec6b1cd8dbd))
* **openclaw:** 挂载宿主机 /srv/openclaw/shared 到容器 /shared（只读） ([2273545](https://github.com/tomturing/hci-troubleshoot-platform/commit/2273545fe8d5d479e15ad65b028cc2bc5eee3a90))
* **postgres:** 支持 NodePort 配置以便外部访问数据库 ([#42](https://github.com/tomturing/hci-troubleshoot-platform/issues/42)) ([e16e664](https://github.com/tomturing/hci-troubleshoot-platform/commit/e16e6644a467af4a6acf935bc72d69a2f75bcecd))
* **S0/S6:** S0候选确认模式v2 + S6三选项闭环 + 数据库v6.3全量落地 ([a356183](https://github.com/tomturing/hci-troubleshoot-platform/commit/a356183aa2c9efbe73768c79ac37c140e1a04db9))
* **s0:** S0 意图识别与分类基线重构 ([#88](https://github.com/tomturing/hci-troubleshoot-platform/issues/88)) ([a9fbd31](https://github.com/tomturing/hci-troubleshoot-platform/commit/a9fbd3102df66f13314e25a377775eccda392d90))
* **sop:** 新增虚拟机开关机失败排障SOP知识库 ([2905a3c](https://github.com/tomturing/hci-troubleshoot-platform/commit/2905a3c19b6aa1332b6427e6c28b9377a3f01262))
* **task30+etl:** Task 30 A-F 全量验证通过 + ETL P0 KB数据建设 ([27a35ff](https://github.com/tomturing/hci-troubleshoot-platform/commit/27a35ff5f2a13d4a82109be042b06f2a46a31271))
* **terminal-bridge:** nginx 支持 exe 下载 + release 脚本检查 + public 目录占位 ([8895a3c](https://github.com/tomturing/hci-troubleshoot-platform/commit/8895a3c9b02a2cc0eb5958dcd167739c12e02ddb))
* **terminal-bridge:** 完整源码+打包脚本+release脚本优化 ([e453069](https://github.com/tomturing/hci-troubleshoot-platform/commit/e45306941e23c7c6dcddfab03388afd015362e46))
* **terminal-bridge:** 添加静态文件托管支持 terminal_bridge.exe 下载 ([fdac9d1](https://github.com/tomturing/hci-troubleshoot-platform/commit/fdac9d1207d128836a94509198190765468f53fe))
* **test:** Task 29 Phase 4 测试覆盖 + CI/CD ([b71bafa](https://github.com/tomturing/hci-troubleshoot-platform/commit/b71bafa2815ac17b4ca09107d38d207b3fcd6b49))
* **ui:** 实现AI回复三阶段渲染状态机（thinking→流式→CommandBlock升级） ([e7842cb](https://github.com/tomturing/hci-troubleshoot-platform/commit/e7842cbcf23d6c023b7281c34bfef5dc7b106b48))
* Vision prompt 增加背景色字段 + 前端截图多维度分类 ([057e3d5](https://github.com/tomturing/hci-troubleshoot-platform/commit/057e3d5ee9196a8e26ff689fd6ef5c907a0bd62c))
* 三层自动化防御 - PostSync冒烟测试 + CI探针路径对齐检查 ([#67](https://github.com/tomturing/hci-troubleshoot-platform/issues/67))（ubuntu_sz_copilot） ([fe67a22](https://github.com/tomturing/hci-troubleshoot-platform/commit/fe67a22bb0ebaf5277c73dda65c1525138de2f9e))
* 优化SSH终端与历史工单交互及文案样式 ([c00cf2f](https://github.com/tomturing/hci-troubleshoot-platform/commit/c00cf2f05871d74c9af4e2a1dc5c0b181bef4ac4))
* 升级 ArgoCD 到 v3.3.6 并新增 repo-server copyutil watchdog ([882ac86](https://github.com/tomturing/hci-troubleshoot-platform/commit/882ac866bb915cc3ce47cdf085ca71e65b6b7b40))
* 增加K3s一键发布与选择性镜像构建 ([c20d524](https://github.com/tomturing/hci-troubleshoot-platform/commit/c20d524df1229f6dea9c03fa82c8606d35ee68c4))
* 新增 SOP 管理 UI 及后端接口，扩展 KBD 编辑和重发布功能 ([89e677e](https://github.com/tomturing/hci-troubleshoot-platform/commit/89e677eb285be552ef6179ec39328b935500e68d))
* 添加 ArgoCD Server NodePort 覆盖配置（端口 30808） ([#81](https://github.com/tomturing/hci-troubleshoot-platform/issues/81)) ([c14ec5f](https://github.com/tomturing/hci-troubleshoot-platform/commit/c14ec5f5e08dfadf0a7d0bf21388c9dbb8f2dc51))
* 避坑指南体系统一化升级 ([#60](https://github.com/tomturing/hci-troubleshoot-platform/issues/60))（aihci_copilot） ([5779f8f](https://github.com/tomturing/hci-troubleshoot-platform/commit/5779f8f43cfdcb0b3e518c949aecf2d2f684290e))
* 项目交付标准化整改（Sprint 1/2/3） ([#3](https://github.com/tomturing/hci-troubleshoot-platform/issues/3)) ([dd1c1c1](https://github.com/tomturing/hci-troubleshoot-platform/commit/dd1c1c13266f2866b17d868a6d443058610b26e6))


### 🐛 Bug 修复

* **admin:** 修复侧边栏菜单顺序，按 order 字段排序 ([#110](https://github.com/tomturing/hci-troubleshoot-platform/issues/110)) ([49429f0](https://github.com/tomturing/hci-troubleshoot-platform/commit/49429f0ba0025c14d7f80d4bfa92d95970b8ec05))
* **admin:** 修复管理台分类基线/KBD审核API路由，清理废弃知识审核模块 ([#115](https://github.com/tomturing/hci-troubleshoot-platform/issues/115)) ([6e566d2](https://github.com/tomturing/hci-troubleshoot-platform/commit/6e566d27906d62b74046917eb1dfc24fb0c6ec0a))
* **admin:** 删除classify.py死代码端点解除路由冲突，网关代理改用自身Token鉴权 ([#119](https://github.com/tomturing/hci-troubleshoot-platform/issues/119)) ([80aeabd](https://github.com/tomturing/hci-troubleshoot-platform/commit/80aeabd8639fc18a946adf3a0d14c209f3acb12b))
* **ai_client:** 根据 endpoint 类型动态选择正确的 API 路径 ([7ea0276](https://github.com/tomturing/hci-troubleshoot-platform/commit/7ea027645b4f26ace04b418ebda3c5a8fa65fd99))
* **ai_client:** 检测 AI 服务返回空响应（rate limit 静默失败） ([7e6ef18](https://github.com/tomturing/hci-troubleshoot-platform/commit/7e6ef18204421b1c25a9b84e4e98e0ac839eee8f))
* **alertmanager:** 修复 Clash fake-IP 导致飞书 webhook 无法连通（PIT-034） ([6237665](https://github.com/tomturing/hci-troubleshoot-platform/commit/6237665bb64bb2c9c5e6932968601810e1d5d9c3))
* **argocd:** argocd-ops 扩展为 App of Apps 守护 argo-apps/local/，防止 Application 被手动旧 yaml 覆盖（PIT-043） ([217556b](https://github.com/tomturing/hci-troubleshoot-platform/commit/217556b5065a22a56a3f52232a2e507e8a7bf291))
* **case-service:** prometheus_client 条件导入修复基础镜像兼容性\n\n- quality_score.py: 将 prometheus_client 改为条件导入，未安装时跳过指标上报\n- main.py: 将 prometheus_client 改为条件导入，/metrics 端点在无库时返回空内容\n- 解决 K3s 部署在旧镜像(2026.03.07-1)上缺少 prometheus_client 时服务崩溃的问题" ([d9d6b3b](https://github.com/tomturing/hci-troubleshoot-platform/commit/d9d6b3be0fe5b7ccdf7a2fdee22bf31251af2756))
* **ci:** build-hci-openclaw 登录 ghcr.io 改用 GHCR_PAT 以支持首次创建新包 ([#48](https://github.com/tomturing/hci-troubleshoot-platform/issues/48))（ubuntu_sz_copilot） ([7bc04dd](https://github.com/tomturing/hci-troubleshoot-platform/commit/7bc04dda2e0c599b08897aab936db8b679d6c6e9))
* **ci:** upload-sarif continue-on-error ([#15](https://github.com/tomturing/hci-troubleshoot-platform/issues/15)) ([6d55bdc](https://github.com/tomturing/hci-troubleshoot-platform/commit/6d55bdc33e1182229fb801da0820985789ad7574))
* **ci:** 为 release-please PR 添加状态检查旁路，解决发版 PR 永久卡住问题 ([#111](https://github.com/tomturing/hci-troubleshoot-platform/issues/111)) ([c644b81](https://github.com/tomturing/hci-troubleshoot-platform/commit/c644b815dd5c47ff5cac46a671b78c4b7caaac19))
* **ci:** 修复 build-and-push 和 auto-deploy-dev 的 always() 条件 ([#40](https://github.com/tomturing/hci-troubleshoot-platform/issues/40)) ([cc4d883](https://github.com/tomturing/hci-troubleshoot-platform/commit/cc4d8835bfa13c4304d33fca82891a8ea9aec737))
* **ci:** 修复 doc-review-agent workflow 因无效 models:read 权限导致持续失败 ([#118](https://github.com/tomturing/hci-troubleshoot-platform/issues/118)) ([8c7563a](https://github.com/tomturing/hci-troubleshoot-platform/commit/8c7563a706b20ad8d5eaae0f782256505760e725))
* **ci:** 修复 Docker build context ([#13](https://github.com/tomturing/hci-troubleshoot-platform/issues/13)) ([ac956ef](https://github.com/tomturing/hci-troubleshoot-platform/commit/ac956efdb41f1ee0cf0dd3dc92951beefa293250))
* **ci:** 修复 push 事件时所有 job 被级联跳过的问题 ([#12](https://github.com/tomturing/hci-troubleshoot-platform/issues/12)) ([2bd7021](https://github.com/tomturing/hci-troubleshoot-platform/commit/2bd7021862de5ea873b053da84ea34cd1c334edf))
* **ci:** 修复 push 到 main 时构建链被跳过的问题 ([#38](https://github.com/tomturing/hci-troubleshoot-platform/issues/38)) ([32eb7fb](https://github.com/tomturing/hci-troubleshoot-platform/commit/32eb7fbe25450518179b4d163a4929592181947c))
* **ci:** 将 hci-openclaw 从主构建矩阵拆分为独立 workflow ([#47](https://github.com/tomturing/hci-troubleshoot-platform/issues/47))（ubuntu_sz_copilot） ([cd5a683](https://github.com/tomturing/hci-troubleshoot-platform/commit/cd5a683b4a15b844eb9cb5aa66bdcd514964f750))
* **ci:** 将 hci-openclaw 从主构建矩阵拆分为独立 workflow ([#49](https://github.com/tomturing/hci-troubleshoot-platform/issues/49)) ([5ae7744](https://github.com/tomturing/hci-troubleshoot-platform/commit/5ae77449c4bc29a6900e600446d5f18237fa15dc))
* **ci:** 添加 always() 条件确保构建链在依赖跳过时仍能运行 ([#39](https://github.com/tomturing/hci-troubleshoot-platform/issues/39)) ([3c8dbef](https://github.com/tomturing/hci-troubleshoot-platform/commit/3c8dbefeec5761c594487a6ddf621352f852d763))
* **ci:** 环境同步改为串行执行，避免并发竞争 ([#104](https://github.com/tomturing/hci-troubleshoot-platform/issues/104)) ([04f51b9](https://github.com/tomturing/hci-troubleshoot-platform/commit/04f51b92de0a08f32ab750e1c3ce85006c0cd495))
* conversation-service DNS 搜索域硬编码旧命名空间 hci-troubleshoot ([#30](https://github.com/tomturing/hci-troubleshoot-platform/issues/30)) ([26410df](https://github.com/tomturing/hci-troubleshoot-platform/commit/26410dffefbd03e937befc037b9158576cf3e150))
* **conversation-service:** SSE 流异常添加日志记录 ([424d125](https://github.com/tomturing/hci-troubleshoot-platform/commit/424d1251481d496904a760671e66f332360b0329))
* **conversation-service:** 修复 KBClient._headers 缺失和日志导入问题 ([73546b4](https://github.com/tomturing/hci-troubleshoot-platform/commit/73546b48ec2b3bc3f6be6e989d329d59f61a4930))
* **conversation-service:** 修正 MessageRole.USER 为 MessageRole.user ([cf7a002](https://github.com/tomturing/hci-troubleshoot-platform/commit/cf7a002cabf58c921f98531863eafa5f6a7ef322))
* **conversation-service:** 补充 ConversationRepository.get_recent_user_messages 方法 ([e1d6167](https://github.com/tomturing/hci-troubleshoot-platform/commit/e1d6167e38477e70425f4ab0a09e55c4ce24f68c))
* **conversation-service:** 补充缺失的 Repo 方法并重构 prompt_audit ([da33a4e](https://github.com/tomturing/hci-troubleshoot-platform/commit/da33a4e052e5a0fb3aa145faaeeea8bdf5ae8d24))
* **conversation:** AI回复消息后台任务使用独立session保存 ([59abbb2](https://github.com/tomturing/hci-troubleshoot-platform/commit/59abbb22b7ab986511209485972809cb5bf4c0f6))
* **conversation:** get_messages 改用独立 session，彻底修复 assistant 消息不落库 ([bc762b5](https://github.com/tomturing/hci-troubleshoot-platform/commit/bc762b554945ee819364038252f687401f5fef23))
* **conversation:** 恢复 9d56852 回退的 knowledge-rag 依赖注入链 ([#45](https://github.com/tomturing/hci-troubleshoot-platform/issues/45))（ubuntu_sz_copilot） ([9595b2e](https://github.com/tomturing/hci-troubleshoot-platform/commit/9595b2ede8ad6e0327900c42f1dbfe11341bd492))
* **dashboard:** 三个面板添加 Init 容器指标覆盖 ImagePullBackOff 初始化容器异常 ([#57](https://github.com/tomturing/hci-troubleshoot-platform/issues/57)) ([9fb8823](https://github.com/tomturing/hci-troubleshoot-platform/commit/9fb8823848db0ed9184454d47e0c19ddcab17676))
* **database:** 在 desired_schema.sql 中添加 pending_resolution 列 ([8ac2eee](https://github.com/tomturing/hci-troubleshoot-platform/commit/8ac2eee2b3027d2961ce77b0f7aee07dd409eb2f))
* **db:** 修复 atlas 二进制路径 ([#128](https://github.com/tomturing/hci-troubleshoot-platform/issues/128)) ([e15de63](https://github.com/tomturing/hci-troubleshoot-platform/commit/e15de63fae825be0dac02b5ea65b1cb097504092))
* **db:** 修复 atlas.sum checksum 格式 ([c6e4b10](https://github.com/tomturing/hci-troubleshoot-platform/commit/c6e4b10d3a7e9265aa4dceb7024142da2310629a))
* **db:** 修复 atlas.sum checksum 格式 ([#130](https://github.com/tomturing/hci-troubleshoot-platform/issues/130)) ([c6e4b10](https://github.com/tomturing/hci-troubleshoot-platform/commit/c6e4b10d3a7e9265aa4dceb7024142da2310629a))
* **db:** 修复 db-migrate 镜像构建失败 ([#127](https://github.com/tomturing/hci-troubleshoot-platform/issues/127)) ([4682d9c](https://github.com/tomturing/hci-troubleshoot-platform/commit/4682d9c608a942f8f77b32ee57f1a56266dbd751))
* **db:** 修复 idle-in-transaction 导致 message INSERT 阻塞 ([be798ee](https://github.com/tomturing/hci-troubleshoot-platform/commit/be798ee57689e62a185f76931ba90869bf9e78f7))
* **db:** 修复迁移链断裂，解决 tool_audit_log 不存在导致的阻塞 ([#121](https://github.com/tomturing/hci-troubleshoot-platform/issues/121)) ([2fd6997](https://github.com/tomturing/hci-troubleshoot-platform/commit/2fd699758ebfafb567e515b71f83aad618c8df95))
* **db:** 修正 migration 文件命名格式 — 纯数字前缀以匹配 dbmate version 提取正则 ([#85](https://github.com/tomturing/hci-troubleshoot-platform/issues/85)) ([5894c7c](https://github.com/tomturing/hci-troubleshoot-platform/commit/5894c7c50fe2b32d88ea6ad4aa4af8bd18d6bedd))
* **db:** 添加 HOME 环境变量解决 atlas 写入权限问题 ([f204945](https://github.com/tomturing/hci-troubleshoot-platform/commit/f204945bcdcea512ee4372c9a1d38f9c890a1bf1))
* **db:** 添加 HOME 环境变量解决 atlas 写入权限问题 ([#129](https://github.com/tomturing/hci-troubleshoot-platform/issues/129)) ([f204945](https://github.com/tomturing/hci-troubleshoot-platform/commit/f204945bcdcea512ee4372c9a1d38f9c890a1bf1))
* **db:** 真正实现 Atlas 声明式 Schema 管理（schema apply + psql extras） ([#132](https://github.com/tomturing/hci-troubleshoot-platform/issues/132)) ([175f7d5](https://github.com/tomturing/hci-troubleshoot-platform/commit/175f7d51cbd1eef959f58f09c51fe906c1f16c57))
* **db:** 移除 --allow-dirty 标志（与 --baseline 冲突） ([e678fc0](https://github.com/tomturing/hci-troubleshoot-platform/commit/e678fc05e9c03a2760d3f2b6186580f52f6c709c))
* **db:** 移除 --allow-dirty 标志（与 --baseline 冲突） ([#131](https://github.com/tomturing/hci-troubleshoot-platform/issues/131)) ([e678fc0](https://github.com/tomturing/hci-troubleshoot-platform/commit/e678fc05e9c03a2760d3f2b6186580f52f6c709c))
* **deploy:** 修复 dbMigrate.image 格式，使 env-repo-sync 能自动更新 tag ([#133](https://github.com/tomturing/hci-troubleshoot-platform/issues/133)) ([6775ecc](https://github.com/tomturing/hci-troubleshoot-platform/commit/6775eccbb31c00b0fdee42ca5bf3186e4c613330))
* **deploy:** 修复 staging 环境 Pod 崩溃问题 ([#64](https://github.com/tomturing/hci-troubleshoot-platform/issues/64)) ([5be4a0c](https://github.com/tomturing/hci-troubleshoot-platform/commit/5be4a0c5460e8ae569ccc15a8a4bd1fefd7dfdb4))
* **deploy:** 修复健康检查路径和 Node.js 内存配置 ([#65](https://github.com/tomturing/hci-troubleshoot-platform/issues/65)) ([1db5973](https://github.com/tomturing/hci-troubleshoot-platform/commit/1db597304adb5a7d2f449bcebfcdf7ed016701de))
* **docs:** 将 admin-ui 事件文档移至模块目录下 ([#105](https://github.com/tomturing/hci-troubleshoot-platform/issues/105)) ([84b1a08](https://github.com/tomturing/hci-troubleshoot-platform/commit/84b1a08d85aa0b7477327937ca56dbb2e76edf44))
* **error-handling:** 透传真实错误信息而非笼统的"内部错误"提示 ([593ea78](https://github.com/tomturing/hci-troubleshoot-platform/commit/593ea782bfd0a62b5ed4e9443ec140389bb0db42))
* **frontend:** admin tsconfig.node.json 补 composite:true，修复 vue-tsc -b 构建失败 ([bd8707b](https://github.com/tomturing/hci-troubleshoot-platform/commit/bd8707bc2f7b1e9dce103ec1b87420625e1746a4))
* **frontend:** 彻底规避 Docker 构建卡死和 lockfile 问题 ([e0d9f6b](https://github.com/tomturing/hci-troubleshoot-platform/commit/e0d9f6be86d746d2eb462183764eafc9c2923390))
* **gitops:** 固定Helm releaseName以消除不可变字段同步失败 ([43614a4](https://github.com/tomturing/hci-troubleshoot-platform/commit/43614a4fab7d85fffbb620bedda586eb8dccc796))
* hci-platform-data-staging 使用多源配置引用环境仓库 values ([#75](https://github.com/tomturing/hci-troubleshoot-platform/issues/75)) ([1fef02a](https://github.com/tomturing/hci-troubleshoot-platform/commit/1fef02a31ac50ff677b53f014aa79fed3cb5f6be))
* **helm:** Alembic deprecated Job 误启用时显式失败，消除静默掩盖风险 ([#122](https://github.com/tomturing/hci-troubleshoot-platform/issues/122)) ([3bf0061](https://github.com/tomturing/hci-troubleshoot-platform/commit/3bf0061549038420158c7a38a20f9261015c5aec))
* **helm:** DATABASE_URL 改用可配置 postgresHost，支持跨 namespace 数据库访问 ([#23](https://github.com/tomturing/hci-troubleshoot-platform/issues/23)) ([200fe00](https://github.com/tomturing/hci-troubleshoot-platform/commit/200fe009b446c7d9a82234b0eca2aae0a0a66a7c))
* **helm:** hci-platform values.yaml 补充 postgres.nodePort 字段 ([#44](https://github.com/tomturing/hci-troubleshoot-platform/issues/44))（ubuntu_sz_claude） ([08f8f0f](https://github.com/tomturing/hci-troubleshoot-platform/commit/08f8f0ff00c854ac2d0c2d9c507aef424dc28ec1))
* **helm:** 去除 nginx emptyDir 重复配置（PR [#63](https://github.com/tomturing/hci-troubleshoot-platform/issues/63) 与 [#64](https://github.com/tomturing/hci-troubleshoot-platform/issues/64) 冲突遗留） ([#66](https://github.com/tomturing/hci-troubleshoot-platform/issues/66))（ubuntu_sz_copilot） ([0522700](https://github.com/tomturing/hci-troubleshoot-platform/commit/05227005eb64a611dff03b8b9921d580843d84cb))
* **helm:** 拆分 postgres Service 为 Headless + 独立 NodePort ([#50](https://github.com/tomturing/hci-troubleshoot-platform/issues/50)) ([5ac86a7](https://github.com/tomturing/hci-troubleshoot-platform/commit/5ac86a7b98fdb65fc521c4f0f87ff6ea935662f0))
* **infra:** 集群最佳实践补丁 — GitOps/资源限制/存储/RS清理 ([#55](https://github.com/tomturing/hci-troubleshoot-platform/issues/55))（ubuntu_gs_copilot） ([e67cb67](https://github.com/tomturing/hci-troubleshoot-platform/commit/e67cb67a1747b8909177bbc67a72740c0d452365))
* **kb_client:** 修复 get_categories_grouped 和 increment_category_hit URL 缺少协议前缀 ([4102f42](https://github.com/tomturing/hci-troubleshoot-platform/commit/4102f4269196948a15e27c779d18b3f7540051e8))
* kb-service 204 response_class + Trivy 仅上报 ([#14](https://github.com/tomturing/hci-troubleshoot-platform/issues/14)) ([3f5326d](https://github.com/tomturing/hci-troubleshoot-platform/commit/3f5326d18e1dba43c1909c8d87cf95042b16f73e))
* **kb-service:** classify base_url 自动添加 /v1 后缀 ([bc89288](https://github.com/tomturing/hci-troubleshoot-platform/commit/bc89288fa864815a00f68f9f336f7863b6e67e09))
* **kb-service:** classify 自动适配 openclaw proxy 的 token 和模型名 ([d19ecfa](https://github.com/tomturing/hci-troubleshoot-platform/commit/d19ecfa79cb5b4e2d4cb7c3fc1205ccb606525da))
* **kb-service:** LLM_MODEL 默认值改为 OPENCLAW_DEFAULT_MODEL fallback ([dd20620](https://github.com/tomturing/hci-troubleshoot-platform/commit/dd206208cadf60defb643bf1cd2bde456cc2abe8))
* **kb-service:** response_model=None 修复 FastAPI 0.109.0 status_code=204 断言 ([#16](https://github.com/tomturing/hci-troubleshoot-platform/issues/16)) ([b480f6c](https://github.com/tomturing/hci-troubleshoot-platform/commit/b480f6c9aaa513bcb56ddb91c82c6bdf1d3a2270))
* **kb-service:** 修复 classify prompt template 花括号转义 KeyError ([2affe8a](https://github.com/tomturing/hci-troubleshoot-platform/commit/2affe8a62538c1987f9c6072b12a7b4b7884da83))
* **kb-service:** 修复分类 YAML 导入与 category_baseline.yaml 格式不匹配 ([#140](https://github.com/tomturing/hci-troubleshoot-platform/issues/140)) ([37395c6](https://github.com/tomturing/hci-troubleshoot-platform/commit/37395c6f74158c399868d3fd3b59f24b8724b113))
* **kb-service:** 修复向量0个/python-docx缺失/幂等category更新三问题 ([551c83c](https://github.com/tomturing/hci-troubleshoot-platform/commit/551c83c857e1f761bf2e1facebdd66f7459183e4))
* **kb-service:** 添加 openai 依赖 + 修复 path_labels 重复解析 ([145a8ee](https://github.com/tomturing/hci-troubleshoot-platform/commit/145a8ee8fe11e065adbec4b1c00edda6f74f7887))
* **kb:** SQLAlchemy metadata保留字修复+BGE hostPath挂载，kbService启用 ([6a231dd](https://github.com/tomturing/hci-troubleshoot-platform/commit/6a231dd3459d8bb5d526a668f08fbab5f5dd8443))
* **kb:** 清理冗余 embedding 环境变量，移除 sop-skills HostPath 挂载，保留 BGE 本地模型路径 ([e88312d](https://github.com/tomturing/hci-troubleshoot-platform/commit/e88312da0a1a5c3bc6ac9c03342eaee4de550d10))
* **learningclaw:** DUAL-010~013 — API Key 注入链修复 + WebUI 接入 ([#21](https://github.com/tomturing/hci-troubleshoot-platform/issues/21)) ([02bc157](https://github.com/tomturing/hci-troubleshoot-platform/commit/02bc1577e1faae5bdf8f28d2f6145ecf53ae7b46))
* local K3s deploy fixes & one-click deploy improvements ([#17](https://github.com/tomturing/hci-troubleshoot-platform/issues/17)) ([b1695ea](https://github.com/tomturing/hci-troubleshoot-platform/commit/b1695ea656724e7a8c3b31e898bef4eca370b42b))
* **markdown:** 修复 marked breaks:true 在代码块内插入&lt;br&gt;破坏格式的问题 ([89b2767](https://github.com/tomturing/hci-troubleshoot-platform/commit/89b276748d25628ec8038325333bb7762a73271f))
* **obs:** 修正 PVC storageClass 为 local-path 匹配现有 PVC，添加 ignoreDifferen… ([#9](https://github.com/tomturing/hci-troubleshoot-platform/issues/9)) ([9b65aa9](https://github.com/tomturing/hci-troubleshoot-platform/commit/9b65aa991d71f6b3849ed8f9b4f62b3f6ef951ee))
* **obs:** 默认启用 Grafana Ingress 修复 /grafana 路由被吞问题 ([#53](https://github.com/tomturing/hci-troubleshoot-platform/issues/53))（ubuntu_sz_claude） ([f392189](https://github.com/tomturing/hci-troubleshoot-platform/commit/f392189f8adbede8afa88e8ef4b9daaf7a3a1e24))
* openclaw Deployment 绕过 Clash fake-ip DNS 劫持 ([2a26e61](https://github.com/tomturing/hci-troubleshoot-platform/commit/2a26e61acc5f7b410b6aa8e307d8eee332458451))
* **ops:** db-repair-env.sh — 对齐 Atlas 迁移体系/修正表数量阈值 ([#126](https://github.com/tomturing/hci-troubleshoot-platform/issues/126)) ([053f0f4](https://github.com/tomturing/hci-troubleshoot-platform/commit/053f0f4f9b0a53f9329a3ffc2ee54915f559e24a))
* **prometheus:** 新增 kube-state-metrics 专用 scrape job 避免 namespace 标签被覆盖 ([#56](https://github.com/tomturing/hci-troubleshoot-platform/issues/56)) ([cf3823e](https://github.com/tomturing/hci-troubleshoot-platform/commit/cf3823e64a59beb5b411840b340599bf56d50db0))
* **rag:** 打通k3s RAG全链路 - 修复向量维度及Token配置 ([5ac7c41](https://github.com/tomturing/hci-troubleshoot-platform/commit/5ac7c4129629afcfcae5956923a88b3844ead96a))
* **scheduler, helm:** 修复 ProductionClaw 启动失败问题 ([#36](https://github.com/tomturing/hci-troubleshoot-platform/issues/36)) ([9766a95](https://github.com/tomturing/hci-troubleshoot-platform/commit/9766a95b6068bf3e5c736159254ee50c7f395d47))
* **secret:** rename ZAI_API_KEY -&gt; OPENCLAW_API_KEY to match conversation-service config.py ([#19](https://github.com/tomturing/hci-troubleshoot-platform/issues/19)) ([6a0d235](https://github.com/tomturing/hci-troubleshoot-platform/commit/6a0d23532644f2acd56b541e3a0b8b4e7b608e3a))
* **security:** securityContext加固 + Pod重启告警规则 + LimitRange兜底 ([#7](https://github.com/tomturing/hci-troubleshoot-platform/issues/7)/[#8](https://github.com/tomturing/hci-troubleshoot-platform/issues/8)/[#9](https://github.com/tomturing/hci-troubleshoot-platform/issues/9)) ([89b9c08](https://github.com/tomturing/hci-troubleshoot-platform/commit/89b9c083f06d1fb97d726a00298891f8fb328d26))
* **seeds:** 修复 00_baseline.sql 的验证 SQL — 移除不存在的 ts 列，与 dbmate 原生表结构对齐 ([#84](https://github.com/tomturing/hci-troubleshoot-platform/issues/84)) ([7365c1e](https://github.com/tomturing/hci-troubleshoot-platform/commit/7365c1e79841efa1b44c8ff33be53c6b32200db4))
* **sop-ingest:** 同名文件内容更新时改为 upsert 而非静默跳过 ([f6fdba3](https://github.com/tomturing/hci-troubleshoot-platform/commit/f6fdba309762ad36f0d496d984ff45f46df9df54))
* **sop-manage:** 修复发布失败 [object Object] + 新增导入/查看/编辑功能 ([27571d5](https://github.com/tomturing/hci-troubleshoot-platform/commit/27571d559ca884989cbe9e314e1a98c600a53916))
* **sop+category:** 修复分类SOP覆盖联动、支持正文编辑与重新分块、修复数据混乱 ([0fd28f4](https://github.com/tomturing/hci-troubleshoot-platform/commit/0fd28f4b1c5b270183ccfae3e5e62fa35487d028))
* **sop:** 修复SOP技能目录挂载及关键字覆盖 ([6fbb4cd](https://github.com/tomturing/hci-troubleshoot-platform/commit/6fbb4cddcfb74302dc5087ff9c57a53463c0c520))
* **store:** 修复流式结束后isStreaming不触发Vue重渲染的根本原因 ([b4e4e10](https://github.com/tomturing/hci-troubleshoot-platform/commit/b4e4e1051b5c499b4be5e9d08998d0ccb5e925b7))
* **store:** 方案A-在流式结束前await nextTick消灭阶段2到3切换竞态 ([6204fb5](https://github.com/tomturing/hci-troubleshoot-platform/commit/6204fb5f14fd4ecc1acdd7ee49a772833802527a))
* **stream/ui:** 修复SSE流截断换行的问题，并在流式期间暂缓渲染交互式命令块 ([dcab8c8](https://github.com/tomturing/hci-troubleshoot-platform/commit/dcab8c8503675715c6a3917221bd3055efaa3d89))
* Task26/27 E2E 验证完成 - KB push 链路修复 + Scheduler RBAC + openclaw 注册 ([9e52efa](https://github.com/tomturing/hci-troubleshoot-platform/commit/9e52efab59a6cc01ed4b2c350369a07c59bfaae3))
* **task30:** 修复 conversation-service 评分再计算的两处缺陷 ([f3d20d6](https://github.com/tomturing/hci-troubleshoot-platform/commit/f3d20d6eb1e2f7afe517e9bb4fa98259052d02ef))
* **task30:** 修复评分链路与重复提问统计关键缺陷 ([d820e89](https://github.com/tomturing/hci-troubleshoot-platform/commit/d820e896abdf81ad8cf3fc277160542304b0feed))
* **terminal-bridge:** rewrite bat in pure ASCII to fix GBK/UTF-8 encoding issue ([9b78f97](https://github.com/tomturing/hci-troubleshoot-platform/commit/9b78f970c4a49a5336adb4412b16ff37757c864b))
* **terminal:** 修复下载按钮无效问题 ([cb03527](https://github.com/tomturing/hci-troubleshoot-platform/commit/cb03527f18da08142a83684759a583202cd0c475))
* **ui:** 修复流式输出期间Markdown代码块无法实时截断解析渲染为CommandBlock的Bug ([0810d4a](https://github.com/tomturing/hci-troubleshoot-platform/commit/0810d4af73e02702d2a36a05f3d6f3ca8c13cd8c))
* **ui:** 修复阶段3升级渲染不生效的根本原因 ([564a8e0](https://github.com/tomturing/hci-troubleshoot-platform/commit/564a8e0eb128e9fc4520ccf74a058c22cfaa13df))
* **ui:** 分离SSH终端与历史工单的抽屉宽度状态，修复拖动相互影响问题 ([4274aa5](https://github.com/tomturing/hci-troubleshoot-platform/commit/4274aa5e1c3cdec882be1f7c12d2e1dd4a157411))
* **ui:** 方案B-统一流式与完成态渲染通道，彻底消灭切换竞态 ([7c032d1](https://github.com/tomturing/hci-troubleshoot-platform/commit/7c032d1cf720dd434890d7ae92ffe0a7f54d756a))
* **ui:** 方案B优化-重构段落拆分逻辑，放弃正则替换为 marked AST 彻底解决 Markdown 解析问题 ([3cddf79](https://github.com/tomturing/hci-troubleshoot-platform/commit/3cddf7966479c2cef2884e345a381c87d5d59448))
* **ui:** 解决el-drawer钉住后点击穿透被阻挡问题并支持边缘拖拽改变宽度 ([e09f1fa](https://github.com/tomturing/hci-troubleshoot-platform/commit/e09f1faf68ceca4f254ad86f60d7311946e357b1))
* 修复 categories.set_dependencies 参数签名不匹配 ([#95](https://github.com/tomturing/hci-troubleshoot-platform/issues/95)) ([54484e7](https://github.com/tomturing/hci-troubleshoot-platform/commit/54484e7346ebe13064573985edb4c6194bfe8d4f))
* 修复 DB schema 漂移 — 补齐缺失表列 + 全套事件文档 ([#108](https://github.com/tomturing/hci-troubleshoot-platform/issues/108)) ([80b56fc](https://github.com/tomturing/hci-troubleshoot-platform/commit/80b56fcf4238748671764dc957f7727f8d365990))
* 修复 kb-service 导入功能 ([#135](https://github.com/tomturing/hci-troubleshoot-platform/issues/135)) ([0667cdc](https://github.com/tomturing/hci-troubleshoot-platform/commit/0667cdcc29eaaf6615c3ea07d41887e83725a9ad))
* 修复 KbCategory 模型重复定义导致 Table already defined 错误 ([#94](https://github.com/tomturing/hci-troubleshoot-platform/issues/94)) ([3b8d90a](https://github.com/tomturing/hci-troubleshoot-platform/commit/3b8d90a15d1dc4e0e9e528de2f21aa620becfcf3))
* 修复 KBD pipeline 三项 bug ([36ac11b](https://github.com/tomturing/hci-troubleshoot-platform/commit/36ac11bb79e1e79fed244d8100758807288ca860))
* 修复 openclaw 网关 400 及 KBD 发布 500 问题 ([#145](https://github.com/tomturing/hci-troubleshoot-platform/issues/145)) ([ff60692](https://github.com/tomturing/hci-troubleshoot-platform/commit/ff60692f9d164afc854207a493794b166197d3f3))
* 修复 Prometheus 告警规则 namespace 过滤条件 ([#70](https://github.com/tomturing/hci-troubleshoot-platform/issues/70)) ([beba21a](https://github.com/tomturing/hci-troubleshoot-platform/commit/beba21a29c7afe4118b5b7278ac931268f9f1098))
* 修复 seed_categories.py JSONB 字符串解析问题 ([#93](https://github.com/tomturing/hci-troubleshoot-platform/issues/93)) ([3d373c4](https://github.com/tomturing/hci-troubleshoot-platform/commit/3d373c4272a28c21b5529e96043f50ab773943ac))
* 修复 SQLAlchemy 保留属性 metadata 冲突导致 kb-service 启动失败 ([#92](https://github.com/tomturing/hci-troubleshoot-platform/issues/92)) ([6807c1f](https://github.com/tomturing/hci-troubleshoot-platform/commit/6807c1fb15e6195fee8e32ebe79d19bc90a3693a))
* 修复K3s发布脚本静默退出与sudo交互卡住 ([1baac2b](https://github.com/tomturing/hci-troubleshoot-platform/commit/1baac2b51c46680d33f0adb160906fb41ebe354e))
* 修复截图详情字段3/4内联内容丢失导致只显示可见内容一栏的问题 ([a3223d0](https://github.com/tomturing/hci-troubleshoot-platform/commit/a3223d00a0e0b84d2a3c6a83b2166cc1789cd1f3))
* 修复截图说明 v2 格式解析——支持 BACKGROUND/TYPE/FULL_TEXT/KEY/TIPS 五字段，修复全显示为其他截图和内容溢出问题 ([acec87f](https://github.com/tomturing/hci-troubleshoot-platform/commit/acec87f637e443e118a9496ef48d1e677bc94c81))
* 修复日志截图识别——改进OCR Prompt照录英文日志+黑色背景兜底分类，更新方案文档 ([d708124](https://github.com/tomturing/hci-troubleshoot-platform/commit/d7081246de046e118988099312efc2b3d2e15263))
* 修复状态机设计问题 ([#102](https://github.com/tomturing/hci-troubleshoot-platform/issues/102)) ([37d44f5](https://github.com/tomturing/hci-troubleshoot-platform/commit/37d44f5384832c9643e7c06a4312f1a5616c0865))
* 修复空 SSE 响应检测逻辑 ([06bb1d1](https://github.com/tomturing/hci-troubleshoot-platform/commit/06bb1d13df27b5f1a4c141efe239bea3f19c345d))
* 在 prometheus Service 定义前添加缺失的 YAML 文档分隔符 ([#73](https://github.com/tomturing/hci-troubleshoot-platform/issues/73)) ([445ece1](https://github.com/tomturing/hci-troubleshoot-platform/commit/445ece1f93c1fc9ee21f48c9ce0e6dfb51bcf049))
* 增加 db-password-check Job 超时时间到 180s ([#72](https://github.com/tomturing/hci-troubleshoot-platform/issues/72)) ([a5b08e6](https://github.com/tomturing/hci-troubleshoot-platform/commit/a5b08e6e3768e619d2960f4f45ee501e17316cd6))
* 增加Helm发布锁保护并固定kubeconfig ([330c670](https://github.com/tomturing/hci-troubleshoot-platform/commit/330c67056dcd4a496869d700efde8304fa72025b))
* 恢复 admin-ui/customer-ui nginx emptyDir volumes ([#68](https://github.com/tomturing/hci-troubleshoot-platform/issues/68)) ([528aa21](https://github.com/tomturing/hci-troubleshoot-platform/commit/528aa2156df90daf134a97af5efde6389af6115f))
* 截图详情始终显示字段2/3；修复Vision截断导致字段缺失（max_tokens+补全逻辑） ([23ce5d1](https://github.com/tomturing/hci-troubleshoot-platform/commit/23ce5d193326e47e87ad12f122083010eefdfa59))
* 添加 api-gateway python-multipart 依赖 ([#137](https://github.com/tomturing/hci-troubleshoot-platform/issues/137)) ([01fa142](https://github.com/tomturing/hci-troubleshoot-platform/commit/01fa1422c37d89fe9a417291beda9c9d81a3d519))
* 添加 kb-service python-multipart 依赖 ([#136](https://github.com/tomturing/hci-troubleshoot-platform/issues/136)) ([523db3a](https://github.com/tomturing/hci-troubleshoot-platform/commit/523db3a6d63053f26fb522944912a455ea0e4f62))
* 清理 PR [#108](https://github.com/tomturing/hci-troubleshoot-platform/issues/108) 误重建的废弃表，修正 schema_repair.sql ([#113](https://github.com/tomturing/hci-troubleshoot-platform/issues/113)) ([35b8ee7](https://github.com/tomturing/hci-troubleshoot-platform/commit/35b8ee780e0ec92210d353c5d664ee61fcb34cca))
* 移除 CoreDNS hosts 插件配置，改由 Clash fake-ip-filter 解决 GitHub 解析问题 ([#71](https://github.com/tomturing/hci-troubleshoot-platform/issues/71)) ([097a3a1](https://github.com/tomturing/hci-troubleshoot-platform/commit/097a3a1615d63cfd1890baa732da097fe1a24572))


### ♻️ 代码重构

* api-gateway 透明代理重构 ([#138](https://github.com/tomturing/hci-troubleshoot-platform/issues/138)) ([f52b84a](https://github.com/tomturing/hci-troubleshoot-platform/commit/f52b84abd001f653854cfffe43111336033e5150))
* **docs:** 重组 solution/events 目录结构 ([#107](https://github.com/tomturing/hci-troubleshoot-platform/issues/107)) ([b694108](https://github.com/tomturing/hci-troubleshoot-platform/commit/b694108cd01eefc819d0c8cb1e1af599e7dd393f))
* **gitops:** 按 ArgoCD 实例重构 argo-apps 目录为 local/ 和 cloud/ ([#25](https://github.com/tomturing/hci-troubleshoot-platform/issues/25)) ([0be0185](https://github.com/tomturing/hci-troubleshoot-platform/commit/0be0185cc9e496717948470ceec88dfcc571bc22))
* **kbd:** 数据管道脚本改造为调用 kb-service API ([#90](https://github.com/tomturing/hci-troubleshoot-platform/issues/90)) ([9cfd107](https://github.com/tomturing/hci-troubleshoot-platform/commit/9cfd1076da01f858fc8568d5ce452b0f07218134))
* **observability:** alertmanager-config Secret 迁入双仓模式（Helm + env repo） ([2ae3442](https://github.com/tomturing/hci-troubleshoot-platform/commit/2ae3442a641a88e6253e8014fe37a2bbffb8c876))
* **terminal-bridge:** 替换为 Go 实现，移除 Python/PyInstaller 方案 ([9224e51](https://github.com/tomturing/hci-troubleshoot-platform/commit/9224e51b061af4f10c70d130180018808a78d627))
* **terminal:** 改为 localhost Bridge 模式，移除服务端 SSH 代理 ([86b3d39](https://github.com/tomturing/hci-troubleshoot-platform/commit/86b3d39894812b67449a036987991e79339b13ab))
* **ui:** 恢复历史工单与SSH终端为抽屉(Drawer)，并且宽度动态适配为两翼 ([08a132f](https://github.com/tomturing/hci-troubleshoot-platform/commit/08a132fb8ca47f8f2c94aa8b1d650dad9d5c47d6))
* 知识库架构重构 —— 删除 data-pipeline、清理 T17 死代码、文档目录重构 ([#100](https://github.com/tomturing/hci-troubleshoot-platform/issues/100)) ([5d807f4](https://github.com/tomturing/hci-troubleshoot-platform/commit/5d807f48c1d33a8783838ef166f74c630ba2be01))


### 📝 文档

* add DUAL-012 entry for pairing required on external browser access ([02bc157](https://github.com/tomturing/hci-troubleshoot-platform/commit/02bc1577e1faae5bdf8f28d2f6145ecf53ae7b46))
* add DUAL-012 entry for pairing required on external browser access ([523b1fe](https://github.com/tomturing/hci-troubleshoot-platform/commit/523b1fe8f818e74dd6dd1d083d39e2b06a20b4ca))
* **AGENTS.md:** 补充 Copilot 提交 PR 的两条强制规则 ([#97](https://github.com/tomturing/hci-troubleshoot-platform/issues/97)) ([0747298](https://github.com/tomturing/hci-troubleshoot-platform/commit/07472981d8e0cceb434df5d0a30b12a80dead02d))
* **copilot-instructions:** 补充 Copilot PR 提交两条强制规则 ([#98](https://github.com/tomturing/hci-troubleshoot-platform/issues/98)) ([c28f86f](https://github.com/tomturing/hci-troubleshoot-platform/commit/c28f86f49c2b5e397f39af4f94a7ac28ca302fee))
* **kb:** 完善知识库设计.md §7 知识导入方法（KBD + SOP） ([a0dea32](https://github.com/tomturing/hci-troubleshoot-platform/commit/a0dea3285a96587e7b764da363c9ae2660016bd2))
* **kb:** 整合 SOP 导入文档到知识库设计.md §7.2 ([55482bd](https://github.com/tomturing/hci-troubleshoot-platform/commit/55482bdd044f52fd64d029e041da0f6f1cd2fcd8))
* **kb:** 补充 classify LLM 集成原始设计与优化说明（v4.5） ([607de73](https://github.com/tomturing/hci-troubleshoot-platform/commit/607de73f7b719247c9dce985b678b9c9033a86ae))
* **knowledge-base:** 重构知识库设计文档，归档老方案 ([#101](https://github.com/tomturing/hci-troubleshoot-platform/issues/101)) ([59ae7be](https://github.com/tomturing/hci-troubleshoot-platform/commit/59ae7be8c7ba1035b0554aa4094ffa7e3f84205b))
* 修复 README.md 中的 ArgoCD App 路径和描述 ([#41](https://github.com/tomturing/hci-troubleshoot-platform/issues/41))（ubuntu_sz_copilot） ([3a62b30](https://github.com/tomturing/hci-troubleshoot-platform/commit/3a62b30a34ba77fea214e146d6e2f256582e8485))
* 同步 Git 规则到 AGENTS.md 和 copilot-instructions.md ([#82](https://github.com/tomturing/hci-troubleshoot-platform/issues/82)) ([63ef07f](https://github.com/tomturing/hci-troubleshoot-platform/commit/63ef07f3d2ec37b58c29687fd39ff2d70c86d399))
* 同步 Task 30 完成状态到项目进展文档 ([5225849](https://github.com/tomturing/hci-troubleshoot-platform/commit/5225849cd7dd5ecbe69be5a814530658335f8640))
* 数据库架构重构 - 基于知识库方案B精简至11张表 ([#86](https://github.com/tomturing/hci-troubleshoot-platform/issues/86)) ([54bf2e6](https://github.com/tomturing/hci-troubleshoot-platform/commit/54bf2e655af718d52313385c02a9f518d7c6b9d7))
* 新增 AI层×RAG层对接架构决策文档(doc 15)，更新 01/12/13 交叉引用 ([a5f6919](https://github.com/tomturing/hci-troubleshoot-platform/commit/a5f6919bb36560c99f5aa476d2b1b3f5e40b9a6d))
* 新增 Task 28/31 Agent 任务编排文档（19_任务编排.md） ([4ef143a](https://github.com/tomturing/hci-troubleshoot-platform/commit/4ef143a1c46c1d1939268b0956261a86c510f6fb))
* 新增§8迭代记录（Task25 ETL + Task28 Prometheus） ([215cf28](https://github.com/tomturing/hci-troubleshoot-platform/commit/215cf28580566d4124270c2d88b2cf8891cc9a8d))
* 新增20_任务编排补齐prompt_audit实现缺口 ([02a2194](https://github.com/tomturing/hci-troubleshoot-platform/commit/02a219441d507decea3030184262ebfe1f35415a))
* 新增任务编排设计文档(18_任务编排.md) ([7624b3e](https://github.com/tomturing/hci-troubleshoot-platform/commit/7624b3e40b127a0c65d83bf354dc406406550196))
* 新增评分评价体系与客户端设计文档，完善任务计划与验收标准 ([0790a5c](https://github.com/tomturing/hci-troubleshoot-platform/commit/0790a5c0e58436d0c83192b73208c958a13180c6))
* 更新 09_项目进展.md Task 26/27 状态为已完成，新增 §2.18 验证记录 ([9e52efa](https://github.com/tomturing/hci-troubleshoot-platform/commit/9e52efab59a6cc01ed4b2c350369a07c59bfaae3))
* 更新交付标准化看板，补充 GAP-06/ADR-005 完成状态 ([dd1c1c1](https://github.com/tomturing/hci-troubleshoot-platform/commit/dd1c1c13266f2866b17d868a6d443058610b26e6))
* 更新详细执行计划（P0-P3，Task25-29）- 2026-03-10 ([67ca425](https://github.com/tomturing/hci-troubleshoot-platform/commit/67ca4258df29260b9cff01e372ffbd2224d6aa9e))
* 更新项目进展 - Task 29 完成记录（§8.4 新增） ([0aa3e75](https://github.com/tomturing/hci-troubleshoot-platform/commit/0aa3e7593569d59208011e55a97e53f9876db364))
* 更新项目进展文档至 2026-03-10 ([bfbb6ad](https://github.com/tomturing/hci-troubleshoot-platform/commit/bfbb6ad57766304aa58be2ce5a72caa5031df5d1))
* 添加团队避坑指南 skill ([#35](https://github.com/tomturing/hci-troubleshoot-platform/issues/35)) ([d142030](https://github.com/tomturing/hci-troubleshoot-platform/commit/d1420308dbcc097cffc91cf24ac2af31fabadbdb))
* 统一 ArgoCD NodePort 端口为 30808（本地+云端） ([#74](https://github.com/tomturing/hci-troubleshoot-platform/issues/74)) ([8d9f43a](https://github.com/tomturing/hci-troubleshoot-platform/commit/8d9f43ad7b04504920082b0e0d4a9c3b19400755))
* 补充三阶段横向对比矩阵及阶段三详细评估到 doc 15 ([4972fba](https://github.com/tomturing/hci-troubleshoot-platform/commit/4972fba8cdb6abc55c330768e98a8023dfe78a6b))
* 补充任务编排文档与模板 ([b1806ee](https://github.com/tomturing/hci-troubleshoot-platform/commit/b1806eea54b71b2d74c4eb597048ff42b025dffa))
* 补充发布指南容器/数据库发布逻辑和 PR 检查项 ([#125](https://github.com/tomturing/hci-troubleshoot-platform/issues/125)) ([f2e12fb](https://github.com/tomturing/hci-troubleshoot-platform/commit/f2e12fb3013f36af849e837be435dde2c64f904f))
* 阶段一外挂RAG验收完成（2026-03-10）- 4-Tier Prompt+SOP命中+Loki日志全部通过 ([38e8381](https://github.com/tomturing/hci-troubleshoot-platform/commit/38e838182be8404349b681a1efd275aec01de297))

## [2.2.0](https://github.com/tomturing/hci-troubleshoot-platform/compare/v2.1.0...v2.2.0) (2026-04-07)


### ✨ 新功能

* add multi-assistant CLI adapters and native nanoclaw runtime ([86c1202](https://github.com/tomturing/hci-troubleshoot-platform/commit/86c1202ced321b2fde85038caf07c201a8ae5be5))
* add terminal_bridge.exe to downloads for customer UI ([4b78d82](https://github.com/tomturing/hci-troubleshoot-platform/commit/4b78d829f9b3bff4fd3a241489dda7d8a771eab5))
* add terminal_bridge.exe to downloads for customer UI ([6f0844a](https://github.com/tomturing/hci-troubleshoot-platform/commit/6f0844a0c6757391d56df95aa1e7f8ec4f7d2918))
* **admin:** 调整侧边栏菜单顺序，按业务优先级排列 ([#103](https://github.com/tomturing/hci-troubleshoot-platform/issues/103)) ([d999647](https://github.com/tomturing/hci-troubleshoot-platform/commit/d999647abc593dee49c35ed639c58c9ede0a9ecc))
* **ai-layer:** LearningClaw+ProductionClaw 一 Pod 一 Case 架构实现 ([60d7aec](https://github.com/tomturing/hci-troubleshoot-platform/commit/60d7aec827f7ffdb1788d2a2f12ffbdf233558a2))
* **api-gateway:** Task 37 SSH 代理与终端交互后端能力 [model: opus] ([96b4212](https://github.com/tomturing/hci-troubleshoot-platform/commit/96b42128a64289f479c97ed2dc9c112460d2ba68))
* ArgoCD 升级脚本增强 + Git commit 标识规范 ([#76](https://github.com/tomturing/hci-troubleshoot-platform/issues/76)) ([4db398a](https://github.com/tomturing/hci-troubleshoot-platform/commit/4db398a9d5e510e34ce29d1a72aae712287deea8))
* **case-service:** 实现 QualityScoreService (Task 30-C) [model: opus] ([2e1d412](https://github.com/tomturing/hci-troubleshoot-platform/commit/2e1d4121639fee7d88399493668b489422fc8fbd))
* **case-service:** 实现工单关闭逻辑 (Task 30-B) [model: sonnet] ([d8abffe](https://github.com/tomturing/hci-troubleshoot-platform/commit/d8abffe65df120195ee8173b5854a31561f91f76))
* **ci:** dev/staging 环境自动晋级，prod 需手动审批 ([#69](https://github.com/tomturing/hci-troubleshoot-platform/issues/69)) ([c55820f](https://github.com/tomturing/hci-troubleshoot-platform/commit/c55820f5c28175bbe0c2e63c5200e81a17b7efe6))
* **conversation:** 实现用户评分API (Task 30-E) ([17d4928](https://github.com/tomturing/hci-troubleshoot-platform/commit/17d492841beb8e3c58c4f016f16c1f98ec9c571c))
* **conversation:** 实现重复提问检测功能 ([3325e8b](https://github.com/tomturing/hci-troubleshoot-platform/commit/3325e8b113cf726ae186d1e1fc84fac3a4fc5cdb))
* **customer:** Markdown 渲染改造，支持标题、列表、引用、代码块 ([c0c5f1c](https://github.com/tomturing/hci-troubleshoot-platform/commit/c0c5f1cf34b5c937e03e57546f15a54301e6155e))
* **customer:** Task 35 命令卡片化与一键发送到终端 ([662e351](https://github.com/tomturing/hci-troubleshoot-platform/commit/662e3514a5a6d20177966e306fc19f6b307b7281))
* **customer:** Task 36 侧边栏 SSH 登录与交互终端页面 ([dfe65cd](https://github.com/tomturing/hci-troubleshoot-platform/commit/dfe65cdea27d3db9681236577db1df622e3e8e6e))
* **database:** 数据库表清理迁移脚本 ([#91](https://github.com/tomturing/hci-troubleshoot-platform/issues/91)) ([0eb1535](https://github.com/tomturing/hci-troubleshoot-platform/commit/0eb1535da03a439cb49954f5defb91a8de5f1713))
* **data:** postgres 服务支持 NodePort 配置 ([#43](https://github.com/tomturing/hci-troubleshoot-platform/issues/43)) ([a4d5104](https://github.com/tomturing/hci-troubleshoot-platform/commit/a4d51044767207841764fe5ab74bc4ddfed76c6f))
* **db:** 新增评分评价体系迁移脚本 (migrate_evaluation_v1) ([4ae2659](https://github.com/tomturing/hci-troubleshoot-platform/commit/4ae2659f96d9d9be814034f18feedcc18fd1308b))
* **etl:** Task 25 P0 产品案例ETL数据管道 - fetcher/converter/enricher/reviewer_cli/ingestor/pipeline 六脚本 + ETL依赖 ([f12d8ff](https://github.com/tomturing/hci-troubleshoot-platform/commit/f12d8ff95f8de083268af285f14c1c656b611fd2))
* frontend dual apps ([2620f13](https://github.com/tomturing/hci-troubleshoot-platform/commit/2620f13b7d43a0f1d27241f757aa7c6531ae09db))
* **frontend:** 实现工单关闭后的评分卡组件 ([6b67221](https://github.com/tomturing/hci-troubleshoot-platform/commit/6b67221e15bbd8d0928c05ae438b1f01f5f924ce))
* Git 标识规则完善（增加 hostname + gpr 函数） ([#79](https://github.com/tomturing/hci-troubleshoot-platform/issues/79)) ([9d0c7aa](https://github.com/tomturing/hci-troubleshoot-platform/commit/9d0c7aa9d1887513fa4c0c1775918a2331c47461))
* **gitops:** 完成 hub-spoke prod 集群应用与发布手册修复 ([#61](https://github.com/tomturing/hci-troubleshoot-platform/issues/61))（aihci_copilot） ([8f6bb81](https://github.com/tomturing/hci-troubleshoot-platform/commit/8f6bb81cc63d8a9506d11d27cc105ee246a376df))
* **helm:** 支持 ghcr.io imagePullSecret 认证拉取镜像 ([#10](https://github.com/tomturing/hci-troubleshoot-platform/issues/10)) ([5513657](https://github.com/tomturing/hci-troubleshoot-platform/commit/5513657fa726091144cf22e04c464cde950ff61d))
* **helm:** 支持 ghcr.io imagePullSecret 认证拉取镜像 ([#31](https://github.com/tomturing/hci-troubleshoot-platform/issues/31)) ([c0cadf0](https://github.com/tomturing/hci-troubleshoot-platform/commit/c0cadf083229095b1a54e4cb6cc7273ee25c31b1))
* Ingress 添加 /openclaw 路由暴露 K3s OpenClaw WebUI ([c49a579](https://github.com/tomturing/hci-troubleshoot-platform/commit/c49a579795fefee6c56f187fac6eaae90f69e31e))
* K3s集群健壮性改进计划 Sprint1/2/3 全量实现 ([#62](https://github.com/tomturing/hci-troubleshoot-platform/issues/62))（ubuntu_sz_copilot） ([2172354](https://github.com/tomturing/hci-troubleshoot-platform/commit/2172354530ecb4fb7a75fb0f0536ce4e19d52c77))
* **k8s:** K3s 生产级 Helm 部署架构落地及问题修复 ([cd0ea1f](https://github.com/tomturing/hci-troubleshoot-platform/commit/cd0ea1fb15d4d176f6b3334136a6fb71eff8cdc0))
* K8s设计文档 + Helm Chart骨架 + 配置统一整合 ([61842a3](https://github.com/tomturing/hci-troubleshoot-platform/commit/61842a390a26b5000b159190a62f1aa289a9233f))
* **kb-service:** 实现知识库模块核心功能 ([#87](https://github.com/tomturing/hci-troubleshoot-platform/issues/87)) ([eb5265f](https://github.com/tomturing/hci-troubleshoot-platform/commit/eb5265f29386667254437b8967e6db3970d924f6))
* **kb-service:** 扩展知识库 API + 代码质量修复 ([#89](https://github.com/tomturing/hci-troubleshoot-platform/issues/89)) ([c9185e0](https://github.com/tomturing/hci-troubleshoot-platform/commit/c9185e020de5a2140559e6d609ea652f3369f294))
* **kbd:** KBD知识生产管道v1 + 审核UI + 59个单元测试 ([#83](https://github.com/tomturing/hci-troubleshoot-platform/issues/83)) ([33dcca3](https://github.com/tomturing/hci-troubleshoot-platform/commit/33dcca341fa2d9307f5b963412f761d2ea7c2844))
* **kb:** 完成 SOP 批量摄入 + 修复大文件 Embedding 超时问题 ([0231897](https://github.com/tomturing/hci-troubleshoot-platform/commit/02318976f25ccb336b90e0cb8393cbaa756871a2))
* **observability:** Task 28 Prometheus进K3s - Helm模板+RBAC+Grafana数据源+5服务/metrics端点 ([969b02c](https://github.com/tomturing/hci-troubleshoot-platform/commit/969b02c002e15f4220725a148f1ac8b404fab08b))
* **observability:** 增加Pod异常监测与飞书告警能力 ([d61f6c5](https://github.com/tomturing/hci-troubleshoot-platform/commit/d61f6c58a6de09ab7dd9725f138979716e6f061f))
* **obs:** hci-platform-obs 引入 env 仓库 values，Grafana 密码由环境仓库统一管理 ([#8](https://github.com/tomturing/hci-troubleshoot-platform/issues/8)) ([aec7b98](https://github.com/tomturing/hci-troubleshoot-platform/commit/aec7b9821c43532ed37e1abc9ddf9ec6b1cd8dbd))
* **openclaw:** 挂载宿主机 /srv/openclaw/shared 到容器 /shared（只读） ([2273545](https://github.com/tomturing/hci-troubleshoot-platform/commit/2273545fe8d5d479e15ad65b028cc2bc5eee3a90))
* **postgres:** 支持 NodePort 配置以便外部访问数据库 ([#42](https://github.com/tomturing/hci-troubleshoot-platform/issues/42)) ([e16e664](https://github.com/tomturing/hci-troubleshoot-platform/commit/e16e6644a467af4a6acf935bc72d69a2f75bcecd))
* **S0/S6:** S0候选确认模式v2 + S6三选项闭环 + 数据库v6.3全量落地 ([a356183](https://github.com/tomturing/hci-troubleshoot-platform/commit/a356183aa2c9efbe73768c79ac37c140e1a04db9))
* **s0:** S0 意图识别与分类基线重构 ([#88](https://github.com/tomturing/hci-troubleshoot-platform/issues/88)) ([a9fbd31](https://github.com/tomturing/hci-troubleshoot-platform/commit/a9fbd3102df66f13314e25a377775eccda392d90))
* **sop:** 新增虚拟机开关机失败排障SOP知识库 ([2905a3c](https://github.com/tomturing/hci-troubleshoot-platform/commit/2905a3c19b6aa1332b6427e6c28b9377a3f01262))
* **storage:** postgres PV Retain策略 + pg_dump备份 + openclaw挂载精简 ([6182368](https://github.com/tomturing/hci-troubleshoot-platform/commit/61823688d71258088bccabce421681d9621424f0))
* **task30+etl:** Task 30 A-F 全量验证通过 + ETL P0 KB数据建设 ([27a35ff](https://github.com/tomturing/hci-troubleshoot-platform/commit/27a35ff5f2a13d4a82109be042b06f2a46a31271))
* **terminal-bridge:** nginx 支持 exe 下载 + release 脚本检查 + public 目录占位 ([8895a3c](https://github.com/tomturing/hci-troubleshoot-platform/commit/8895a3c9b02a2cc0eb5958dcd167739c12e02ddb))
* **terminal-bridge:** 完整源码+打包脚本+release脚本优化 ([e453069](https://github.com/tomturing/hci-troubleshoot-platform/commit/e45306941e23c7c6dcddfab03388afd015362e46))
* **terminal-bridge:** 添加静态文件托管支持 terminal_bridge.exe 下载 ([fdac9d1](https://github.com/tomturing/hci-troubleshoot-platform/commit/fdac9d1207d128836a94509198190765468f53fe))
* **test:** Task 29 Phase 4 测试覆盖 + CI/CD ([b71bafa](https://github.com/tomturing/hci-troubleshoot-platform/commit/b71bafa2815ac17b4ca09107d38d207b3fcd6b49))
* **ui:** 实现AI回复三阶段渲染状态机（thinking→流式→CommandBlock升级） ([e7842cb](https://github.com/tomturing/hci-troubleshoot-platform/commit/e7842cbcf23d6c023b7281c34bfef5dc7b106b48))
* 三层自动化防御 - PostSync冒烟测试 + CI探针路径对齐检查 ([#67](https://github.com/tomturing/hci-troubleshoot-platform/issues/67))（ubuntu_sz_copilot） ([fe67a22](https://github.com/tomturing/hci-troubleshoot-platform/commit/fe67a22bb0ebaf5277c73dda65c1525138de2f9e))
* 优化SSH终端与历史工单交互及文案样式 ([c00cf2f](https://github.com/tomturing/hci-troubleshoot-platform/commit/c00cf2f05871d74c9af4e2a1dc5c0b181bef4ac4))
* 全自动工作流 — cleanup 通过后自动创建审查 Session ([e12fdc6](https://github.com/tomturing/hci-troubleshoot-platform/commit/e12fdc62dd91c49190befb87bea7d284eac48768))
* 升级 ArgoCD 到 v3.3.6 并新增 repo-server copyutil watchdog ([882ac86](https://github.com/tomturing/hci-troubleshoot-platform/commit/882ac866bb915cc3ce47cdf085ca71e65b6b7b40))
* 合并 ST-A1 (SSE 错误回传) + ST-B1 (KB pgvector 表定义) 功能代码 ([9dc8024](https://github.com/tomturing/hci-troubleshoot-platform/commit/9dc80248cc69354e777d3043a3a542e336197386))
* 增加K3s一键发布与选择性镜像构建 ([c20d524](https://github.com/tomturing/hci-troubleshoot-platform/commit/c20d524df1229f6dea9c03fa82c8606d35ee68c4))
* 完善生产部署与多节点稳态能力，修复联调脚本可移植性与JSON组包问题 ([2bdb1f6](https://github.com/tomturing/hci-troubleshoot-platform/commit/2bdb1f6f278ea44c375615ece75e8c1e40f5ef1c))
* 完成全链路可观测性基建集成、微服务全流联调与文档系统中文化升级 ([4c6ba1b](https://github.com/tomturing/hci-troubleshoot-platform/commit/4c6ba1bd8158e7745a28a9e11c39fa79f6e2c202))
* 支持 global.publicUrl 配置路径路由模式下 Grafana Root URL（域名绑定 acli.sangfor.com.cn） ([3d6ae2a](https://github.com/tomturing/hci-troubleshoot-platform/commit/3d6ae2a9644384f7a6a8fa1c287769cf323d265f))
* 新增Admin API端点 - GET /api/cases/all (分页+筛选所有工单), /stats (统计), /clients (客户端列表) ([362eb45](https://github.com/tomturing/hci-troubleshoot-platform/commit/362eb45c5c87056b948fd78e451a711ef462b16f))
* 架构v2.0代码落地 — 多类型AI助手支持 ([05d1b99](https://github.com/tomturing/hci-troubleshoot-platform/commit/05d1b9990735b712f53ce9ab817e3ef96e0f12c1))
* 添加 ArgoCD Server NodePort 覆盖配置（端口 30808） ([#81](https://github.com/tomturing/hci-troubleshoot-platform/issues/81)) ([c14ec5f](https://github.com/tomturing/hci-troubleshoot-platform/commit/c14ec5f5e08dfadf0a7d0bf21388c9dbb8f2dc51))
* 生产环境加固 —— NetworkPolicy/HPA/openclaw ConfigMap/域名绑定/Tempo 2Gi ([363a676](https://github.com/tomturing/hci-troubleshoot-platform/commit/363a676229aa0459508eacc9e052619601b97f22))
* 质量门禁通过后自动流转 VK Issue 状态 (vk-hooks.sh + REST API) ([4b41b7a](https://github.com/tomturing/hci-troubleshoot-platform/commit/4b41b7a517490a0636a4296b8903ea5133b0a738))
* 避坑指南体系统一化升级 ([#60](https://github.com/tomturing/hci-troubleshoot-platform/issues/60))（aihci_copilot） ([5779f8f](https://github.com/tomturing/hci-troubleshoot-platform/commit/5779f8f43cfdcb0b3e518c949aecf2d2f684290e))
* 项目交付标准化整改（Sprint 1/2/3） ([#3](https://github.com/tomturing/hci-troubleshoot-platform/issues/3)) ([dd1c1c1](https://github.com/tomturing/hci-troubleshoot-platform/commit/dd1c1c13266f2866b17d868a6d443058610b26e6))


### 🐛 Bug 修复

* /admin 重定向改为 302，避免浏览器缓存旧 Location ([bfddaa8](https://github.com/tomturing/hci-troubleshoot-platform/commit/bfddaa840898df1e47c8a0fd31a0f0218adb9b39))
* /openclaw 无斜杠自动 301 跳转 /openclaw/（PIT-030） ([1d5a7fb](https://github.com/tomturing/hci-troubleshoot-platform/commit/1d5a7fb5e5a4e20e197326f0feb690e5b144f2c3))
* admin-ui nginx 加 absolute_redirect off，修复无斜杠访问 /admin 301 跳转到内部地址 ([6ac511a](https://github.com/tomturing/hci-troubleshoot-platform/commit/6ac511a2b553cacb98f5b6b3374a5cebef54ab4c))
* **admin:** 修复侧边栏菜单顺序，按 order 字段排序 ([#110](https://github.com/tomturing/hci-troubleshoot-platform/issues/110)) ([49429f0](https://github.com/tomturing/hci-troubleshoot-platform/commit/49429f0ba0025c14d7f80d4bfa92d95970b8ec05))
* AGENTS.md 改为真实文件，CLAUDE.md 改为 symlink，新增工作前必读章节 ([4aaaf76](https://github.com/tomturing/hci-troubleshoot-platform/commit/4aaaf76efc1f73daae92530fe363d16e21f50864))
* **alertmanager:** 修复 Clash fake-IP 导致飞书 webhook 无法连通（PIT-034） ([6237665](https://github.com/tomturing/hci-troubleshoot-platform/commit/6237665bb64bb2c9c5e6932968601810e1d5d9c3))
* **case-service:** prometheus_client 条件导入修复基础镜像兼容性\n\n- quality_score.py: 将 prometheus_client 改为条件导入，未安装时跳过指标上报\n- main.py: 将 prometheus_client 改为条件导入，/metrics 端点在无库时返回空内容\n- 解决 K3s 部署在旧镜像(2026.03.07-1)上缺少 prometheus_client 时服务崩溃的问题" ([d9d6b3b](https://github.com/tomturing/hci-troubleshoot-platform/commit/d9d6b3be0fe5b7ccdf7a2fdee22bf31251af2756))
* **ci:** build-hci-openclaw 登录 ghcr.io 改用 GHCR_PAT 以支持首次创建新包 ([#48](https://github.com/tomturing/hci-troubleshoot-platform/issues/48))（ubuntu_sz_copilot） ([7bc04dd](https://github.com/tomturing/hci-troubleshoot-platform/commit/7bc04dda2e0c599b08897aab936db8b679d6c6e9))
* **ci:** upload-sarif continue-on-error ([#15](https://github.com/tomturing/hci-troubleshoot-platform/issues/15)) ([6d55bdc](https://github.com/tomturing/hci-troubleshoot-platform/commit/6d55bdc33e1182229fb801da0820985789ad7574))
* **ci:** 为 release-please PR 添加状态检查旁路，解决发版 PR 永久卡住问题 ([#111](https://github.com/tomturing/hci-troubleshoot-platform/issues/111)) ([c644b81](https://github.com/tomturing/hci-troubleshoot-platform/commit/c644b815dd5c47ff5cac46a671b78c4b7caaac19))
* **ci:** 修复 build-and-push 和 auto-deploy-dev 的 always() 条件 ([#40](https://github.com/tomturing/hci-troubleshoot-platform/issues/40)) ([cc4d883](https://github.com/tomturing/hci-troubleshoot-platform/commit/cc4d8835bfa13c4304d33fca82891a8ea9aec737))
* **ci:** 修复 Docker build context ([#13](https://github.com/tomturing/hci-troubleshoot-platform/issues/13)) ([ac956ef](https://github.com/tomturing/hci-troubleshoot-platform/commit/ac956efdb41f1ee0cf0dd3dc92951beefa293250))
* **ci:** 修复 push 事件时所有 job 被级联跳过的问题 ([#12](https://github.com/tomturing/hci-troubleshoot-platform/issues/12)) ([2bd7021](https://github.com/tomturing/hci-troubleshoot-platform/commit/2bd7021862de5ea873b053da84ea34cd1c334edf))
* **ci:** 修复 push 到 main 时构建链被跳过的问题 ([#38](https://github.com/tomturing/hci-troubleshoot-platform/issues/38)) ([32eb7fb](https://github.com/tomturing/hci-troubleshoot-platform/commit/32eb7fbe25450518179b4d163a4929592181947c))
* **ci:** 将 hci-openclaw 从主构建矩阵拆分为独立 workflow ([#47](https://github.com/tomturing/hci-troubleshoot-platform/issues/47))（ubuntu_sz_copilot） ([cd5a683](https://github.com/tomturing/hci-troubleshoot-platform/commit/cd5a683b4a15b844eb9cb5aa66bdcd514964f750))
* **ci:** 将 hci-openclaw 从主构建矩阵拆分为独立 workflow ([#49](https://github.com/tomturing/hci-troubleshoot-platform/issues/49)) ([5ae7744](https://github.com/tomturing/hci-troubleshoot-platform/commit/5ae77449c4bc29a6900e600446d5f18237fa15dc))
* **ci:** 添加 always() 条件确保构建链在依赖跳过时仍能运行 ([#39](https://github.com/tomturing/hci-troubleshoot-platform/issues/39)) ([3c8dbef](https://github.com/tomturing/hci-troubleshoot-platform/commit/3c8dbefeec5761c594487a6ddf621352f852d763))
* **ci:** 环境同步改为串行执行，避免并发竞争 ([#104](https://github.com/tomturing/hci-troubleshoot-platform/issues/104)) ([04f51b9](https://github.com/tomturing/hci-troubleshoot-platform/commit/04f51b92de0a08f32ab750e1c3ce85006c0cd495))
* conversation-service DNS 搜索域硬编码旧命名空间 hci-troubleshoot ([#30](https://github.com/tomturing/hci-troubleshoot-platform/issues/30)) ([26410df](https://github.com/tomturing/hci-troubleshoot-platform/commit/26410dffefbd03e937befc037b9158576cf3e150))
* **conversation:** AI回复消息后台任务使用独立session保存 ([59abbb2](https://github.com/tomturing/hci-troubleshoot-platform/commit/59abbb22b7ab986511209485972809cb5bf4c0f6))
* **conversation:** get_messages 改用独立 session，彻底修复 assistant 消息不落库 ([bc762b5](https://github.com/tomturing/hci-troubleshoot-platform/commit/bc762b554945ee819364038252f687401f5fef23))
* **conversation:** 恢复 9d56852 回退的 knowledge-rag 依赖注入链 ([#45](https://github.com/tomturing/hci-troubleshoot-platform/issues/45))（ubuntu_sz_copilot） ([9595b2e](https://github.com/tomturing/hci-troubleshoot-platform/commit/9595b2ede8ad6e0327900c42f1dbfe11341bd492))
* **dashboard:** 三个面板添加 Init 容器指标覆盖 ImagePullBackOff 初始化容器异常 ([#57](https://github.com/tomturing/hci-troubleshoot-platform/issues/57)) ([9fb8823](https://github.com/tomturing/hci-troubleshoot-platform/commit/9fb8823848db0ed9184454d47e0c19ddcab17676))
* **db:** 修复 idle-in-transaction 导致 message INSERT 阻塞 ([be798ee](https://github.com/tomturing/hci-troubleshoot-platform/commit/be798ee57689e62a185f76931ba90869bf9e78f7))
* **db:** 修正 migration 文件命名格式 — 纯数字前缀以匹配 dbmate version 提取正则 ([#85](https://github.com/tomturing/hci-troubleshoot-platform/issues/85)) ([5894c7c](https://github.com/tomturing/hci-troubleshoot-platform/commit/5894c7c50fe2b32d88ea6ad4aa4af8bd18d6bedd))
* **deploy:** 修复 staging 环境 Pod 崩溃问题 ([#64](https://github.com/tomturing/hci-troubleshoot-platform/issues/64)) ([5be4a0c](https://github.com/tomturing/hci-troubleshoot-platform/commit/5be4a0c5460e8ae569ccc15a8a4bd1fefd7dfdb4))
* **deploy:** 修复健康检查路径和 Node.js 内存配置 ([#65](https://github.com/tomturing/hci-troubleshoot-platform/issues/65)) ([1db5973](https://github.com/tomturing/hci-troubleshoot-platform/commit/1db597304adb5a7d2f449bcebfcdf7ed016701de))
* **docs:** 将 admin-ui 事件文档移至模块目录下 ([#105](https://github.com/tomturing/hci-troubleshoot-platform/issues/105)) ([84b1a08](https://github.com/tomturing/hci-troubleshoot-platform/commit/84b1a08d85aa0b7477327937ca56dbb2e76edf44))
* **frontend:** admin tsconfig.node.json 补 composite:true，修复 vue-tsc -b 构建失败 ([bd8707b](https://github.com/tomturing/hci-troubleshoot-platform/commit/bd8707bc2f7b1e9dce103ec1b87420625e1746a4))
* **frontend:** 彻底规避 Docker 构建卡死和 lockfile 问题 ([e0d9f6b](https://github.com/tomturing/hci-troubleshoot-platform/commit/e0d9f6be86d746d2eb462183764eafc9c2923390))
* **gitops:** 固定Helm releaseName以消除不可变字段同步失败 ([43614a4](https://github.com/tomturing/hci-troubleshoot-platform/commit/43614a4fab7d85fffbb620bedda586eb8dccc796))
* hci-platform-data-staging 使用多源配置引用环境仓库 values ([#75](https://github.com/tomturing/hci-troubleshoot-platform/issues/75)) ([1fef02a](https://github.com/tomturing/hci-troubleshoot-platform/commit/1fef02a31ac50ff677b53f014aa79fed3cb5f6be))
* Helm ConfigMap 固化 KB_ENABLED 配置，与 kbService.enabled 联动 ([2935d49](https://github.com/tomturing/hci-troubleshoot-platform/commit/2935d49987838f6218b110b508477cae9685243a))
* **helm,build:** 修复无域名部署的5个生产环境问题 ([0393415](https://github.com/tomturing/hci-troubleshoot-platform/commit/03934157c562d4abdbd1bb9f3f9a0ec0ee103cae))
* **helm:** DATABASE_URL 改用可配置 postgresHost，支持跨 namespace 数据库访问 ([#23](https://github.com/tomturing/hci-troubleshoot-platform/issues/23)) ([200fe00](https://github.com/tomturing/hci-troubleshoot-platform/commit/200fe009b446c7d9a82234b0eca2aae0a0a66a7c))
* **helm:** hci-platform values.yaml 补充 postgres.nodePort 字段 ([#44](https://github.com/tomturing/hci-troubleshoot-platform/issues/44))（ubuntu_sz_claude） ([08f8f0f](https://github.com/tomturing/hci-troubleshoot-platform/commit/08f8f0ff00c854ac2d0c2d9c507aef424dc28ec1))
* **helm:** 去除 nginx emptyDir 重复配置（PR [#63](https://github.com/tomturing/hci-troubleshoot-platform/issues/63) 与 [#64](https://github.com/tomturing/hci-troubleshoot-platform/issues/64) 冲突遗留） ([#66](https://github.com/tomturing/hci-troubleshoot-platform/issues/66))（ubuntu_sz_copilot） ([0522700](https://github.com/tomturing/hci-troubleshoot-platform/commit/05227005eb64a611dff03b8b9921d580843d84cb))
* **helm:** 拆分 postgres Service 为 Headless + 独立 NodePort ([#50](https://github.com/tomturing/hci-troubleshoot-platform/issues/50)) ([5ac86a7](https://github.com/tomturing/hci-troubleshoot-platform/commit/5ac86a7b98fdb65fc521c4f0f87ff6ea935662f0))
* **infra:** 集群最佳实践补丁 — GitOps/资源限制/存储/RS清理 ([#55](https://github.com/tomturing/hci-troubleshoot-platform/issues/55))（ubuntu_gs_copilot） ([e67cb67](https://github.com/tomturing/hci-troubleshoot-platform/commit/e67cb67a1747b8909177bbc67a72740c0d452365))
* **k3s:** prevent premature exits in deployment scripts ([663d5cb](https://github.com/tomturing/hci-troubleshoot-platform/commit/663d5cb8b5ee5394b7caebb8aacdf4b4ac77d335))
* kb-service 204 response_class + Trivy 仅上报 ([#14](https://github.com/tomturing/hci-troubleshoot-platform/issues/14)) ([3f5326d](https://github.com/tomturing/hci-troubleshoot-platform/commit/3f5326d18e1dba43c1909c8d87cf95042b16f73e))
* **kb-service:** response_model=None 修复 FastAPI 0.109.0 status_code=204 断言 ([#16](https://github.com/tomturing/hci-troubleshoot-platform/issues/16)) ([b480f6c](https://github.com/tomturing/hci-troubleshoot-platform/commit/b480f6c9aaa513bcb56ddb91c82c6bdf1d3a2270))
* kbService 添加 enabled: false 开关，防止未构建镜像时意外部署 ([f1b3e39](https://github.com/tomturing/hci-troubleshoot-platform/commit/f1b3e39103e6aa8b6ef5a753cfbe18cfd43dc612))
* **kb:** SQLAlchemy metadata保留字修复+BGE hostPath挂载，kbService启用 ([6a231dd](https://github.com/tomturing/hci-troubleshoot-platform/commit/6a231dd3459d8bb5d526a668f08fbab5f5dd8443))
* **kb:** 清理冗余 embedding 环境变量，移除 sop-skills HostPath 挂载，保留 BGE 本地模型路径 ([e88312d](https://github.com/tomturing/hci-troubleshoot-platform/commit/e88312da0a1a5c3bc6ac9c03342eaee4de550d10))
* **learningclaw:** DUAL-010~013 — API Key 注入链修复 + WebUI 接入 ([#21](https://github.com/tomturing/hci-troubleshoot-platform/issues/21)) ([02bc157](https://github.com/tomturing/hci-troubleshoot-platform/commit/02bc1577e1faae5bdf8f28d2f6145ecf53ae7b46))
* local K3s deploy fixes & one-click deploy improvements ([#17](https://github.com/tomturing/hci-troubleshoot-platform/issues/17)) ([b1695ea](https://github.com/tomturing/hci-troubleshoot-platform/commit/b1695ea656724e7a8c3b31e898bef4eca370b42b))
* **markdown:** 修复 marked breaks:true 在代码块内插入&lt;br&gt;破坏格式的问题 ([89b2767](https://github.com/tomturing/hci-troubleshoot-platform/commit/89b276748d25628ec8038325333bb7762a73271f))
* **observability:** 修复 Grafana 无数据问题 ([b6e7dec](https://github.com/tomturing/hci-troubleshoot-platform/commit/b6e7dec9eaa305af13966b24642e0d022056082f))
* **obs:** 修正 PVC storageClass 为 local-path 匹配现有 PVC，添加 ignoreDifferen… ([#9](https://github.com/tomturing/hci-troubleshoot-platform/issues/9)) ([9b65aa9](https://github.com/tomturing/hci-troubleshoot-platform/commit/9b65aa991d71f6b3849ed8f9b4f62b3f6ef951ee))
* **obs:** 默认启用 Grafana Ingress 修复 /grafana 路由被吞问题 ([#53](https://github.com/tomturing/hci-troubleshoot-platform/issues/53))（ubuntu_sz_claude） ([f392189](https://github.com/tomturing/hci-troubleshoot-platform/commit/f392189f8adbede8afa88e8ef4b9daaf7a3a1e24))
* openclaw ConfigMap subPath 权限问题 —— 改为 initContainer 复制方案 ([ac29ad7](https://github.com/tomturing/hci-troubleshoot-platform/commit/ac29ad70518cae347b3893ebe18f5d82988c4168))
* openclaw Deployment 绕过 Clash fake-ip DNS 劫持 ([2a26e61](https://github.com/tomturing/hci-troubleshoot-platform/commit/2a26e61acc5f7b410b6aa8e307d8eee332458451))
* **openclaw:** /openclaw redirect 带 token，修复 WS 1008 device identity required ([9620363](https://github.com/tomturing/hci-troubleshoot-platform/commit/962036357fc641184d9b562a2fff0be33c9ae0c1))
* **openclaw:** hostPath改为/home/node与容器路径完全一致 ([4e3acac](https://github.com/tomturing/hci-troubleshoot-platform/commit/4e3acac4d4d8d6fd2b7786915616b3714721256a))
* **openclaw:** securityContext runAsUser=1001 匹配宿主机node用户 ([574b0c6](https://github.com/tomturing/hci-troubleshoot-platform/commit/574b0c62a0dbf399dee61aede80afe98aae1e53f))
* pnpm-workspace.yaml 引号规范化（统一使用双引号） ([7ea2c78](https://github.com/tomturing/hci-troubleshoot-platform/commit/7ea2c78ab1e540417df9f1a11b5392eb2ba74dfe))
* **prometheus:** 新增 kube-state-metrics 专用 scrape job 避免 namespace 标签被覆盖 ([#56](https://github.com/tomturing/hci-troubleshoot-platform/issues/56)) ([cf3823e](https://github.com/tomturing/hci-troubleshoot-platform/commit/cf3823e64a59beb5b411840b340599bf56d50db0))
* **rag:** 打通k3s RAG全链路 - 修复向量维度及Token配置 ([5ac7c41](https://github.com/tomturing/hci-troubleshoot-platform/commit/5ac7c4129629afcfcae5956923a88b3844ead96a))
* repair multi-assistant routing and scheduler endpoint flow ([114d069](https://github.com/tomturing/hci-troubleshoot-platform/commit/114d0691e027db60c2efd5e0cd74263eb80d68bb))
* **scheduler, helm:** 修复 ProductionClaw 启动失败问题 ([#36](https://github.com/tomturing/hci-troubleshoot-platform/issues/36)) ([9766a95](https://github.com/tomturing/hci-troubleshoot-platform/commit/9766a95b6068bf3e5c736159254ee50c7f395d47))
* **secret:** rename ZAI_API_KEY -&gt; OPENCLAW_API_KEY to match conversation-service config.py ([#19](https://github.com/tomturing/hci-troubleshoot-platform/issues/19)) ([6a0d235](https://github.com/tomturing/hci-troubleshoot-platform/commit/6a0d23532644f2acd56b541e3a0b8b4e7b608e3a))
* **security:** securityContext加固 + Pod重启告警规则 + LimitRange兜底 ([#7](https://github.com/tomturing/hci-troubleshoot-platform/issues/7)/[#8](https://github.com/tomturing/hci-troubleshoot-platform/issues/8)/[#9](https://github.com/tomturing/hci-troubleshoot-platform/issues/9)) ([89b9c08](https://github.com/tomturing/hci-troubleshoot-platform/commit/89b9c083f06d1fb97d726a00298891f8fb328d26))
* **seeds:** 修复 00_baseline.sql 的验证 SQL — 移除不存在的 ts 列，与 dbmate 原生表结构对齐 ([#84](https://github.com/tomturing/hci-troubleshoot-platform/issues/84)) ([7365c1e](https://github.com/tomturing/hci-troubleshoot-platform/commit/7365c1e79841efa1b44c8ff33be53c6b32200db4))
* **sop:** 修复SOP技能目录挂载及关键字覆盖 ([6fbb4cd](https://github.com/tomturing/hci-troubleshoot-platform/commit/6fbb4cddcfb74302dc5087ff9c57a53463c0c520))
* **store:** 修复流式结束后isStreaming不触发Vue重渲染的根本原因 ([b4e4e10](https://github.com/tomturing/hci-troubleshoot-platform/commit/b4e4e1051b5c499b4be5e9d08998d0ccb5e925b7))
* **store:** 方案A-在流式结束前await nextTick消灭阶段2到3切换竞态 ([6204fb5](https://github.com/tomturing/hci-troubleshoot-platform/commit/6204fb5f14fd4ecc1acdd7ee49a772833802527a))
* **stream/ui:** 修复SSE流截断换行的问题，并在流式期间暂缓渲染交互式命令块 ([dcab8c8](https://github.com/tomturing/hci-troubleshoot-platform/commit/dcab8c8503675715c6a3917221bd3055efaa3d89))
* Task26/27 E2E 验证完成 - KB push 链路修复 + Scheduler RBAC + openclaw 注册 ([9e52efa](https://github.com/tomturing/hci-troubleshoot-platform/commit/9e52efab59a6cc01ed4b2c350369a07c59bfaae3))
* **task30:** 修复 conversation-service 评分再计算的两处缺陷 ([f3d20d6](https://github.com/tomturing/hci-troubleshoot-platform/commit/f3d20d6eb1e2f7afe517e9bb4fa98259052d02ef))
* **task30:** 修复评分链路与重复提问统计关键缺陷 ([d820e89](https://github.com/tomturing/hci-troubleshoot-platform/commit/d820e896abdf81ad8cf3fc277160542304b0feed))
* **terminal-bridge:** rewrite bat in pure ASCII to fix GBK/UTF-8 encoding issue ([9b78f97](https://github.com/tomturing/hci-troubleshoot-platform/commit/9b78f970c4a49a5336adb4412b16ff37757c864b))
* **terminal:** 修复下载按钮无效问题 ([cb03527](https://github.com/tomturing/hci-troubleshoot-platform/commit/cb03527f18da08142a83684759a583202cd0c475))
* **ui:** 修复流式输出期间Markdown代码块无法实时截断解析渲染为CommandBlock的Bug ([0810d4a](https://github.com/tomturing/hci-troubleshoot-platform/commit/0810d4af73e02702d2a36a05f3d6f3ca8c13cd8c))
* **ui:** 修复阶段3升级渲染不生效的根本原因 ([564a8e0](https://github.com/tomturing/hci-troubleshoot-platform/commit/564a8e0eb128e9fc4520ccf74a058c22cfaa13df))
* **ui:** 分离SSH终端与历史工单的抽屉宽度状态，修复拖动相互影响问题 ([4274aa5](https://github.com/tomturing/hci-troubleshoot-platform/commit/4274aa5e1c3cdec882be1f7c12d2e1dd4a157411))
* **ui:** 方案B-统一流式与完成态渲染通道，彻底消灭切换竞态 ([7c032d1](https://github.com/tomturing/hci-troubleshoot-platform/commit/7c032d1cf720dd434890d7ae92ffe0a7f54d756a))
* **ui:** 方案B优化-重构段落拆分逻辑，放弃正则替换为 marked AST 彻底解决 Markdown 解析问题 ([3cddf79](https://github.com/tomturing/hci-troubleshoot-platform/commit/3cddf7966479c2cef2884e345a381c87d5d59448))
* **ui:** 解决el-drawer钉住后点击穿透被阻挡问题并支持边缘拖拽改变宽度 ([e09f1fa](https://github.com/tomturing/hci-troubleshoot-platform/commit/e09f1faf68ceca4f254ad86f60d7311946e357b1))
* VK 固定端口 9527 + 交叉审查规则升级为强制 ([b2d01fc](https://github.com/tomturing/hci-troubleshoot-platform/commit/b2d01fc158ef90839583e7b0a69a2a94daa2a781))
* vk-hooks 使用 status_id + status_map.json 修复 REST API 状态更新 ([2916d2b](https://github.com/tomturing/hci-troubleshoot-platform/commit/2916d2b6d788d6ebfd4b6046c683cf2edfa3af98))
* 修复 categories.set_dependencies 参数签名不匹配 ([#95](https://github.com/tomturing/hci-troubleshoot-platform/issues/95)) ([54484e7](https://github.com/tomturing/hci-troubleshoot-platform/commit/54484e7346ebe13064573985edb4c6194bfe8d4f))
* 修复 DB schema 漂移 — 补齐缺失表列 + 全套事件文档 ([#108](https://github.com/tomturing/hci-troubleshoot-platform/issues/108)) ([80b56fc](https://github.com/tomturing/hci-troubleshoot-platform/commit/80b56fcf4238748671764dc957f7727f8d365990))
* 修复 KbCategory 模型重复定义导致 Table already defined 错误 ([#94](https://github.com/tomturing/hci-troubleshoot-platform/issues/94)) ([3b8d90a](https://github.com/tomturing/hci-troubleshoot-platform/commit/3b8d90a15d1dc4e0e9e528de2f21aa620becfcf3))
* 修复 Prometheus 告警规则 namespace 过滤条件 ([#70](https://github.com/tomturing/hci-troubleshoot-platform/issues/70)) ([beba21a](https://github.com/tomturing/hci-troubleshoot-platform/commit/beba21a29c7afe4118b5b7278ac931268f9f1098))
* 修复 seed_categories.py JSONB 字符串解析问题 ([#93](https://github.com/tomturing/hci-troubleshoot-platform/issues/93)) ([3d373c4](https://github.com/tomturing/hci-troubleshoot-platform/commit/3d373c4272a28c21b5529e96043f50ab773943ac))
* 修复 SQLAlchemy 保留属性 metadata 冲突导致 kb-service 启动失败 ([#92](https://github.com/tomturing/hci-troubleshoot-platform/issues/92)) ([6807c1f](https://github.com/tomturing/hci-troubleshoot-platform/commit/6807c1fb15e6195fee8e32ebe79d19bc90a3693a))
* 修复7项代码缺陷并补全 Helm kb-service 模板 ([90f0c0d](https://github.com/tomturing/hci-troubleshoot-platform/commit/90f0c0d4f9c93dd9b6ce27fa26e0fcd2cb9c0191))
* 修复K3s发布脚本静默退出与sudo交互卡住 ([1baac2b](https://github.com/tomturing/hci-troubleshoot-platform/commit/1baac2b51c46680d33f0adb160906fb41ebe354e))
* 修复全部架构审查问题，Helm lint 通过 ([1121437](https://github.com/tomturing/hci-troubleshoot-platform/commit/1121437f715b9f462bacf5dd6024c7a4aa5a35b0))
* 修复状态机设计问题 ([#102](https://github.com/tomturing/hci-troubleshoot-platform/issues/102)) ([37d44f5](https://github.com/tomturing/hci-troubleshoot-platform/commit/37d44f5384832c9643e7c06a4312f1a5616c0865))
* 修复质量门禁脚本 + ruff 全量格式化 ([bfb6051](https://github.com/tomturing/hci-troubleshoot-platform/commit/bfb6051c0fcb0a1b20ea1ce608c94b2b04b0e700))
* 在 prometheus Service 定义前添加缺失的 YAML 文档分隔符 ([#73](https://github.com/tomturing/hci-troubleshoot-platform/issues/73)) ([445ece1](https://github.com/tomturing/hci-troubleshoot-platform/commit/445ece1f93c1fc9ee21f48c9ce0e6dfb51bcf049))
* 增加 db-password-check Job 超时时间到 180s ([#72](https://github.com/tomturing/hci-troubleshoot-platform/issues/72)) ([a5b08e6](https://github.com/tomturing/hci-troubleshoot-platform/commit/a5b08e6e3768e619d2960f4f45ee501e17316cd6))
* 增加Helm发布锁保护并固定kubeconfig ([330c670](https://github.com/tomturing/hci-troubleshoot-platform/commit/330c67056dcd4a496869d700efde8304fa72025b))
* 恢复 admin-ui/customer-ui nginx emptyDir volumes ([#68](https://github.com/tomturing/hci-troubleshoot-platform/issues/68)) ([528aa21](https://github.com/tomturing/hci-troubleshoot-platform/commit/528aa2156df90daf134a97af5efde6389af6115f))
* 移除 CoreDNS hosts 插件配置，改由 Clash fake-ip-filter 解决 GitHub 解析问题 ([#71](https://github.com/tomturing/hci-troubleshoot-platform/issues/71)) ([097a3a1](https://github.com/tomturing/hci-troubleshoot-platform/commit/097a3a1615d63cfd1890baa732da097fe1a24572))
* 补充 ruff ignore 规则 (B904/UP042/N806/F841/B007) + 修复 conftest.py import 排序 ([de97a41](https://github.com/tomturing/hci-troubleshoot-platform/commit/de97a412efc05cd3c8ce7c1d8a26591bb067760a))
* 质量门禁前端 lint 检查增强 (检测 .pnpm 目录 + lint 脚本存在性) ([5afe463](https://github.com/tomturing/hci-troubleshoot-platform/commit/5afe46337344dc656770b75626949af393645eae))
* 验证期间发现的两项遗漏问题修复 ([7458fd2](https://github.com/tomturing/hci-troubleshoot-platform/commit/7458fd28e25c1d1b87c57b3b780429e405d1adc6))


### ♻️ 代码重构

* **docs:** 重组 solution/events 目录结构 ([#107](https://github.com/tomturing/hci-troubleshoot-platform/issues/107)) ([b694108](https://github.com/tomturing/hci-troubleshoot-platform/commit/b694108cd01eefc819d0c8cb1e1af599e7dd393f))
* **gitops:** 按 ArgoCD 实例重构 argo-apps 目录为 local/ 和 cloud/ ([#25](https://github.com/tomturing/hci-troubleshoot-platform/issues/25)) ([0be0185](https://github.com/tomturing/hci-troubleshoot-platform/commit/0be0185cc9e496717948470ceec88dfcc571bc22))
* **kbd:** 数据管道脚本改造为调用 kb-service API ([#90](https://github.com/tomturing/hci-troubleshoot-platform/issues/90)) ([9cfd107](https://github.com/tomturing/hci-troubleshoot-platform/commit/9cfd1076da01f858fc8568d5ce452b0f07218134))
* **observability:** alertmanager-config Secret 迁入双仓模式（Helm + env repo） ([2ae3442](https://github.com/tomturing/hci-troubleshoot-platform/commit/2ae3442a641a88e6253e8014fe37a2bbffb8c876))
* **terminal-bridge:** 替换为 Go 实现，移除 Python/PyInstaller 方案 ([9224e51](https://github.com/tomturing/hci-troubleshoot-platform/commit/9224e51b061af4f10c70d130180018808a78d627))
* **terminal:** 改为 localhost Bridge 模式，移除服务端 SSH 代理 ([86b3d39](https://github.com/tomturing/hci-troubleshoot-platform/commit/86b3d39894812b67449a036987991e79339b13ab))
* **ui:** 恢复历史工单与SSH终端为抽屉(Drawer)，并且宽度动态适配为两翼 ([08a132f](https://github.com/tomturing/hci-troubleshoot-platform/commit/08a132fb8ca47f8f2c94aa8b1d650dad9d5c47d6))
* 知识库架构重构 —— 删除 data-pipeline、清理 T17 死代码、文档目录重构 ([#100](https://github.com/tomturing/hci-troubleshoot-platform/issues/100)) ([5d807f4](https://github.com/tomturing/hci-troubleshoot-platform/commit/5d807f48c1d33a8783838ef166f74c630ba2be01))


### 📝 文档

* add DUAL-012 entry for pairing required on external browser access ([02bc157](https://github.com/tomturing/hci-troubleshoot-platform/commit/02bc1577e1faae5bdf8f28d2f6145ecf53ae7b46))
* add DUAL-012 entry for pairing required on external browser access ([523b1fe](https://github.com/tomturing/hci-troubleshoot-platform/commit/523b1fe8f818e74dd6dd1d083d39e2b06a20b4ca))
* **AGENTS.md:** 补充 Copilot 提交 PR 的两条强制规则 ([#97](https://github.com/tomturing/hci-troubleshoot-platform/issues/97)) ([0747298](https://github.com/tomturing/hci-troubleshoot-platform/commit/07472981d8e0cceb434df5d0a30b12a80dead02d))
* **copilot-instructions:** 补充 Copilot PR 提交两条强制规则 ([#98](https://github.com/tomturing/hci-troubleshoot-platform/issues/98)) ([c28f86f](https://github.com/tomturing/hci-troubleshoot-platform/commit/c28f86f49c2b5e397f39af4f94a7ac28ca302fee))
* **deploy:** 建立统一配置体系并汇总部署问题 ([1b3a46a](https://github.com/tomturing/hci-troubleshoot-platform/commit/1b3a46a4a8ef2f88c1801af1538431e0be746343))
* **knowledge-base:** 重构知识库设计文档，归档老方案 ([#101](https://github.com/tomturing/hci-troubleshoot-platform/issues/101)) ([59ae7be](https://github.com/tomturing/hci-troubleshoot-platform/commit/59ae7be8c7ba1035b0554aa4094ffa7e3f84205b))
* update README and progress for frontend completion ([76f6143](https://github.com/tomturing/hci-troubleshoot-platform/commit/76f61438515c3e9239246faf3b2f5b9ce6f63576))
* workflow.md 补充 status_map.json 初始化说明 ([bbaeb4d](https://github.com/tomturing/hci-troubleshoot-platform/commit/bbaeb4d19dcb022b8281339b6b822673910a972f))
* 优化 13_RAG设计 流程图和文件树样式 ([6eb03a5](https://github.com/tomturing/hci-troubleshoot-platform/commit/6eb03a5f8a86e51b9ed5024ec449fab595b07acf))
* 修复 README.md 中的 ArgoCD App 路径和描述 ([#41](https://github.com/tomturing/hci-troubleshoot-platform/issues/41))（ubuntu_sz_copilot） ([3a62b30](https://github.com/tomturing/hci-troubleshoot-platform/commit/3a62b30a34ba77fea214e146d6e2f256582e8485))
* 同步 Git 规则到 AGENTS.md 和 copilot-instructions.md ([#82](https://github.com/tomturing/hci-troubleshoot-platform/issues/82)) ([63ef07f](https://github.com/tomturing/hci-troubleshoot-platform/commit/63ef07f3d2ec37b58c29687fd39ff2d70c86d399))
* 同步 Task 30 完成状态到项目进展文档 ([5225849](https://github.com/tomturing/hci-troubleshoot-platform/commit/5225849cd7dd5ecbe69be5a814530658335f8640))
* 序号对齐 — 13_AI→12_AI，14_RAG→13_RAG，更新全库引用 ([3808651](https://github.com/tomturing/hci-troubleshoot-platform/commit/3808651bc214169cb7c18e8d1517820b6fb5c0a7))
* 数据库架构重构 - 基于知识库方案B精简至11张表 ([#86](https://github.com/tomturing/hci-troubleshoot-platform/issues/86)) ([54bf2e6](https://github.com/tomturing/hci-troubleshoot-platform/commit/54bf2e655af718d52313385c02a9f518d7c6b9d7))
* 文档整理合并 v4.0 ([596cb4f](https://github.com/tomturing/hci-troubleshoot-platform/commit/596cb4f2750f30619e61d31cf0f1257fd4a3cc2d))
* 新增 AI层×RAG层对接架构决策文档(doc 15)，更新 01/12/13 交叉引用 ([a5f6919](https://github.com/tomturing/hci-troubleshoot-platform/commit/a5f6919bb36560c99f5aa476d2b1b3f5e40b9a6d))
* 新增 Task 28/31 Agent 任务编排文档（19_任务编排.md） ([4ef143a](https://github.com/tomturing/hci-troubleshoot-platform/commit/4ef143a1c46c1d1939268b0956261a86c510f6fb))
* 新增§8迭代记录（Task25 ETL + Task28 Prometheus） ([215cf28](https://github.com/tomturing/hci-troubleshoot-platform/commit/215cf28580566d4124270c2d88b2cf8891cc9a8d))
* 新增20_任务编排补齐prompt_audit实现缺口 ([02a2194](https://github.com/tomturing/hci-troubleshoot-platform/commit/02a219441d507decea3030184262ebfe1f35415a))
* 新增任务编排设计文档(18_任务编排.md) ([7624b3e](https://github.com/tomturing/hci-troubleshoot-platform/commit/7624b3e40b127a0c65d83bf354dc406406550196))
* 新增评分评价体系与客户端设计文档，完善任务计划与验收标准 ([0790a5c](https://github.com/tomturing/hci-troubleshoot-platform/commit/0790a5c0e58436d0c83192b73208c958a13180c6))
* 更新 09_项目进展.md Task 26/27 状态为已完成，新增 §2.18 验证记录 ([9e52efa](https://github.com/tomturing/hci-troubleshoot-platform/commit/9e52efab59a6cc01ed4b2c350369a07c59bfaae3))
* 更新交付标准化看板，补充 GAP-06/ADR-005 完成状态 ([dd1c1c1](https://github.com/tomturing/hci-troubleshoot-platform/commit/dd1c1c13266f2866b17d868a6d443058610b26e6))
* 更新详细执行计划（P0-P3，Task25-29）- 2026-03-10 ([67ca425](https://github.com/tomturing/hci-troubleshoot-platform/commit/67ca4258df29260b9cff01e372ffbd2224d6aa9e))
* 更新项目进展 - Task 29 完成记录（§8.4 新增） ([0aa3e75](https://github.com/tomturing/hci-troubleshoot-platform/commit/0aa3e7593569d59208011e55a97e53f9876db364))
* 更新项目进展文档（2026-02-27） ([af51bff](https://github.com/tomturing/hci-troubleshoot-platform/commit/af51bff3b426e76691c83756a22dcdf9dfcfb796))
* 更新项目进展文档至 2026-03-10 ([bfbb6ad](https://github.com/tomturing/hci-troubleshoot-platform/commit/bfbb6ad57766304aa58be2ce5a72caa5031df5d1))
* 添加团队避坑指南 skill ([#35](https://github.com/tomturing/hci-troubleshoot-platform/issues/35)) ([d142030](https://github.com/tomturing/hci-troubleshoot-platform/commit/d1420308dbcc097cffc91cf24ac2af31fabadbdb))
* 生产部署问题全景复盘 + nginx HTML no-cache 修复 ([bd5f55d](https://github.com/tomturing/hci-troubleshoot-platform/commit/bd5f55dd5e252e6675563e897178f5f3c707d417))
* 统一 ArgoCD NodePort 端口为 30808（本地+云端） ([#74](https://github.com/tomturing/hci-troubleshoot-platform/issues/74)) ([8d9f43a](https://github.com/tomturing/hci-troubleshoot-platform/commit/8d9f43ad7b04504920082b0e0d4a9c3b19400755))
* 补充 REVISION 13 修复记录（nginx no-cache 固化、OpenClaw 设备身份/LLM 超时、Docker build Clash 劫持） ([6252b3c](https://github.com/tomturing/hci-troubleshoot-platform/commit/6252b3c5ccfec1f3104d02b3398e74b754fb04e4))
* 补充三阶段横向对比矩阵及阶段三详细评估到 doc 15 ([4972fba](https://github.com/tomturing/hci-troubleshoot-platform/commit/4972fba8cdb6abc55c330768e98a8023dfe78a6b))
* 补充任务编排文档与模板 ([b1806ee](https://github.com/tomturing/hci-troubleshoot-platform/commit/b1806eea54b71b2d74c4eb597048ff42b025dffa))
* 阶段一外挂RAG验收完成（2026-03-10）- 4-Tier Prompt+SOP命中+Loki日志全部通过 ([38e8381](https://github.com/tomturing/hci-troubleshoot-platform/commit/38e838182be8404349b681a1efd275aec01de297))
