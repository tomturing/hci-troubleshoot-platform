---
status: completed_code_level
category: task
audience: developer, tester
last_updated: 2026-08-03
owner: team
---

# KBD Evidence 单图确认输入与未确认图片保真修复任务

## 对应方案

[KBD Evidence 单图确认输入与未确认图片保真修复方案](../../../solution/knowledge-base/events/2026-08-03-KBD-Evidence单图确认输入与未确认图片保真修复方案.md)

## 实施清单

| ID | 工作项 | 状态 |
|---|---|---|
| KVEH-01 | 将 `reviewed_image_seqs` 收紧为严格非负整数，拒绝 bool、字符串、浮点数与负数 | ✅ |
| KVEH-02 | 仅在确认范围内创建/更新 `evidence.quality/provenance`，未确认图片完整保真 | ✅ |
| KVEH-03 | Evidence、quality、provenance 类型错误返回 `422`，不静默重置 | ✅ |
| KVEH-04 | 增加输入类型、未确认无 Evidence 和异常 Evidence 的自动回归 | ✅ |
| KVEH-05 | 更新 API/知识库现行文档、事件索引与冷启动首页 | ✅ |

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
uv run python scripts/ci/check_docs_naming.py
BASE_SHA=$(git rev-parse origin/main) HEAD_SHA=$(git rev-parse HEAD) \
  uv run python scripts/ci/check_module_doc_sync.py
```

结果：36 项 Python 测试通过，Ruff、文档命名和 PR 视角模块文档同步检查通过。

## 合入后人工验收

部署 kb-service 与 Admin UI 后，在 KBD27123 的维护工作稿中提交包含未确认图片的
`images_json`：只确认一张图片，刷新后确认目标显示“专家已确认”，其他图片的 Evidence
对象与保存前完全一致；提交布尔或字符串 `reviewed_image_seqs` 时应收到 `422`。
