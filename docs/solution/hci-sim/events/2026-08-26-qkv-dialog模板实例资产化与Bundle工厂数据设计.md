---
status: superseded
category: solution
audience: product, architect, developer, qa, sre
last_updated: 2026-08-26
owner: team
---

# qkv_dialog 模板/实例资产化与 Bundle 工厂数据设计

> **状态说明（2026-08-26）**：本文原先提出“新增 `fixture_library` 领域及 6 张表”，该方案是需求完整态的长期选项，**不是当前实施结论，现已被取代**。当前阶段必须以配套的[《hci-sim 数据库最小化设计审查》](2026-08-26-hci-sim数据库最小化设计审查.md)为准：先保留运行闭环，收缩冗余状态，暂不新增资产领域。只有达到跨 KBD 复用、独立查询/审批和 stale 传播等触发条件后，才评估渐进增加资产表。

## 1. 结论

当前 `hci_sim` 数据库设计**暂不新增领域或 6 张资产表**。已有的独立数据库、不可变 Bundle、依赖、审批、审计和激活指针作为基础复用；模板/实例先以 Bundle 冻结输入中的摘要保存。下文的 `fixture_library` 仅描述未来需求达到阈值后的完整态选项。

完整态长期选项才需要新增一个面向“Fixture 资产库”的领域（建议 schema 名称：`fixture_library`），至少增加：

1. 模板逻辑实体及不可变模板修订；
2. 实例逻辑实体及不可变实例修订；
3. 分类基线和 Catalog 基线的不可变快照；
4. 模板/实例修订与两类基线快照的绑定；
5. Bundle 与模板/实例修订的依赖关系和审计事件。

不建议把这些数据直接塞入 `fixture.bundle.compile_input`。`compile_input` 是某次 Bundle 编译的冻结输入，适合重放和幂等判断，不适合作为可搜索、可编辑、可审批的长期资产主数据。

## 2. 第一性原理拆解

需求的不可再分事实不是“页面多一个编辑器”，而是以下四个持久化不变量：

| 不变量 | 必须回答的问题 |
|---|---|
| 可复用 | 同一个 qkv_dialog 模板能否被多个 KBD/实例引用？ |
| 可追溯 | 某个 Bundle 的 stdout 是由哪个模板、哪个实例、哪个修订生成的？ |
| 可重放 | 分类基线或 Catalog 后续变化后，历史 Bundle 是否仍能按原规则重建？ |
| 可治理 | 编辑、审核、发布、退役是否有不可变版本、责任人和调用链？ |

因此，模板/实例必须是独立资产；Bundle 只是这些资产与 KBD、Tool、Policy、Artifact 绑定后的编译产物。

## 3. qkv_dialog 模板事实基线

### 3.1 采集与精确检索是两层逻辑

`qkv_dialog` 当前负责粗定位：

```text
acli log get -k <keyword> -p /sf/log/today -c 2
acli log get -k <keyword> -p /sf/log/today/vt -c 2
```

它从日志行提取 `END`、`REQUEST_ID/trace` 和 `HOST`。后续 `qfk_log` 才使用绝对时间、日志文件和路径精确检索。

`today` 不是历史目录常量。对于白盒 `sfvt_vtpdaemon.log`，真实推导为：

```text
END_MS
  -> 转换为 HCI 本地绝对时间
  -> 取月内日号 D
  -> /sf/log/D/vt/sfvt_vtpdaemon.log
```

例如 `2026-08-26 09:45:19.991807` 对应：

```text
/sf/log/26/vt/sfvt_vtpdaemon.log
```

文件名必须为 `sfvt_vtpdaemon.log`，不能写成 `sfvt_vtlog.log`。

### 3.2 `END` 与 `END_MS` 的边界

当前标准变量池定义的是 `END`，qfk_log 的 `-t` 接受秒级绝对时间：

```text
YYYY-MM-DD
YYYY-MM-DD HH
YYYY-MM-DD HH:MM:SS
```

因此模板中可以保留日志原文的微秒时间，但正式变量契约应明确：

```text
END_MS = 日志原始时间，例如 2026-08-26 09:45:19.991807
END    = 传递给 qfk_log -t 的归一化时间，例如 2026-08-26 09:45:19
```

在 `END_MS` 的转换规则落地前，不得把 `END_MS` 直接传给 qfk_log，也不能把 Unix 毫秒数伪装成 `-t` 的合法值。

### 3.3 推荐模板形态

qkv_dialog stdout 应模拟真实 `sfvt_vtpdaemon.log` 的业务日志行，而不是生成 qkv_alert/qkv_task 风格的 JSON：

```text
{{LOG_PATH}}:{{EVENT_TIME_US}} err [sfvt_vtpdaemon] {{EVENT_CLOCK_MS}} E {{PID}} QemuServer.pm(VTP::QemuServer::vm_start_error_deal):12936 | [{{TRACE_ROOT}}:{{TRACE_SPAN}}:{{TRACE_SEGMENT}}] [my_die_with_errcode {{ERRCODE}}] message: {{KEYWORD}}（{{VM_NAME}}）失败，错误信息：{{ERROR_MESSAGE}}
{{LOG_PATH}}:{{CONTEXT_TIME_US}} warning [sfvt_vtpdaemon] {{CONTEXT_CLOCK_MS}} W {{PID}} OpLog.pm((eval)):586 | [{{TRACE_ROOT}}:{{TRACE_SPAN}}:{{TRACE_SEGMENT}}] Errcode tracing: {{ERRCODE_TRACE}}, message: {{KEYWORD}}（{{VM_NAME}}）失败，错误信息：{{ERROR_MESSAGE}}
```

真实实例示例：

```text
/sf/log/26/vt/sfvt_vtpdaemon.log:2026-08-26 09:45:19.991807 err [sfvt_vtpdaemon] 09:45:19.991 E 6955 QemuServer.pm(VTP::QemuServer::vm_start_error_deal):12936 | [a8e4524c9151ac0956995f05d1289081:d41339:45e4a7] [my_die_with_errcode 0x0100186F] message: 启动虚拟机（Rocky-IMG）失败，错误信息：虚拟机镜像忙，正在执行其他操作！
/sf/log/26/vt/sfvt_vtpdaemon.log:2026-08-26 09:45:20.330764 warning [sfvt_vtpdaemon] 09:45:20.330 W 6955 OpLog.pm((eval)):586 | [a8e4524c9151ac0956995f05d1289081:d41339:231e62] Errcode tracing: 0x0100186F/0x010015BE/0x010015BE/0x01002D46, message: 启动虚拟机（Rocky-IMG）失败，错误信息：虚拟机镜像忙，正在执行其他操作！
```

模板不得凭空追加 `request_id:`。样例中的复合 trace 目前不等价于解析器稳定支持的 `request_id=`/`trace_id=`；是否将第一段 trace 映射为 `REQUEST_ID`，必须由日志语义和实机样本确认。

## 4. 现有 hci_sim 设计覆盖度

### 4.1 可以复用的部分

当前 `hci_sim` 已具备：

- `control_plane.scenario`：KBD revision、variant、input fingerprint 和生命周期；
- `fixture.bundle`：不可变 Bundle 元数据、对象 URI、digest、编译输入和状态；
- `fixture.dependency`：Bundle 对 KBD/Tool/Policy 等修订的反向失效依赖；
- `fixture.approval`、`audit.entity_event`：审批和调用链审计；
- `fixture.bundle_activation`：已发布 Bundle 的 active/previous 指针；
- `artifact.*`：真实 Artifact 的扫描、审批和元数据。

这些表可以继续作为 Bundle 发布和 Runtime 消费的控制面，不需要另建一套 hci-sim 数据库。

### 4.2 无法满足的部分

现有设计缺少以下语义：

1. `fixture.bundle.compile_input` 没有模板/实例的独立身份、版本列表、搜索字段和编辑状态；
2. `fixture.dependency` 只有 Bundle 反向依赖，不能表达“某个模板修订被哪些实例使用”；
3. `fixture.provenance` 面向 route→artifact，不能准确表达 route→template/instance；
4. 分类基线和 Resolution Catalog 当前主要来自主库/文件热加载，历史 Bundle 没有统一的不可变内容快照；
5. Bundle Factory 当前能编辑 Bundle manifest，但不能管理可复用模板、实例和它们的审核状态；
6. 若直接更新 `compile_input` 或 manifest，会破坏“编辑产生新不可变修订”的控制面原则。

## 5. 建议的数据领域和表

### 5.1 新增 schema：`fixture_library`

该 schema 属于 hci-sim 控制面，保存可复用的仿真资产，不保存客户原始日志或 Lease 密钥。建议表如下：

| 表 | 作用 | 关键字段 |
|---|---|---|
| `fixture_library.template` | 模板逻辑实体 | `id`、`template_key`、`signal_type`、`status`、`created_by` |
| `fixture_library.template_revision` | 不可变模板内容 | `template_id`、`revision`、`content_json`、`template_digest`、`parser_contract`、`category_snapshot_id`、`catalog_snapshot_id`、`parent_revision_id` |
| `fixture_library.instance` | 实例逻辑实体 | `id`、`instance_key`、`template_id`、`support_id`、`title`、`status` |
| `fixture_library.instance_revision` | 不可变实例/渲染结果 | `instance_id`、`revision`、`template_revision_id`、`stdout`、`stderr`、`bindings_json`、`instance_digest`、`validation_json`、基线快照引用 |
| `fixture_library.baseline_snapshot` | 分类/Catalog 的不可变快照 | `id`、`baseline_type`、`source_name`、`source_revision`、`checksum`、`content_json`、`captured_at`、`trace_id` |
| `fixture_library.revision_dependency` | 资产之间的反向依赖 | `owner_type`、`owner_revision_id`、`dependency_type`、`dependency_id`、`revision`、`digest` |

推荐状态：

```text
draft -> review_pending -> approved -> active -> retired
```

实际对外生效的模板/实例应始终引用不可变 revision；“编辑”只能复制出新 revision，不能覆盖旧内容。

### 5.2 基线关联方式

模板和实例修订都必须绑定：

```text
category_baseline_snapshot_id
catalog_baseline_snapshot_id
```

快照中同时保存来源名称、来源 revision、checksum 和规范化后的内容。原因是：仅保存文件名或当前 active 指针无法重放历史编译；仅保存 checksum 又无法在原始文件变化后解释差异。

跨 `hci_troubleshoot` 与 `hci_sim` 数据库仍不建立外键。快照由控制面在创建资产修订时复制并冻结，主库只提供事实来源；hci-sim 保存 `source_revision/source_checksum` 作为跨库可追溯键。

### 5.3 Bundle 关联方式

编译 Bundle 时：

1. `fixture.bundle.compile_input` 保存模板/实例 revision ID 和 digest，作为完整冻结输入；
2. `fixture.dependency` 增加 `dependency_type=template_revision|instance_revision|baseline_snapshot`；
3. 每个 Route 的来源应记录精确 `instance_revision_id`，不能继续使用 Artifact 笛卡尔积代替 route provenance；
4. 任何资产修订或基线快照被退役/替换时，由 dependency/outbox 将依赖 Bundle 标记为 stale。

## 6. Bundle 工厂页面演进

现有 `/admin/simulation/bundle-factory` 保留为 Bundle 总览和发布页面，新增子页面：

```text
/admin/simulation/bundle-factory/templates
/admin/simulation/bundle-factory/instances
```

### 模板页面

- 按 `signal_type=qkv_alert/qkv_task/qkv_dialog` 筛选；
- 编辑模板正文、变量 schema、解析契约、适用日志文件和关键字策略；
- 展示关联分类基线/Catalog 基线的 revision、checksum；
- 查看历史 revision、差异、引用它的实例和 Bundle；
- 通过校验后提交 `review_pending`，禁止直接改 active revision。

### 实例页面

- 选择模板 revision 和 KBD 分类基线；
- 编辑关键字绑定、时间绑定、路径推导、stdout/stderr、exit code、fault；
- 对 qkv_dialog 强制校验 `sfvt_vtpdaemon.log`、`/sf/log/<D>/vt` 推导和 `END_MS -> END` 归一化；
- 展示“模板文本”和“渲染后的实例 stdout”两栏，避免把实例误当成模板；
- 生成新 Bundle Draft 时显示将冻结的模板/实例 revision、基线 checksum 和 dependency digest。

页面只调用 API，不直接访问 hci_sim 数据库。Gateway 注入唯一 `X-Trace-ID`、actor 和角色；后端记录编辑、校验、审批、编译和发布的完整调用链。

## 7. 对抗性审查

| 失败模式 | 为什么会失败 | 必须的防线 |
|---|---|---|
| 把 stdout 样例直接塞进 Bundle | 失去模板复用和来源追踪 | 模板/实例 revision 独立持久化，Bundle 只引用 digest |
| 只存 `today` 路径 | 历史 END 无法定位真实白盒目录 | 按 `END_MS` 推导 `/sf/log/<D>/vt`，并保存推导规则版本 |
| 模板写错 `sfvt_vtlog.log` | qfk_log Catalog 无法解析或命中错误文件 | 文件 basename 白名单与 Catalog checksum 校验 |
| 只保存 Catalog 当前版本 | Catalog 热加载后历史 Bundle 漂移 | 创建资产修订时冻结 baseline snapshot |
| 直接编辑 active 模板 | 已发布 Bundle 无法重放，审批被绕过 | active revision 只读，编辑生成新 revision |
| 把复合 trace 当 REQUEST_ID | parser 可能产出错误调用链，qfk_log 精确检索失真 | 只按实机确认的 trace 语义映射；不臆造 `request_id` |
| 用 `compile_input` 兼做资产库 | JSON 可存但无法高效查询、引用和审批 | 新增 `fixture_library` 领域和规范化索引 |
| 资产被删除后 Bundle 仍声称可复现 | 历史 Bundle 依赖断裂 | 只允许 retire，不物理删除；Bundle dependency 保留 digest |

## 8. 分阶段落地建议

### Phase 1：资产库最小闭环

- 新增 `fixture_library` schema 和六类表；
- 录入 qkv_alert、qkv_task、qkv_dialog 的首批模板和实例；
- 建立分类/Catalog snapshot；
- 提供只读列表、详情、revision diff API；
- Bundle 编译输入开始记录 asset revision digest。

### Phase 2：Bundle 工厂子页面

- 模板/实例编辑器、校验、复制 revision；
- qkv_dialog 专项校验：真实文件名、月内日号路径、END_MS 归一化、trace 语义；
- 增加模板/实例引用关系和 stale 影响面预览。

### Phase 3：治理与运行闭环

- 接入企业身份源，替换配置化 actor；
- 专家/安全双审和发布 outbox；
- 资产变更自动标记 Bundle stale；
- Bundle 发布前执行真实 Catalog/分类快照一致性和 Route provenance 完整性检查。

## 9. 最终判断

当前 `hci_sim` 数据库不是推倒重来，而是**控制面基础已具备、资产库领域缺失**：

```text
现有 control_plane/fixture/artifact/audit
    = Bundle 生命周期、Artifact、运行和审计

新增 fixture_library
    = 模板、实例、基线快照、资产版本和引用关系
```

在新增领域和表之前，不应把 Bundle 工厂子页面做成直接编辑 `compile_input` 的 JSON 页面；那只能保存当前一次 Draft，不能满足长期积累模板、实例、分类基线、Catalog 基线和正确性回归的目标。
