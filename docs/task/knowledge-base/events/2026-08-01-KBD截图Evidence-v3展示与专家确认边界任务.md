---
status: completed_code_level
category: task
audience: developer, tester
last_updated: 2026-08-01
owner: team
---

# KBD 截图 Evidence v3 展示与专家确认边界任务

## 对应方案

[KBD 截图 Evidence v3 展示与专家确认边界方案](../../../solution/knowledge-base/events/2026-08-01-KBD截图Evidence-v3展示与专家确认边界方案.md)

## 实施清单

| ID | 工作项 | 状态 |
|---|---|---|
| KVE-01 | 正文截图段关联 `images_json` 权威 Evidence；精确匹配允许重复引用，兜底使用稳定 seq 顺序 | ✅ |
| KVE-02 | Evidence v3 卡片展示 FULL_TEXT、Observed Facts、DESCRIPTION/Inferences 与确认状态；历史 v1/v2 仅在实际存在字段时兼容显示 | ✅ |
| KVE-03 | 修正 `inference_needs_review=false` 被 Inferences 非空覆盖的前端误判；`manual_reviewed` 显示成功状态 | ✅ |
| KVE-04 | KBD PATCH 新增 `reviewed_image_seqs`，只确认本次显式审核图片，校验重复和未知 seq，并保留旧调用兼容 | ✅ |
| KVE-05 | 图片来源变更后将 Signal Proposal 标为 stale；前端保存反馈提示重新抽取与复核 | ✅ |
| KVE-06 | 覆盖未确认/已确认 DESCRIPTION、安全正文、单图确认范围和 Signal 输入隔离的回归测试 | ✅ |
| KVE-07 | 执行后端测试、Ruff、Admin UI 生产构建、diff 检查 | ✅ |

## 验收步骤

1. 部署新 kb-service 与 Admin UI 镜像后，打开 KBD `27123`。
2. 展开任务截图和 `ps auxf` 终端截图：应显示 Observed Facts，以及黄色“模型推断·待确认”
   DESCRIPTION；不应再显示空的“失败任务/命令返回/排障建议”。
3. 在维护工作稿中仅编辑并点击“确认并保存修订” `img_2`；刷新后 `img_2` 显示绿色
   “专家已确认”，其他图片仍维持原状态。
4. 验证 Signal Proposal 被标记 stale，执行“重新抽取关键信号”并复核来源引用。
5. 发布维护工作稿前确认 Agent active revision 未变化；发布后再验证新 revision 生效。

## 自动验证

```bash
cd /aihci/hci-troubleshoot-platform
uv run pytest backend/kb-service/tests/test_vision_evidence.py \
  backend/kb-service/tests/test_kbd_sync.py \
  backend/kb-service/tests/test_kbd_expert_maintenance.py \
  backend/kb-service/tests/test_kbd_patch_signals.py -q
uv run ruff check backend/kb-service/app/routes/admin.py \
  backend/kb-service/tests/test_kbd_expert_maintenance.py \
  backend/kb-service/tests/test_kbd_sync.py
cd frontend && corepack pnpm --filter @hci/admin build
```

结果：30 项 Python 测试通过，Ruff 通过，Admin UI `vue-tsc` 与 Vite 生产构建通过。
