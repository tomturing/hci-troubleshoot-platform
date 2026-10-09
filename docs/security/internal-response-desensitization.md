# 内部管理面响应脱敏增强（SRC-2026-5358 修复建议补充）

本文档记录对 SRC-2026-5358《管理面 internal 与离线诊断域多个接口完全无鉴权》报告中两条纵深加固建议的落地。

## 背景

SRC-2026-5358 已将 `/api/internal/*` 全部收敛至 `require_admin`、离线诊断域经网关重签与归属校验（详见 `docs/security/identity-resign.md`）。但报告"修复建议"中另有两条**数据最小化**增强项原本未实施——它们仅影响已授权的平台管理员视图，不构成匿名漏洞，但为彻底闭环报告建议，本次补上。

## 增强一：采集器 `command_template` 不返回模板原文

- 改造点：`CollectorDefinitionResponse`（`backend/diagnosis-service/app/schemas/collector_definition.py`）。
- 行为：`command_template` 响应字段不再返回执行命令原文，改为脱敏占位字符串 `<REDACTED: 执行面敏感配置，由后端按需解析>`；新增 `command_template_redacted: bool = true` 与 `command_template_hash: str`（sha256）供有需要的内部比对。
- 不影响执行：后端采集器执行时直接读取事实源实体（`entity.command_template`），不经此响应序列化；契约校验（validator）仍基于事实源，脱敏仅作用于对外 API 响应。列表与详情接口统一生效。

## 增强二：KBD 内部工单字段不外泄

- 改造点：`analyze_kbd_collection_impact`（`backend/diagnosis-service/app/services/offline_governance_service.py`）的 `kbd` 响应块。
- 行为：移除 `support_id` 与 `title`（内部工单标题），仅保留 `kbd_id`、`category_id`、`status`、`updated_at` 等非敏感元数据。
- 即便调用方为平台管理员（`require_platform_admin`），内部工单信息也不再经 API 响应外泄。

## 验证

- 单元测试 / 集成测试已同步更新断言（脱敏标记为真、哈希非空）。
- 手动回归：以管理员令牌调用 `/api/internal/collectors`、`/api/internal/kbd-collection-impact/{id}`，确认命令模板原文与内部工单字段不再出现于响应。
