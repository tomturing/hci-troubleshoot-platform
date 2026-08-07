---
status: active
category: frontend
audience: engineer
last_updated: 2026-08-06
owner: team
---

# KBD 审核详情页耗时操作反馈

KBD 条目详情页中的「重新分类」「重新识图」和「重新抽取」都会调用 LLM，完成时间可能较长。为避免管理员在等待期间切换到其他工作后错过结果，三类操作的结果提示必须保留在页面上，直到管理员主动点击关闭。

## 反馈规则

- 成功结果使用 Element Plus Message 的 `duration: 0` 和 `showClose: true`。
- 失败报错使用相同的常驻配置，不能使用默认自动关闭时长。
- 重新识图没有原始图片等异常结果也按常驻提示处理，确保操作结果不会静默消失。
- 确认操作使用的确认框不属于结果反馈提示，不改变其现有交互。

## 实现位置

规则集中在 `frontend/admin/src/views/KbdReviewView.vue` 的三个详情操作处理函数中：

- `handleReclassify`
- `handleReanalyzeImages`
- `handleReextractSignals`

单张图片重新识图沿用相同的常驻提示行为。
