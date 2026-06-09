# Checklist - 诊断分析阶段环境数据丢失与 FactStore 缺陷修复

- [x] [conversation-service] 修改 `conversation_service.py` 扩展环境上下文的加载阶段
- [x] [agent-service] 修改 `evidence_builder.py` 优化 `check_information_quality` 校验，使其支持 `FactStore` 兜底
- [x] [Database Schema] 在 `desired_schema.sql` 尾部追加 `fact` 和 `claim_evidence_link` 表定义
- [x] [Database Migration] 在开发环境应用 DDL 迁移并验证表已存在
- [x] [Verification] 运行单元测试验证代码逻辑正确且无回归
