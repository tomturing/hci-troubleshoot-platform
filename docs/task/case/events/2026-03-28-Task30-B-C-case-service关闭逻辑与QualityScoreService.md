---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: Task 30-B-C
---

# Task 30-B + 30-C：case-service 关闭逻辑 + QualityScoreService

```
你是一名负责 hci-troubleshoot-platform 后端 case-service 开发的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【前置条件】
Task 30-A（数据库迁移）必须已执行，数据库中存在 case.close_reason 和 assistant_evaluation.composite_score 列。

【任务一：30-B case-service close 逻辑】
文件：backend/case-service/app/routes/cases.py（或 services/case_service.py，根据现有代码结构决定）

在 "关闭工单" 的 API handler 中：
1. 接收 close_reason 参数（枚举值：user_command/timeout/abandon/admin_close）
2. 将 close_reason 写入 case 表
3. close 完成后异步（或同步）调用 QualityScoreService.calculate_and_save(case_id)

【任务二：30-C QualityScoreService】
新建文件：backend/case-service/app/services/quality_score.py

实现 QualityScoreService 类，核心方法 calculate_and_save(case_id)：

算法设计（见 docs/16_评分机制与评价系统.md §3 和 §4）：

BASE_WEIGHTS = {
    "user_rating":   0.30,
    "close_intent":  0.20,
    "efficiency":    0.20,
    "repeat_penalty": 0.15,
    "ai_quality":    0.15,
}

各维度评分：
- close_intent_score：user_command=100, timeout=60, abandon=10, admin_close=50
- efficiency_score：基于 session_duration_sec 和 message_count（见§3 设计）
- repeat_penalty_score：repeat_question_count 越高分越低，公式见§3
- ai_quality_score：kb_hit_count/message_count 比率映射到0-100
- user_rating_score：若有用户评分则 (rating/5)*100，否则该项权重归零后重新归一化

无用户评分时，动态归一化（4维 weights 归一化后重新加权）。

最终写入 assistant_evaluation 表：composite_score、score_breakdown（各维原始分 JSON）、close_reason、calculated_at。

同时暴露 Prometheus Counter/Gauge：
- hci_case_quality_score（Gauge，按 close_reason 分 label）

【约束】
- 使用 uv 管理依赖，不引入新的非必要三方库
- 对数据库写操作使用现有 SQLAlchemy session 模式（参考同目录其他 service 文件）
- 代码注释使用中文
- 不要修改与 Task 30 无关的代码

【验收标准】
- POST /cases/{case_id}/close，DB 中 case.close_reason 有值
- assistant_evaluation 表有对应记录，composite_score 非空
- 单元测试：compute_quality_score(close_reason="user_command", session_duration_sec=300, message_count=5, repeat_question_count=0) → composite_score ∈ [70, 100]
- 单元测试：compute_quality_score(close_reason="abandon", session_duration_sec=120, message_count=20, repeat_question_count=4) → composite_score ∈ [0, 40]
- Prometheus 指标 hci_case_quality_score 存在且有数据

完成后提交 PR，等待 Claude 审核。
```