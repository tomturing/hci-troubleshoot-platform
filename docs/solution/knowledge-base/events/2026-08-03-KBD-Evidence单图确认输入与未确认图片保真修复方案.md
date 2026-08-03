---
status: completed_code_level
category: solution
audience: developer, reviewer
last_updated: 2026-08-03
owner: team
---

# KBD Evidence 单图确认输入与未确认图片保真修复方案

## 背景与需求

KBD Evidence v3 的单图确认接口已使用 `reviewed_image_seqs` 表达真实审核范围。PR 审查发现
两个边界没有完全收口：Pydantic 的普通 `list[int]` 可把 JSON 布尔值或可转换值解析为整数，
使 `true` 有机会被当作 `seq=1`；同时归一化函数会为所有图片调用 `setdefault`，即使图片
没有被确认，也会写入空的 `evidence/quality` 结构。

目标是在不改变“省略 `reviewed_image_seqs` 兼容旧版整组确认”语义的前提下，保证确认范围
不可被类型宽松解析扩大，且部分确认不修改任何未确认图片的 Evidence 快照。

## 方案（WHAT）

### 1. 严格校验确认 seq

请求模型将 `reviewed_image_seqs` 的元素定义为严格非负整数。服务端拒绝 bool、字符串、
浮点数和负数；已有的重复和不存在 seq 校验继续返回 `422`。因此只有客户端明确传入的图片
稳定 seq 才可触发 `expert_confirmed`。

### 2. 未确认图片完全保真

归一化仍校验每张图片的 `seq`、`section`、`desc` 基础结构，但只有确认范围内的图片才创建
或更新 `evidence.quality` 与 `evidence.provenance`。不在范围内的图片不新增、不删除、不重写
任何 Evidence 键，包含原本不存在 `evidence` 的图片。

若请求已携带 `evidence`、`quality` 或 `provenance`，其值必须为对象；类型错误返回 `422`，
不得以空对象静默覆盖。省略 `reviewed_image_seqs` 时仍按历史兼容规则确认提交的全部图片，
因此这些图片可被规范化为专家已确认状态。

## 决策依据（WHY）

### 为什么不能依赖普通 `int`

Python 的 `bool` 是 `int` 子类，宽松验证会把 `true/false` 解释为 `1/0`。审核范围是影响
Agent 安全文本和后续发布的信任边界，必须以严格类型而非“可转换”类型表达稳定身份。

### 为什么未确认图片不能被补写空 Evidence

`images_json` 是整组 revision 快照。即使空对象不改变当前展示，也会把本次审核范围外的图片
伪造成被规范化过的数据，破坏精确审计、后续 diff 和专家复核的可解释性。遇到异常结构时
fail closed 比静默纠正更能保留来源事实。

## 影响范围

- `backend/kb-service/app/routes/admin.py`：严格 seq 请求类型；仅更新已确认图片；异常 Evidence
  返回 `422`。
- `backend/kb-service/tests/test_kbd_expert_maintenance.py`：补充严格类型、未确认图片无 Evidence
  保真和异常 Evidence 回归。
- [知识库设计](../知识库设计.md)、[知识库任务](../../../task/knowledge-base/知识库任务.md)：记录
  精确确认/保真边界。
- [接口设计](../../接口设计.md)：补充 `PATCH` 输入类型与 `422` 契约。

## 验收标准

- [x] `reviewed_image_seqs` 拒绝 `true`、`"1"`、`1.0` 和负数。
- [x] 仅确认 `seq=1` 时，未确认且无 `evidence` 的 `seq=0` 完全保持原始对象。
- [x] `evidence`、`quality`、`provenance` 类型错误时返回 `422`，不被空对象覆盖。
- [x] 省略 `reviewed_image_seqs` 时继续兼容旧版整组确认语义。
- [x] KBD Evidence 相关 36 项 Python 测试与 Ruff 通过；PR 现有 Admin UI 生产构建结果保持通过。
