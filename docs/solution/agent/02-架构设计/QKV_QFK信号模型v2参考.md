---
status: active
category: meta
audience: all
last_updated: 2026-09-23
owner: team
---

# QKV/QFK 信号模型 v2 参考（迁移阅读） — 阅读入口已整合

本路径仅保留旧链接兼容，不再维护正文或字段示例。

- **当前文档**：[关键信号架构设计](../../knowledge-base/关键信号架构设计.md)。
- **完整导航**：[KBD 与关键信号文档入口](../../knowledge-base/README.md)。
- **历史原文**：[2026-09-23 整理前快照](../../../archive/kbd-signals-2026-09-23/solution/agent/02-架构设计/QKV_QFK信号模型v2参考.md)，仅供追溯，不作为现行配置依据。

## 变更历史

| 日期 | 变更 |
|---|---|
| 2026-09-23 | 合并重复主题，保留旧路径与章节定位；后续修改当前文档。 |

<!-- 兼容整理前章节定位；实际内容见上方当前文档。 -->
<a id="qkvqfk-信号模型-v2-参考迁移阅读"></a>
<a id="变更历史"></a>
<a id="1-模型总览"></a>
<a id="2-逐行注释的完整示例"></a>
<a id="21-qkv-信号前端带-produces-产出变量"></a>
<a id="22-qfk-信号后端带-match-判定--依赖变量"></a>
<a id="3-字段要点表"></a>
<a id="31-顶层"></a>
<a id="32-signal数组元素"></a>
<a id="33-acquire采集段必填"></a>
<a id="34-match判定段"></a>
<a id="35-orchestrate编排段"></a>
<a id="text-extract"></a>
<a id="36-provenance来源段"></a>
<a id="37-review复核段"></a>
<a id="4-acquiretool-枚举与-args-字段"></a>
<a id="41-枚举共-12-个"></a>
<a id="42-各-tool-的-args-字段表"></a>
<a id="5-判定器-match-说明"></a>
<a id="51-字段含义"></a>
<a id="52-mode多词逻辑"></a>
<a id="53-match-字段范围已修复"></a>
<a id="6-关键字语义消歧极易配错"></a>
<a id="7-校验与常见错误"></a>

请阅读上方当前文档；这里不再重复维护旧章节。
