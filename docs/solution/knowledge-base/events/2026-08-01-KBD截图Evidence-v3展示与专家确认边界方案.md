---
status: completed_code_level
category: solution
audience: developer, reviewer
last_updated: 2026-08-01
owner: team
---

# KBD 截图 Evidence v3 展示与专家确认边界方案

## 背景与需求

KBD27123 的任务截图与终端截图已完成 OCR 和 Evidence IR 生成，`images_json` 中同时保存
`FULL_TEXT`、Observed Facts、DESCRIPTION、Inferences 与质量状态。但 Agent 安全正文
`content_md` 会隔离 `inference_status=unverified` 的 DESCRIPTION，只保留可观察文字。

管理台正文截图卡片仍按旧 v2 的 `KEY/TIPS` 格式渲染：当安全正文不存在
`DESCRIPTION/KEY/TIPS` 时，页面把“失败任务/命令返回/排障建议”显示为“无”，造成
“除可见内容外识别为空”的误导。另有一个写边界问题：单图编辑提交整个 `images_json`
时，服务端会把所有图片一起升级为 `expert_confirmed`，无法表达真实的逐图审核范围。

目标是让管理端完整、诚实地展示 Evidence v3，同时保持未确认模型推断不能进入运行参数
或被 Agent 当作事实；`expert_confirmed` 必须表示一次明确的单图专家确认，而不是发布动作
或整组图片的副作用。

## 方案（WHAT）

### 1. 正文截图卡片以 Evidence v3 为准

正文解析出的截图段通过稳定 `seq` 关联 `images_json` 的权威 Evidence。关联优先按
DESCRIPTION 或可见 OCR 内容精确匹配，允许同一图片在多个正文位置重复引用；仅无法匹配
时才按出现顺序兜底。

关联成功的 Evidence v3 卡片固定展示：

1. `FULL_TEXT` 作为可见内容；
2. `Observed Facts`，标识为可生成关键信号的直接观察事实；
3. DESCRIPTION/Inferences 的语义状态：`expert_confirmed` 显示“专家已确认”，其余显示
   “模型推断·待确认”或“需专家复核”。

未确认推断仍可在管理端供专家审阅，但必须明确标注：它不进入 Agent 文档，也不参与
关键信号运行参数生成。没有 Evidence v3 关联的历史条目才继续使用 v1/v2 的
`DESCRIPTION/KEY/TIPS` 兼容显示；不存在的旧字段不再渲染空行。

### 2. 单图专家确认显式化

KBD 更新请求新增可选 `reviewed_image_seqs`。管理端“确认并保存修订”只提交当前图片的
稳定 seq；服务端仅将该 seq 的 Evidence 规范化为：

```json
{
  "status": "manual_reviewed",
  "needs_review": false,
  "inference_status": "expert_confirmed",
  "inference_needs_review": false,
  "provenance": {"expert_edited": true}
}
```

不在该集合的图片保持原有质量状态。为了兼容旧调用方，省略 `reviewed_image_seqs` 时，
仍按旧语义把提交的整组图片视为已审核；重复或引用不存在 seq 的请求返回 `422`。

图片 Evidence 改动会使已有 `signals_json.generation_metadata.status` 变为 `stale`，因此
专家确认后必须重新抽取并复核 Signal Proposal。保存工作稿不会改变已发布 KBD 的 Agent
active revision；只有显式发布维护工作稿后，确认后的 DESCRIPTION 才成为运行时可用知识。

### 3. Signal 输入边界保持不变

关键信号抽取只读取诊断叙事字段和图片的 OCR 原文、fields、Observed Facts。无论
DESCRIPTION 是 `unverified` 还是 `expert_confirmed`，DESCRIPTION/Inferences 都不直接
进入 Signal LLM：前者避免模型推断成为参数，后者避免已确认的解释文本被二次推导成未声明
的运行参数。若某项信息需要驱动信号，专家应将截图直接可见事实修订到 Observed Facts，或
将已验证的诊断步骤写入 `steps_text`，再重新抽取。

## 决策依据（WHY）

### 为什么管理端显示未确认 DESCRIPTION

隐藏未确认 DESCRIPTION 会让专家误以为识图失败，无法完成审核；将其标为事实又会破坏
Evidence 信任边界。管理端以醒目的待确认状态展示，Agent/Signal 输入仍隔离，兼顾可审阅性
与运行安全。

### 为什么不恢复 KEY/TIPS

当前 Vision Prompt 的正式契约是 `TYPE/BACKGROUND/FULL_TEXT/DESCRIPTION`，不再生产
`KEY/TIPS`。从 FULL_TEXT 猜测旧字段会重新引入不可审计的前端语义推断，并让不同消费端
得到不同结果。历史数据保留兼容，新数据按 Evidence v3 直接展示。

### 为什么使用 reviewed_image_seqs 而不新增单图 PATCH 接口

`images_json` 是同一 KBD revision 的整体 Evidence 快照。通过稳定 seq 声明本次审核范围，
可以在一次乐观锁更新中原子保存整组快照、重建安全 `content_md`、标记 Signal 过期；新建
单图接口会额外引入并发合并与版本一致性问题。seq 是领域稳定身份，不使用页面数组下标。

## 影响范围

- [知识库设计](../知识库设计.md)：补充 Evidence v3 展示、单图确认和 Signal 边界。
- [知识库任务](../../../task/knowledge-base/知识库任务.md)：归档完成任务与验收项。
- [接口设计](../../接口设计.md)：记录 KBD PATCH 的 `reviewed_image_seqs` 契约。
- [文档冷启动入口](../../../README.md)：记录当前代码级里程碑和部署待验状态。
- `frontend/admin/src/views/KbdReviewView.vue`：Evidence v3 卡片、质量标签、稳定关联与单图确认请求。
- `backend/kb-service/app/routes/admin.py`：单图确认范围校验与兼容规范化。

## 验收标准

- [x] KBD27123 的任务/终端截图正文卡片不再显示伪造的空 KEY/TIPS 字段；可见
  Observed Facts 和待确认 DESCRIPTION。
- [x] 相同 `img_0` 在不同正文位置重复出现时，均关联同一份 Evidence。
- [x] 保存 `img_2` 时只将 `img_2` 标记为 `expert_confirmed`，其他未审核图片保持原状态。
- [x] 已确认 DESCRIPTION 被安全正文保留；未确认 DESCRIPTION 仍被 Agent 正文隔离。
- [x] Signal 格式化输入不包含 DESCRIPTION/Inferences，并继续包含 Observed Facts/OCR。
- [x] KBD 后端相关 30 项测试、Ruff、Admin UI TypeScript/Vite 生产构建通过。
- [ ] 构建并部署 kb-service 与 Admin UI 后，在 hci-dev 用 KBD27123 完成人工页面验收。
