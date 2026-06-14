---
status: active
category: solution
audience: engineer
last_updated: 2026-06-15
owner: team
---

# shared/models/__init__.py ORM 注册副作用消除

## 问题背景

**kb-service CrashLoopBackOff** 导致 ArgoCD 应用 `hci-platform-staging` Degraded。

### 根因分析

SQLAlchemy ORM 类定义会在 import 时把表注册到 `Base.metadata`。PR #464 在 kb-service 的 `main.py` 中新增了 `shared.models.dynamic_resource` 导入，触发了 `shared/models/__init__.py` 的执行，进而导入所有子模块（包括 `KBChunk`）。同时 kb-service 内部也定义了 `KBChunk`，两者注册到同一个 `Base.metadata`，导致表重复定义冲突。

### 导入链分析

```
kb-service/app/main.py:45 → from shared.models.dynamic_resource import DynamicResourceRevision
    ↓ 触发 shared/models/__init__.py
shared/models/__init__.py:11 → from .kb import KBChunk  (shared 版本被注册)

同时：
kb-service/app/routes/admin.py:34 → from app.models.document import KBDocument
    ↓ 触发 app/models/__init__.py
app/models/__init__.py:3 → from .chunk import KBChunk  (kb-service 内部版本也被注册)

两个 KBChunk 注册到同一个 Base.metadata → 冲突！
```

### 为什么旧版本没有问题

PR #464 之前，kb-service 的 `main.py` 没有 `shared.models.*` 导入，不会触发 `shared/models/__init__.py` 的执行，KB 相关 ORM 不会被提前注册。

## 方案选型

| 方案 | 优点 | 缺点 |
|------|------|------|
| **方案 A: 延迟导入** | 保持 API 兼容性，只加载需要的模块 | 需要维护 `_MODULE_MAP` 映射表 |
| **方案 B: 移除内部模型** | 统一使用 shared 模型，避免重复定义 | kb-service 需要依赖 shared，耦合增加 |
| **方案 C: 分离 Base** | 每个服务独立 Base，完全隔离 | 外键关系跨服务时会断裂 |

**决策：方案 A（延迟导入）**

第一性原理判断：SQLAlchemy ORM 类定义不是普通类型声明，它会在 import 时把表注册到 Base.metadata。真正的问题不是 kb_chunk 这张表本身，而是 `shared/models/__init__.py` 作为包根导入时带来了不必要的 ORM 注册副作用。

## 实现方案

使用 Python 3.7+ `__getattr__` 实现延迟导入：

```python
# shared/models/__init__.py

__all__ = ["User", "Conversation", ...]

_MODULE_MAP = {
    "User": "user",
    "Conversation": "conversation",
    ...
}

def __getattr__(name: str):
    """延迟导入：只在使用时才加载对应模块"""
    if name not in __all__:
        raise AttributeError(f"module {__name__} has no attribute {name}")

    module_name = _MODULE_MAP.get(name)
    import importlib
    module = importlib.import_module(f".{module_name}", __package__)
    value = getattr(module, name)
    globals()[name] = value  # 缓存避免重复导入
    return value
```

### 用法变化

```python
# 包根导入（只加载对应模块）
from shared.models import Conversation  # 只加载 conversation.py

# 直接导入（同上，推荐）
from shared.models.conversation import Conversation

# 全量导入（慎用）
from shared.models import *  # 触发所有模块加载
```

## 影响范围

| 服务 | 影响 |
|------|------|
| kb-service | 启动时不再与 shared.models.kb 冲突 |
| agent-service | 包根导入 ClaimVerification/ReasoningOutput 正常工作 |
| conversation-service | 直接导入模式不受影响 |
| case-service | 直接导入模式不受影响 |

## 验证结果

```python
# kb-service 场景测试
from app.models import KBChunk  # kb-service 内部
from shared.models.dynamic_resource import DynamicResourceRevision
# kb_chunk 表注册数量: 1 ✅
```

## 不选其他方案的原因

- **方案 B**：kb-service 需要独立演化，不应强依赖 shared 的 KB 模型定义
- **方案 C**：外键关系（如 `KBChunk.document_id → KBDocument.id`）依赖同一个 Base.metadata，分离后会断裂

## 后续改进

- [ ] 考虑为 kb-service 创建独立的 Base（但需要处理外键关系）
- [ ] 文档化其他模块的 ORM 注册副作用风险