---
status: active
category: meta
audience: all
last_updated: 2026-09-23
owner: team
---

# QKV/QFK 信号配置操作指南（迁移阅读） — 阅读入口已整合

本路径仅保留旧链接兼容，不再维护正文或字段示例。

- **当前文档**：[KBD审核与信号配置-使用手册](../../../user-guide/KBD审核与信号配置-使用手册.md)。
- **完整导航**：[KBD 与关键信号文档入口](../../knowledge-base/README.md)。
- **历史原文**：[2026-09-23 整理前快照](../../../archive/kbd-signals-2026-09-23/solution/agent/02-架构设计/QKV_QFK信号配置操作指南.md)，仅供追溯，不作为现行配置依据。

## 变更历史

| 日期 | 变更 |
|---|---|
| 2026-09-23 | 合并重复主题，保留旧路径与章节定位；后续修改当前文档。 |

<!-- 兼容整理前章节定位；实际内容见上方当前文档。 -->
<a id="qkvqfk-信号配置操作指南迁移阅读"></a>
<a id="一概述"></a>
<a id="11-什么是-qkvqfk"></a>
<a id="12-配置入口"></a>
<a id="二qkv-关键词清洗规则"></a>
<a id="21-自动清洗规则"></a>
<a id="22-状态自动检测仅-qkv_task"></a>
<a id="23-关键字须对齐分类基线语义约束"></a>
<a id="24-校验与容忍原则不硬拒"></a>
<a id="三qkv-产出变量配置orchestrateproduces"></a>
<a id="31-produces-字段说明"></a>
<a id="32-各信号类型的默认产出变量"></a>
<a id="qkv_alert"></a>
<a id="qkv_task"></a>
<a id="qkv_dialog"></a>
<a id="33-时间格式说明"></a>
<a id="34-可视化编辑步骤"></a>
<a id="35-配置示例"></a>
<a id="36-变量引用方式v2-写法"></a>
<a id="四qfk-后端信号字段规范acquire--matchproduces"></a>
<a id="41-共有字段"></a>
<a id="42-特有字段"></a>
<a id="qfk_log"></a>
<a id="qfk_system"></a>
<a id="qfk_service"></a>
<a id="qfk_vm--network--storage--hardware--platform"></a>
<a id="43-host-字段特殊处理"></a>
<a id="44-配置示例v2-写法"></a>
<a id="qfk_log-示例"></a>
<a id="qfk_system-示例"></a>
<a id="qfk_service-示例"></a>
<a id="45-产出变量与非-json-行列提取"></a>
<a id="五qfk-判定器配置match"></a>
<a id="51-matcher-类型说明"></a>
<a id="52-匹配模式说明"></a>
<a id="六注意事项"></a>
<a id="61-配置生效条件"></a>
<a id="62-常见错误"></a>
<a id="63-最佳实践"></a>
<a id="七附录"></a>
<a id="71-已注册的-qkv-工具acquiretool-值"></a>
<a id="72-已注册的-qfk-工具acquiretool-值"></a>
<a id="73-变更历史"></a>

请阅读上方当前文档；这里不再重复维护旧章节。
