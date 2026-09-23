---
status: active
category: meta
audience: all
last_updated: 2026-09-23
owner: team
---

# 关键信号（signals_json）数据模型分层重构 RFC — 阅读入口已整合

本路径仅保留旧链接兼容，不再维护正文或字段示例。

- **当前文档**：[关键信号架构设计](../../knowledge-base/关键信号架构设计.md)。
- **完整导航**：[KBD 与关键信号文档入口](../../knowledge-base/README.md)。
- **历史原文**：[2026-09-23 整理前快照](../../../archive/kbd-signals-2026-09-23/solution/agent/02-架构设计/关键信号数据模型分层重构RFC.md)，仅供追溯，不作为现行配置依据。

## 变更历史

| 日期 | 变更 |
|---|---|
| 2026-09-23 | 合并重复主题，保留旧路径与章节定位；后续修改当前文档。 |

<!-- 兼容整理前章节定位；实际内容见上方当前文档。 -->
<a id="关键信号signals_json数据模型分层重构-rfc"></a>
<a id="0-摘要tldr"></a>
<a id="1-背景与问题陈述"></a>
<a id="11-事故溯源来自事件文档"></a>
<a id="12-当前存储态真实库内样例已去除敏感信息"></a>
<a id="13-字段膨胀事实基于代码读取证据"></a>
<a id="2-第一性原理分析"></a>
<a id="21-单一事实来源ssot"></a>
<a id="22-单一职责--有界上下文"></a>
<a id="23-命名即契约"></a>
<a id="24-缺少-schema-演进治理演化债"></a>
<a id="25-读写模型错位cqrs-视角"></a>
<a id="26-与现有架构原则的关系重要一致性声明"></a>
<a id="27-字段消费实情审计expected--match_mode--risk--extraction_method"></a>
<a id="271-expected--match_modeqfk被消费但命名错位"></a>
<a id="272-risk只写不读被-require_human_confirm-取代的冗余闸门"></a>
<a id="273-extraction_method只写不读期待未来而未至的-provenance"></a>
<a id="274-审计小结"></a>
<a id="3-业界最佳实践与范式对照"></a>
<a id="31-ddd实体与值对象"></a>
<a id="32-cqrs--读写模型统一"></a>
<a id="33-配置即数据12-factor--schema-版本化"></a>
<a id="34-监控探针领域的定型分离范式"></a>
<a id="35-数据迁移模式"></a>
<a id="4-目标数据模型嵌套分层"></a>
<a id="41-统一结构"></a>
<a id="42-字段映射表扁平--嵌套"></a>
<a id="43-与-keysignal-基类的关系"></a>
<a id="44-统一-acquireargs-契约producerconsumer-同构"></a>
<a id="441-第一性原理为什么必须-producerconsumer-同构"></a>
<a id="442-工业范式工具参数-schema-注册表registry"></a>
<a id="443-结构设计公共参数一次定义--各工具单独注册"></a>
<a id="444-语义消歧彻底解决同名重载"></a>
<a id="445-consumer-同构backendsignal-由-acquireargs--match-重建"></a>
<a id="446-收益第一性原理--范式"></a>
<a id="447-代码落地已搭建骨架"></a>
<a id="5-影响范围与消费者全景基于架构文档调研"></a>
<a id="6-不变量与契约保存时强制"></a>
<a id="61-json-schema-契约与-ci-校验开放问题③已决议是"></a>
<a id="7-迁移策略直接切-v2-列形态--边界归一已决议"></a>
<a id="核心思路"></a>
<a id="部署与迁移顺序关键"></a>
<a id="落地状态截至-2026-07-24"></a>
<a id="8-风险与权衡"></a>
<a id="9-与增量缺陷修复的关系至关重要"></a>
<a id="10-开放问题待评审决议"></a>
<a id="11-实施里程碑建议"></a>
<a id="12-参考文档"></a>

请阅读上方当前文档；这里不再重复维护旧章节。
