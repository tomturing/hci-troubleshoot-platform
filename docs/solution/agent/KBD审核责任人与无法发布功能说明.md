# KBD 审核责任人与无法发布功能说明

## 功能概述

本次更新为 KBD 知识条目管理系统新增了两项核心功能：

1. **发布审核责任人管理** - 支持为 KBD 案例分配审核责任人，便于责任追踪和按责任人筛选
2. **无法发布状态** - 支持将不符合发布条件的 KBD 标记为"无法发布"，并记录原因

## 使用指南

### 1. 审核责任人管理

#### 1.1 维护审核责任人列表

在 KBD 管理页面顶部，有一个可展开/收缩的"发布审核责任人"卡片：

- **默认收起**：不占用主视口空间
- **点击展开**：查看和管理所有审核责任人
- **添加责任人**：点击"+ 添加"按钮，填写姓名和邮箱（可选）
- **编辑责任人**：点击"编辑"按钮修改信息
- **删除责任人**：点击"删除"按钮（关联的 KBD 的审核责任人将被清空）

#### 1.2 为 KBD 分配审核责任人

**批量设置**：
1. 在 KBD 列表中勾选多个条目
2. 点击批量操作工具栏中的"设置审核责任人"按钮
3. 在弹出的对话框中选择责任人
4. 点击"确认设置"

**单个设置**：
- 暂不支持单个 KBD 的审核责任人设置（可通过批量操作选择单个条目实现）

#### 1.3 按审核责任人筛选

在页面顶部的筛选栏中：
- 找到"审核责任人"下拉框
- 选择特定的责任人进行筛选
- 或选择"全部"查看所有 KBD

### 2. 无法发布状态

#### 2.1 标记 KBD 为无法发布

**批量标记**：
1. 在 KBD 列表中勾选多个条目
2. 点击批量操作工具栏中的"设为无法发布"按钮
3. 在弹出的对话框中填写原因（必填）
4. 点击"确认"

**单个标记**：
1. 在 KBD 列表中找到目标条目（仅 draft 状态显示此按钮）
2. 点击操作列中的"无法发布"按钮
3. 在弹出的对话框中填写原因（必填）
4. 点击"确认"

#### 2.2 查看无法发布的 KBD

**通过状态筛选**：
- 在筛选栏的"状态"下拉框中选择"无法发布"
- 列表将显示所有标记为无法发布的 KBD

**查看详情**：
- 点击 KBD 的"详情"按钮
- 在详情对话框中可以看到：
  - 状态：无法发布
  - 标记时间
  - 无法发布原因

#### 2.3 恢复无法发布的 KBD

目前不支持直接从"无法发布"状态恢复。如需恢复，请联系系统管理员通过数据库操作。

## 技术实现

### 数据库变更

1. **新增表：kbd_review_owner**
   - id: 主键
   - name: 责任人姓名
   - email: 邮箱（可选，唯一）
   - created_at: 创建时间
   - updated_at: 更新时间

2. **kbd_entry 表新增字段**
   - review_owner_id: 审核责任人 ID（外键）
   - unpublishable_at: 标记为无法发布的时间
   - unpublishable_by: 操作人 ID
   - unpublishable_reason: 无法发布原因

### API 端点

#### 审核责任人管理
- `GET /api/v1/kbd/review-owners` - 列表查询
- `POST /api/v1/kbd/review-owners` - 创建
- `PUT /api/v1/kbd/review-owners/{id}` - 更新
- `DELETE /api/v1/kbd/review-owners/{id}` - 删除

#### 批量操作
- `POST /api/v1/kbd/batch/set-review-owner` - 批量设置审核责任人
- `POST /api/v1/kbd/batch/set-unpublishable` - 批量设为无法发布

#### 单个操作
- `POST /api/v1/kbd/{id}/set-unpublishable` - 单条设为无法发布

#### 列表查询增强
- `GET /api/v1/kbd/pending` 新增参数：
  - `review_owner_id`: 按审核责任人筛选
  - `status`: 支持 `unpublishable` 值

### 前端组件

1. **审核责任人管理卡片**
   - 位置：KBD 管理页面顶部
   - 功能：展开/收缩、增删改查审核责任人
   - 样式：类似批量任务卡片

2. **批量操作工具栏**
   - 新增按钮："设置审核责任人"、"设为无法发布"
   - 对话框：下拉选择责任人、文本域输入原因

3. **筛选器**
   - 新增：审核责任人下拉筛选框
   - 增强：状态筛选支持"无法发布"

4. **列表表格**
   - 新增列：审核责任人
   - 增强：状态标签支持"无法发布"样式

5. **操作列**
   - 新增：draft 状态 KBD 显示"无法发布"按钮

## 测试验证

已创建自动化测试脚本：`test_review_owner_feature.sh`

运行测试：
```bash
chmod +x test_review_owner_feature.sh
./test_review_owner_feature.sh
```

测试覆盖：
- ✅ 审核责任人 CRUD API
- ✅ 批量设置审核责任人 API
- ✅ 单条设为无法发布 API
- ✅ 批量设为无法发布 API
- ✅ 列表查询支持审核责任人筛选
- ✅ 列表查询支持状态筛选（包括 unpublishable）

## 已知限制

1. **无法发布状态不可恢复**：当前版本不支持从"无法发布"状态恢复到其他状态，需要数据库操作
2. **审核责任人无权限控制**：所有管理员都可以管理审核责任人和分配责任人
3. **无审核责任人统计报表**：暂不提供按审核责任人统计 KBD 数量的报表功能

## 后续优化建议

1. 支持从"无法发布"状态恢复到"draft"状态
2. 审核责任人与用户系统集成，支持从用户列表选择
3. 添加审核责任人统计报表
4. 支持审核责任人变更历史记录
5. 添加无法发布原因的标准化选项（下拉选择 + 自定义输入）

## 相关文件

### 后端
- `database/atlas-migrations/20260924000000_add_review_owner_and_unpublishable.sql` - 数据库迁移
- `database/atlas-migrations/20260924000001_update_batch_job_type_constraint.sql` - 约束更新
- `database/desired_schema.sql` - Schema 定义
- `backend/kb-service/app/models/kbd_review_owner.py` - 审核责任人模型
- `backend/kb-service/app/models/kbd_entry.py` - KBD 条目模型（更新）
- `backend/kb-service/app/routes/admin.py` - API 路由
- `backend/api-gateway/app/routes/kb.py` - API 网关路由

### 前端
- `frontend/admin/src/views/KbdReviewView.vue` - KBD 管理页面

### 测试
- `test_review_owner_feature.sh` - 自动化测试脚本

## 访问地址

- **Admin UI**: http://localhost:3002
- **API 文档**: http://localhost:18004/docs

## 联系方式

如有问题或建议，请联系开发团队。
