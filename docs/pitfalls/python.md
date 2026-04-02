# Python 代码避坑

## PIT-003：SQLAlchemy ORM 懒加载在 async 上下文中报错

在 async 路由中访问关联对象时，必须使用 `selectinload` / `joinedload` 预加载，或在同步 session 中访问。

## PIT-004：Pydantic v2 与 v1 的 validator 写法不兼容

v2 使用 `@field_validator`，v1 使用 `@validator`，混用会静默失效。

## PIT-009：dataclass 默认值使用可变对象

```python
# 错误
@dataclass
class Foo:
    items: list = []  # 所有实例共享同一个列表

# 正确
from dataclasses import field
@dataclass
class Foo:
    items: list = field(default_factory=list)
```

## PIT-040：SQLAlchemy 模型使用保留属性名导致启动失败

SQLAlchemy Declarative Base 类有保留属性（如 `metadata`、`registry`），自定义列名不能与这些属性冲突。

```python
# 错误：metadata 是 Base 类的保留属性，用于存储表元数据
class MyModel(Base):
    metadata = Column(JSONB, ...)  # ❌ 启动时抛出 InvalidRequestError

# 正确：使用不同属性名，通过 Column("列名", ...) 指定数据库列名
class MyModel(Base):
    extra_metadata = Column("metadata", JSONB, ...)  # ✅ 属性名避开保留字，列名不变
```

**症状：**
- 服务启动时抛出 `sqlalchemy.exc.InvalidRequestError: Attribute name 'metadata' is reserved`
- Pod 进入 CrashLoopBackOff

**修复：**
- Python 属性名改为非保留字（如 `entry_metadata`）
- 数据库列名保持不变（无需数据迁移）
- 使用 `Column("原列名", 类型, ...)` 语法
