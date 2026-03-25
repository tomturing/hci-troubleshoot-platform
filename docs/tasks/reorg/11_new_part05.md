<!--
  分片标识：新 11_完整技术方案.md（实施规范 HOW）— 第 5/5 部分
  内容来源：
    - 原 08 §六（第 1566–1610 行：Phase 3 实施进度快照）
    - 原 11 §十一（第 564–634 行：Phase 3 实施进度快照）
  合并目标：新 11 文档 §十二（进度快照，两个来源合并为一节）
  说明：原 08§六 和 11§十一 内容高度重叠，以 11§十一 更详细版本为主，08§六 补充架构现状部分
-->

## 十二、实施进度快照（2026-03-25）

> 本节记录截至 2026-03-25 的真实实施状态，是**差距分析**的直接来源。

### 12.1 整体 Phase 进度

| Phase | 里程碑 | 状态 |
|-------|--------|------|
| Phase 0 | Prompt 手术（解除"知识库代言人"枷锁） | ✅ 完成 |
| Phase 1 | KB Service 复活 + SOP 入库 | ✅ 完成 |
| Phase 2 | 诊断状态机（diagnostic_stage S0-S6） | ✅ 完成 |
| Phase 3-core | ReactExecutor + 工具层 + 确认机制代码 | ✅ 代码完成 |
| **Phase 3-deploy** | **Helm 注入 REACT_ENABLED + SCP 配置** | **❌ 待做（P0）** |
| Phase 3-fe | stage_change 事件 + 诊断进度条 | ❌ 待做（P1）|
| Phase 3-test | ReAct 端到端集成测试 | ❌ 待做（P2）|
| Phase 4 | vm_power_on/migrate + 监控看板 | ⏳ 计划中 |

### 12.2 当前 Pod 状态（参考）

```
# 截至 2026-03-25（kb-service CrashLoopBackOff 已修复，commit 43caaec）
kb-service            1/1 Running    ← review.py response_model=None 修复后重建
conversation-service  1/1 Running    ← ReactExecutor 代码就绪，REACT_ENABLED=False
productionclaw-pool   3×1/1 Running  ← per-case pod pool 正常
learningclaw-0        1/1 Running    ← 知识反馈闭环待完整接入
openclaw (守护进程)    1/1 Running    ← 作为 fallback，phase 3 稳定后评估废弃
```

### 12.3 已完成组件清单

| 组件 | 文件 | 关键特性 |
|------|------|---------|
| ReAct 执行器 | `core/react_executor.py` | MAX_STEPS=15 防无限循环；风险分级拦截；SSE 桥接 |
| GLM 客户端 | `core/glm_client.py` | JSON 修复；指数退避 3 次；流式合并 |
| 工具注册表 | `core/tool_registry.py` | 15 工具；risk_level 元数据；`get_tools_for_llm()` 格式化 |
| SCP 适配器 | `adapters/scp_adapter.py` | `get_active_alerts / get_failed_tasks / get_vm_list / get_cluster_detail` |
| acli 适配器 | `adapters/acli_adapter.py` | SSH 执行；`_validate_ip/_validate_safe_id` 命令注入防护 |
| 工具路由 | `adapters/tool_router.py` | `category` 字段分发；统一 `execute()` 入口 |
| Prompt 构建 | `services/prompt_builder.py` | 5段式；`diagnostic_stage` 感知 |
| 人工确认 | `services/confirm_service.py` | Redis BRPOP；120s 超时；submit_confirm() |
| 审计日志 | `services/audit_service.py` | `tool_audit_log` 写入；started_at/completed_at |
| 主服务集成 | `services/conversation_service.py` | `REACT_ENABLED + tool_router + confirm_service → agent_mode_available` |
| `/confirm` 端点 | `routes/conversations.py` | `POST /{session_id}/confirm` → ConfirmService |
| 确认弹窗 | `frontend/.../ConfirmDialog.vue` | `confirm_request` 事件渲染 + 确认/取消按钮 |
| SSE 事件处理 | `frontend/.../stores/chat.ts` | `confirm_request / tool_executing / thinking` |
| DB schema | `database/init_schema.sql` | `diagnostic_stage / hypothesis / react_state / tool_audit_log` |

### 12.4 剩余差距清单（优先级排序）

| 优先级 | 差距 | 类型 | 估计工作量 | 解锁的能力 |
|--------|------|------|----------|-----------|
| P0 🔴 | **Helm deployment.yaml 缺 REACT_ENABLED / SCP_BASE_URL / HCI_SSH_* 环境变量** | 部署配置 | 1h | ReactExecutor 在生产环境被激活 |
| P0 🔴 | **env 仓库 values.yaml 缺对应配置节** | 部署配置 | 1h | SCP 和 acli 适配器能连接真实平台 |
| P1 🟠 | **`chat.ts` 缺 `stage_change` 事件分支** | 前端代码 | 2h | 诊断阶段切换在前端可见 |
| P1 🟠 | **缺 `DiagnosticProgress.vue` 组件** | 前端组件 | 4h | S0→S6 进度条 UI |
| P2 🟡 | **缺 ReAct 端到端集成测试** | 测试 | 4h | CI 能验证 ReactExecutor 全流程 |
| P2 🟡 | **`knowledge_retriever.py` 未作为独立模块** | 代码重构 | 3h | 三级知识检索逻辑可独立测试 |
| P3 🟢 | **缺 `diagnostic_state.py` Pydantic 模型** | 代码重构 | 2h | DiagnosticSession 类型化 |
| P3 🟢 | **缺 `dialog_tools.py`** | 功能补充 | 3h | `ask_user()` / `confirm_action()` 工具 |
| P3 🟢 | **缺 Grafana 工具调用监控面板** | 可观测性 | 4h | tool_audit_log 数据可视化 |

### 12.5 解锁 ReactExecutor 的最小步骤

**两步即可在 dev 环境激活 Phase 3（无需写新代码）：**

**步骤 1**：更新 `deploy/helm/hci-platform/templates/conversation-service/deployment.yaml`，在 `env` 段添加：
```yaml
- name: REACT_ENABLED
  value: "true"
- name: SCP_BASE_URL
  value: {{ .Values.scpBaseUrl | quote }}
- name: SCP_API_KEY
  valueFrom:
    secretKeyRef:
      name: hci-secrets
      key: scp-api-key
- name: HCI_SSH_USER
  value: {{ .Values.hciSsh.user | default "admin" | quote }}
- name: HCI_SSH_KEY_PATH
  value: {{ .Values.hciSsh.keyPath | default "" | quote }}
```

**步骤 2**：在 `hci-platform-env` 仓库的 `environments/dev/values.yaml` 中添加：
```yaml
scpBaseUrl: "http://192.168.x.x:8082"   # 替换为实际 SCP 地址
hciSsh:
  user: "admin"
  keyPath: "/etc/ssh/hci_key"
```

完成后重新 Helm upgrade，`conversation_service.agent_mode_available` 即为 `True`，对话自动走 ReAct 路径。

---

*进度快照最后更新：2026-03-25*
