# SOP 发布与变量更新依赖校验设计及修复说明书

本说明书详细阐述了针对超融合基础设施故障排障（SOP）过程中，关于工具与技能可用性校验的整体设计方案、第一性原理推导，以及本次修复的全部修改内容。

---

## 1. 背景与问题定义

在 HCI 智能排障平台 v2.16.0 中，我们发现了以下几个核心阻断与体验问题：
1. **技能工具绑定冲突**：`hci-alert-parsing` 和 `hci-task-parsing` 两个 Skill 在种子数据中绑定的 `allowed_tools` 值为 `'bash'`，而实际 Agent 工具箱注册的工具名为 `'bash_exec'`。这导致执行器在运行时由于工具名称不匹配直接拒绝执行。
2. **SOP 变量门禁阻断范围过宽（根节点死锁）**：`find_missing_guarded_variables_for_node_window` 原本在当前节点是非叶子节点（如根节点）时，仍会把该节点下所有子分支所需的受控变量（如 `disk_dev`, `disk_sn`）全部合并进行前置检测和拦截。这导致排障 Agent 在根节点尝试执行 `acli_exec` 或 `get_active_alerts` 获取基础环境信息时，便因为后续分支的变量未就绪而被提前拦截，导致流程无法推进。
3. **发布阶段缺乏依赖校验**：在 SOP 发布（Approve）以及变量更新（PATCH variable-schema）阶段，系统没有验证变量声明中所依赖的工具（`tool_call`）或技能（`skill_call`）是否在数据库中真实存在且处于启用状态（`is_active = true`）。这导致管理员可以发布一个依赖了不存在或已停用工具/技能的 SOP，直到运行时才报错阻断。

---

## 2. 依赖校验方案设计与第一性原理

为解决“发布阶段缺乏依赖校验”的问题，我们基于第一性原理进行了两种架构方案的对比和选型：

### 方案对比

| 特性 | 方案一：共享 ORM 直接查询数据库（推荐并已实现） | 方案二：跨服务 API 接口调用校验 |
| :--- | :--- | :--- |
| **通信机制** | 通过 SQLAlchemy 异步会话直接读取 `tool_definition` 和 `skill_definition` 表 | `kb-service` 通过 HTTP 请求调用 `conversation-service` 接口 |
| **性能开销** | **极低**。直接利用本地数据库短连接，无网络 I/O 序列化及网络抖动开销 | **中等**。需要发起跨容器的 HTTP 网络请求 |
| **部署依赖** | **无**。仅依赖数据库就绪。 | **高**。需要配置跨服务 URL，且存在部署启动顺序（ArgoCD 同步）和 readiness 的依赖 |
| **服务隔离性** | 存在数据库级别的轻度耦合 | 遵循微服务间的数据和接口强隔离 |

### 第一性原理选型结论
鉴于本平台所有的微服务已共享同一个 PostgreSQL 实例（`hci_troubleshoot`），且所有核心 ORM 数据模型都由 `shared.models` 进行统一维护，我们最终采用**方案一（直接数据库查询）**。该方案最大程度保证了系统的内聚性与健壮性，避免了复杂的网络通信拓扑与容器启动时的死锁。

---

## 3. 详细修改内容

### 3.1 数据库与种子数据修复
- **修改文件**：[03_skill_definitions.sql](file:///aihci/hci-troubleshoot-platform/database/seeds/03_skill_definitions.sql)
  - 将 `hci-alert-parsing` 和 `hci-task-parsing` 的 `allowed_tools` 字段由 `'bash'` 更新为 `'bash_exec'`。
- **Staging 数据库热修复**：
  - 在 staging 数据库中直接执行以下 SQL，完成在线热修复：
    ```sql
    UPDATE skill_definition SET allowed_tools = 'bash_exec' WHERE skill_name IN ('hci-alert-parsing', 'hci-task-parsing');
    ```

### 3.2 变量门禁范围优化
- **修改文件**：[nav.py](file:///aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/sop/nav.py)
  - 优化 `find_missing_guarded_variables_for_node_window` 的提取逻辑：若当前处于非叶子节点，则只检测当前节点本身声明的受控变量，不合并子分支的前置变量。
  - 这保证了 Agent 在根节点可以使用 `acli_exec` 等工具而不被阻断。

### 3.3 SOP 发布与更新依赖校验集成
- **修改文件**：[admin.py](file:///aihci/hci-troubleshoot-platform/backend/kb-service/app/routes/admin.py)
  - 新增 `validate_variable_schema_dependencies` 异步校验函数，解析变量 schema 中所有的 `tool_call` 和 `skill_call`。
  - 在 SOP 审核发布路由（`POST /api/admin/sop/{document_id}/approve`）和变量 Schema 直接更新路由（`PATCH /api/admin/sop/{document_id}/variable-schema`）写入数据库前，强行拦截校验。
  - 如果依赖的工具/技能未注册或 `is_active = false`，抛出 HTTP `422` 异常，返回格式如下，以无缝兼容前端错误弹窗：
    ```json
    {
      "error": "missing_dependencies",
      "message": "SOP 依赖了未注册或未启用的工具：xxx，请先创建或启用它们。",
      "validation_issues": [
        {
          "level": "error",
          "location": "变量声明",
          "line_number": null,
          "message": "依赖了未注册或未启用的工具：'xxx'，请先创建或启用它。"
        }
      ]
    }
    ```

### 3.4 自动化测试覆盖
- **新增文件**：[test_sop_publish_dependencies.py](file:///aihci/hci-troubleshoot-platform/backend/kb-service/tests/test_sop_publish_dependencies.py)
  - 测试用例覆盖：无依赖直接通过、所有依赖正常且启用时通过、工具缺失抛出 422 异常、技能缺失抛出 422 异常、多项依赖同时缺失时完整列出报错。

---

## 4. 验证结果

### 自动化测试
1. **新增依赖校验测试**：
   - 运行：`pytest backend/kb-service/tests/test_sop_publish_dependencies.py` -> **100% 通过** (5/5)
2. **知识库服务全量测试**：
   - 运行：`pytest backend/kb-service/tests/` -> **100% 通过** (125/125)
3. **排障 Agent 服务全量测试**：
   - 运行：`pytest backend/agent-service/tests/` -> **100% 通过** (465/465)
