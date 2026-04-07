---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 33
---

# Task 33：prompt_audit 数据质量看板与告警（P3）

```
你是一名负责 hci-troubleshoot-platform 数据可观测性与质量监控的后端 / 平台工程 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
在 Task 32 完成后，prompt_audit 将成为评分体系中 ai_quality 维度的唯一真实数据源。
为防止未来改动导致 prompt_audit 再次出现大量 NULL / 写入失败，需要一个轻量的数据质量看板与告警机制。

【任务目标】
1. 为 prompt_audit 关键字段设计并实现最小可用的数据质量监控指标
2. 在 Grafana 中提供一个简单面板，用于观察：
   - 每日 prompt_audit 写入条数
   - has_sop / kb_chunks_count / kb_top_score 非空占比
   - messages 采样占比
3. 为以下异常场景提供告警能力（可先以日志/邮件/Slack Webhook 的形式呈现）：
   - 连续 N 分钟 prompt_audit 写入为 0
   - has_sop / kb_chunks_count 非空占比 < 80%
   - messages 非空占比 > 30%（采样率被意外改高）

【实现建议】

Step 1：数据质量统计 SQL / 视图
- 在 docs/02_数据库设计.md 或独立 SQL 文件中补充数据质量统计查询示例，例如：
  - 近 7 天按日汇总：
      SELECT
          date_trunc('day', captured_at) AS day,
          COUNT(*) AS total,
          AVG((has_sop IS NOT NULL)::int) AS has_sop_not_null_ratio,
          AVG((kb_chunks_count IS NOT NULL)::int) AS kb_count_not_null_ratio,
          AVG((messages IS NOT NULL)::int) AS messages_sample_ratio
      FROM prompt_audit
      WHERE captured_at >= NOW() - INTERVAL '7 days'
      GROUP BY 1
      ORDER BY 1;

Step 2：Prometheus 导出
- 复用现有 metrics 方案，在 case-service 或 conversation-service 增加一个周期性任务（如 APScheduler / 后台协程）：
  - 每 60 秒执行一次统计 SQL，将结果写入 Prometheus Gauge：
      - hci_prompt_audit_daily_total
      - hci_prompt_audit_has_sop_not_null_ratio
      - hci_prompt_audit_kb_count_not_null_ratio
      - hci_prompt_audit_messages_sample_ratio
- 确保：
  - 未安装 prometheus_client 时，统计任务自动降级为仅日志输出
  - 统计 SQL 出错不会影响主业务流程

Step 3：Grafana 面板与告警规则
- 在现有 Grafana Dashboard 中新增一个「Prompt Audit 数据质量」分组：
  - 折线图：hci_prompt_audit_daily_total
  - 折线图：*_ratio 指标（0-1 或 0-100）
- 为以下规则配置告警（可按环境调整阈值）：
  - has_sop_not_null_ratio < 0.8 持续 10 分钟
  - kb_count_not_null_ratio < 0.8 持续 10 分钟
  - messages_sample_ratio > 0.3 持续 10 分钟

【约束】
- 不修改 prompt_audit 表结构
- 不在主请求链路中增加阻塞性统计逻辑，所有统计应在后台异步执行
- 指标命名需与现有 hci_* 指标前缀保持一致

【验收标准】
- [ ] 在 Prometheus 中能查询到 hci_prompt_audit_* 指标，且随对话量变化
- [ ] Grafana 看板「Prompt Audit 数据质量」渲染正常，能看出最近 7 天内的写入量与比例
- [ ] 人为停用 conversation-service 一段时间后，告警规则能检测到写入为 0 的异常
- [ ] 将 messages 采样率临时调高到 50% 时，messages_sample_ratio 指标明显上升，并触发预期告警
```

---