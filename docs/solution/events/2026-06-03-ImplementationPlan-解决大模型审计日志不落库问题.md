# Implementation Plan - 解决大模型审计日志不落库（SQLAlchemy 外键编译冲突）问题

## Goal Description

经过对当前 staging 环境中 `agent-service` 容器日志与 PostgreSQL 数据库底层结构进行深度诊断，我们定位了大模型审计日志 `audit_log` 完全没有落库的底层致命原因为 **SQLAlchemy 外键编译冲突（`NoReferencedTableError`）**：
1. 共享模型 [audit.py](file:///aihci/hci-troubleshoot-platform/backend/shared/models/audit.py) 中 `AuditLog` 声明了指向 `system_prompt.id` 的外键：
   `system_prompt_id = Column(Integer, ForeignKey("system_prompt.id", ondelete="SET NULL"), nullable=True)`
2. 对应的 `SystemPrompt` ORM 模型类之前私有地定义在 [system_prompt.py](file:///aihci/hci-troubleshoot-platform/backend/conversation-service/app/models/system_prompt.py) 中。
3. `agent-service` 是一个完全独立的微服务，在运行期未加载 `conversation-service` 下的私有模型。这就导致 `agent-service` 运行内存中的 SQLAlchemy `Base.metadata` 根本不存在 `system_prompt` 表结构。
4. 当 `agent-service` 尝试调用 `PromptAuditService.write_prompt_audit` 将大模型 Prompt 写入 `audit_log` 时，SQLAlchemy 对 `AuditLog` 进行元数据编译并检查外键合法性，因找不到引用的 `system_prompt` 表而直接抛出异常。
5. 该异常被 `PromptAuditService` 的异常隔离保护机制（`try...except`）捕获，使得大模型审计记录被**静默丢弃**。

本方案旨在将 `SystemPrompt` 移入 `shared` 共享模型库中，并在 [audit.py](file:///aihci/hci-troubleshoot-platform/backend/shared/models/audit.py) 中显式导入 `SystemPrompt` 以便自动将其表结构注册进 `Base.metadata`，从而根治外键缺失冲突，确保大模型审计日志 100% 正确落库。

---

## User Review Required

> [!IMPORTANT]
> 1. 本次重构涉及跨微服务的公共模型搬移（从 `conversation-service` 下沉到 `shared` 层）。
> 2. 会删除原有的 [system_prompt.py](file:///aihci/hci-troubleshoot-platform/backend/conversation-service/app/models/system_prompt.py)，并将引用指向 `shared` 层，不会对现有数据库 Schema 造成任何变更或破坏。

---

## Open Questions

> [!NOTE]
> 暂无开放性设计疑问。下沉公共模型为微服务架构中的标准演进做法。

---

## Proposed Changes

### 1. Shared Models Module (backend/shared)

#### [NEW] [system_prompt.py](file:///aihci/hci-troubleshoot-platform/backend/shared/models/system_prompt.py)
* **目标**：下沉创建共享的 `SystemPrompt` ORM 模型类。
* **内容**：迁移原私有模型代码，继承 `from ..database.postgres import Base`。

#### [MODIFY] [audit.py](file:///aihci/hci-troubleshoot-platform/backend/shared/models/audit.py)
* **目标**：使 `AuditLog` 编译元数据时能自动关联到 `system_prompt` 的表结构。
* **改动**：在文件顶部显式引入 `SystemPrompt` 依赖：
  ```python
  from .system_prompt import SystemPrompt
  ```

---

### 2. Conversation Service (backend/conversation-service)

#### [DELETE] [system_prompt.py](file:///aihci/hci-troubleshoot-platform/backend/conversation-service/app/models/system_prompt.py)
* **目标**：彻底删除私有的 `SystemPrompt` 声明文件，消除模型二义性。

#### [MODIFY] [__init__.py](file:///aihci/hci-troubleshoot-platform/backend/conversation-service/app/models/__init__.py)
* **目标**：重定向模型导出路径，使其指向共享层。
* **改动**：将 `from .system_prompt import SystemPrompt` 改为：
  ```python
  from shared.models.system_prompt import SystemPrompt
  ```

---

## Verification Plan

### Automated Tests
- 在本地分别运行 `conversation-service` 和 `agent-service` 的单元测试，确保无导入错误和运行时语法报错：
  ```bash
  uv run pytest backend/conversation-service/tests/ -q
  uv run pytest backend/agent-service/tests/ -q
  ```
- 编写一个专项测试用例，模拟 `agent-service` 的 `PromptAuditService` 导入和实例化 `AuditLog` 并触发表元数据编译的过程。
- 运行代码检查门禁：
  ```bash
  make lint
  ```

### Manual Verification
- 提 PR 部署至 Staging 环境后，触发 S0 或 ReAct 对话交互，并在 staging 数据库中执行 SQL 查询：
  ```sql
  select count(*), max(started_at) from audit_log;
  ```
  验证 `audit_log` 记录条数增加，最新审计记录能够 100% 成功生成。
