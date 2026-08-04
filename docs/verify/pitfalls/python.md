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

## PIT-041：SQLAlchemy 模型重复定义导致 Table already defined 错误

同一个表在多个文件中定义 ORM 类会导致冲突：

```python
# models/kb_category.py
class KbCategory(Base):
    __tablename__ = "kb_category"
    # ...

# routes/classify.py — ❌ 错误：重复定义
class KbCategory(Base):
    __tablename__ = "kb_category"
    # ...
```

**症状：**
- 服务启动时抛出 `InvalidRequestError: Table 'kb_category' is already defined for this MetaData instance`

**修复：**
- 一个表只定义一个 ORM 类
- 其他文件通过 `from app.models import KbCategory` 导入
- 避免在路由文件中重复定义模型

## V-009：Signal 图片输入必须按章节白名单且与 Candidate 溯源闭环

**症状：** KBD 关键信号 Prompt 已将 `root_cause`、`solution` 直接字段置空，却仍生成来自删除配置、
重启服务等方案动作的 Candidate；Candidate 的 `source_section` 看似是 `steps_text`，但 evidence 不在
该段落或标注截图中。

**根因：** `images_json` 是另一条自然语言输入通道。若把所有图片的 OCR、observed facts、fields 或
`context_before/context_after` 无差别拼入 Prompt，solution/root_cause 所属图片会绕过字段级置空；仅把
LLM 自报的 `source_section` 限制为枚举也不能证明其 `source_refs/evidence` 真实可追溯。

**修复：** 在调用 LLM 前按诊断章节白名单过滤图片，未知 section fail closed；图片只传原子观察事实，
不传自由上下文、DESCRIPTION、inferences 或 legacy desc；由同一份过滤结果建立 `source_ref -> facts`
索引，拒绝未进入输入的 source ref、非诊断章节和无法逐字回溯的图片 evidence。

**预防：** 增加反例测试：solution/root_cause 截图含 `rm`/`restart` 文本时，序列化 Prompt 不得含该文本；
Candidate 引用该截图或将方案文字伪标为诊断图 evidence 时必须进入 rejected candidates。不要把“写操作
后置拒绝”误当成输入隔离，它只能防执行，不能防标签泄漏。
