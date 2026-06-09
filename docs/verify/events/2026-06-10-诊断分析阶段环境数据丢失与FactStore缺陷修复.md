# Walkthrough - 诊断分析阶段环境数据丢失与 FactStore 缺陷修复

我们已成功定位并完成了诊断分析阶段环境数据丢失和 FactStore 运行异常的全部修复工作。

## 变更内容

### 1. 通讯请求端 (`conversation-service`)
- **文件**：[conversation_service.py](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/conversation-service/app/services/conversation_service.py)
- **修复**：修改了获取环境上下文数据的触发条件。允许在 `S0` 至 `S4` 诊断阶段均可正确触发环境数据的加载，避免由于诊断状态机的提前更新导致 `env_context` 为空并下发 `None`。

### 2. 事实校验端 (`agent-service`)
- **文件**：[evidence_builder.py](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/services/evidence_builder.py)
- **修复**：在 `check_information_quality` 方法中，当入参 `env_context` 缺失时，新增优先检查 `FactStore` 中是否存有历史环境事实。只有在参数和数据库事实均不存在时，才触发“环境数据为空”的澄清拦截，保证了事实库的 Fallback 兜底逻辑正常运转。

### 3. 数据库结构 (`Database Schema & Migration`)
- **文件**：[desired_schema.sql](file:///mnt/d/aihci/hci-troubleshoot-platform/database/desired_schema.sql)
- **修改**：在 `desired_schema.sql` 尾部追加了 `fact` 表和 `claim_evidence_link` 表的建表语句及相关的索引结构设计。
- **操作**：通过 `kubectl exec` 命令在 K3s 集群的 PostgreSQL 开发数据库上物理应用了建表 DDL，彻底补全了缺失的数据实体表。

---

## 验证结果

### 自动测试
运行了 `agent-service` 的全量可靠性核心单元测试，确保逻辑更改的正确性并保证无任何 Regression：
- `test_reliability_phase2.py`：52 项测试全部通过（耗时 24.43s）
- `test_reliability_phase4.py`：7 项测试全部通过（耗时 9.23s）

### 数据库验证
通过交互式命令行查询数据库 `\dt`，已确认事实持久化所需的表已存在于数据库中：
```
 public | claim_evidence_link  | table | hci_admin
 public | fact                 | table | hci_admin
```
