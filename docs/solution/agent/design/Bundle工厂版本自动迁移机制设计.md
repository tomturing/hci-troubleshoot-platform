# Bundle 工厂版本自动迁移机制设计

## 背景

**问题**：Bundle 工厂代码修复后，已生成的 Bundle 不会自动更新，需要手动重新发布，工作量巨大。

**根本原因**：
1. Bundle 在发布时预编译并缓存
2. 缺少版本跟踪和迁移机制
3. 工厂升级时，过期 Bundle 无法自动更新

## 设计方案

### 1. 版本追踪

**新增数据库表**：`bundle_metadata`

```sql
CREATE TABLE bundle_metadata (
    id SERIAL PRIMARY KEY,
    kbd_id INTEGER NOT NULL,
    support_id VARCHAR(20) NOT NULL,
    bundle_digest VARCHAR(128) NOT NULL,
    factory_version VARCHAR(50) NOT NULL,  -- v4-fixture-assets
    compiler_revision VARCHAR(100),
    compiled_at TIMESTAMP WITH TIME ZONE NOT NULL,
    ...
);
```

**关键信息**：
- `factory_version`：Bundle 使用的工厂版本
- `compiler_revision`：编译器完整修订版本
- `compiled_at`：编译时间

### 2. 自动检测过期 Bundle

```python
def get_outdated_bundles(current_version: str) -> list[BundleMigrationStatus]:
    """获取需要迁移的 Bundle 列表"""
    # 查询 factory_version != current_version 的 Bundle
```

### 3. 批量迁移 API

**新增 API 端点**：

```
GET  /api/v1/bundle-migration/health   # 查询健康状态
POST /api/v1/bundle-migration/migrate  # 批量迁移
GET  /api/v1/bundle-migration/version  # 查询当前版本
```

**使用示例**：

```bash
# 查询需要迁移的 Bundle
curl -X GET http://diagnosis-service:8008/api/v1/bundle-migration/health

# 预览迁移（dry-run）
curl -X POST http://diagnosis-service:8008/api/v1/bundle-migration/migrate \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'

# 执行批量迁移
curl -X POST http://diagnosis-service:8008/api/v1/bundle-migration/migrate \
  -H "Content-Type: application/json" \
  -d '{"dry_run": false}'

# 迁移指定 KBD
curl -X POST http://diagnosis-service:8008/api/v1/bundle-migration/migrate \
  -H "Content-Type: application/json" \
  -d '{"kbd_ids": [756], "dry_run": false}'
```

### 4. 编译时记录版本

**修改 `compile_signal_acquisition`**：

```python
def compile_signal_acquisition(...) -> CompiledSignalAcquisition:
    # ... 编译逻辑 ...
    
    # 记录工厂版本
    metadata = {
        "factory_version": CURRENT_BUNDLE_FACTORY_VERSION,
        "compiler_revision": COMPILER_REVISION,
        "compiled_at": datetime.utcnow(),
    }
    
    # 保存到数据库
    save_bundle_metadata(kbd_id, metadata)
    
    return CompiledSignalAcquisition(...)
```

### 5. 工厂升级流程

**升级步骤**：

1. **修改工厂版本**：
   ```python
   CURRENT_BUNDLE_FACTORY_VERSION = "v5-new-feature"
   ```

2. **检测过期 Bundle**：
   - 系统自动检测 `factory_version != v5-new-feature` 的 Bundle
   - 生成迁移报告

3. **执行迁移**（可选）：
   - **自动模式**：系统启动时自动迁移所有过期 Bundle
   - **手动模式**：通过 API 手动触发迁移

### 6. 配置选项

**环境变量**：

```bash
# 是否启用自动迁移
BUNDLE_AUTO_MIGRATION_ENABLED=true

# 迁移批处理大小
BUNDLE_MIGRATION_BATCH_SIZE=10

# 迁移失败重试次数
BUNDLE_MIGRATION_RETRY_COUNT=3
```

## 实现文件

### 后端

- `backend/diagnosis-service/app/services/bundle_migration.py` - 迁移核心逻辑
- `backend/diagnosis-service/app/routes/migration.py` - API 路由
- `database/data-migrations/035_bundle_factory_version_metadata.sql` - 数据库表

### API

- `GET  /api/v1/bundle-migration/health` - 健康检查
- `POST /api/v1/bundle-migration/migrate` - 批量迁移
- `GET  /api/v1/bundle-migration/version` - 版本查询

### 前端

- `frontend/admin/src/views/BundleFactoryView.vue` - Bundle 工厂页面，包含迁移按钮和对话框
- `backend/api-gateway/app/routes/diagnosis.py` - API 网关代理路由

## 优势

1. **代码修复立即生效**：工厂升级后，可以批量重新编译
2. **无需手动操作**：提供 API 自动化迁移
3. **可追溯**：记录每个 Bundle 的版本历史
4. **灵活性**：支持批量迁移和单个迁移

## 未来改进

1. **渐进式迁移**：按使用频率优先迁移热门 Bundle
2. **健康检查集成**：在系统启动时自动检测并报警
3. **版本回滚**：支持回滚到旧版本的 Bundle
4. **性能优化**：增量迁移，避免全量重编译

## 修复时间

2026-09-09
