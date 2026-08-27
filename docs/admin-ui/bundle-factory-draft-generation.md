# Bundle 工厂 KBD Draft 生成与 QKV 资产库

## 生成链路

Bundle 工厂生成 Draft 前，Gateway 读取 KBD 的 C1 active snapshot。Resolver 必须为已发布 KBD 的每一个 Signal 解析出确定的执行表示：普通采集生成可执行路由，`qkv_vm_console`、`qkv_effect` 等条件型生产者冻结为专用 Intent。除这些受限例外外，只要存在未解析 Signal，就返回 `SYNTHETIC_ROUTE_UNRESOLVED`，不会生成部分覆盖的 Draft。

链路如下：

1. Admin 的 Bundle 工厂提交 `support_id` 到 Gateway；Gateway 生成或透传 `X-Trace-ID`，不接受浏览器传入 KBD revision。
2. Gateway 调用 KB Service C1 `GET /api/kb/hci-sim/capabilities/{support_id}`，只接受 `ready_for_artifact_binding` 的已发布快照。
3. Gateway 将 C1 的 `resolved`、目标 node/container 及 `compiler_revision=bundle-factory-v4-fixture-assets` 发给 hci-sim。
4. hci-sim 先基于 C1 的 Signal/Tool/变量契约构建 synthetic Manifest；这是所有信号的基线行为。
5. 对 `qkv_alert`、`qkv_task`、`qkv_dialog`，编译器以 `signal_type` 和 argv 中的 `-k`/`--keyword` 查询 `fixture.asset_revision` 的已发布实例，再验证实例引用的模板修订仍为已发布状态。命中后以实例 bindings 渲染模板 `stdout_template`，替换该 Route 的 stdout。
6. 未命中资产、模板失效或渲染变量不完整时，保留 C1 的已冻结 `sample_output`。这不是静默成功：Runtime 日志记录 `fixture_asset_resolution_failed` 及调用链；非 QKV 信号始终走原有 C1 路径。
7. Registry 将 KBD/Signal/Tool/Policy 依赖，以及实际命中的 instance/template 的 `asset_key@revision`、digest 作为 dependencies 和 route source 写入 `fixture.bundle.compile_input`。因此后续编辑、发布、退役资产不会改写既有 Draft。

`fixture.asset_revision` 是最小长期存储：一个不可变修订表而非新领域。每行都有资产键、类型、信号、修订、状态、内容、模板引用、分类基线快照、Catalog 基线快照、内容摘要、操作者和唯一调用链。资产编辑只会创建 `revision+1` 草稿；发布新修订会退役同一资产键的旧发布修订。该表不依赖已误提交且已废弃的 `fixture.bundle_template`。

## QKV 模板与实例

当前迁移 `000006_fixture_asset_revision.sql` 恢复并长期保存以下 3 个模板与 3 个实例：

| 信号 | 模板 | 实例 | stdout 规则 |
| --- | --- | --- | --- |
| `qkv_alert` | `qkv_alert.template` | `qkv_alert.instance.sample` | JSON `data[]`，使用告警类型、对象、时间和紧急程度 bindings。 |
| `qkv_task` | `qkv_task.template` | `qkv_task.instance.sample` | JSON `data[]`，本次检索 keyword 覆盖 `KEYWORD`，其余任务失败事实来自实例。 |
| `qkv_dialog` | `qkv_dialog.template` | `qkv_dialog.instance.sample` | `/sf/log/<D>/vt/sfvt_vtpdaemon.log` 双日志行；`END_MS` 为微秒日志时间，`END` 为同一时刻的秒内毫秒时钟。错误码链按真实 warning 记录保存，且不伪造 request_id。 |

所有种子均记录分类基线 `category_baseline.yaml@1.0` 和对应 Catalog 基线 checksum。它们是样例资产而不是“全量 QKV 真相”；关键词不匹配时允许 C1 回退，避免把样例错误套用到所有 KBD。

## 管理入口

`/simulation/bundle-factory/assets` 是 Bundle Factory 的资产管理子页，可筛选模板/实例及状态、查看基线和调用链、创建新修订、发布草稿修订。资产内容和两类基线均以 JSON 编辑；实例必须指向同信号的模板修订。浏览器只访问 Gateway，Gateway 以固定专家/发布者身份调用 hci-sim 控制面。

QKV Producer 的样例 stdout 不写入固定的 `SIM-*` 值，而是写入 `{{VARIABLE}}` 模板。Go Bundle 编译器随后使用同一组 Scenario Variables 渲染 Producer stdout 与 Consumer argv，避免同一变量在两条路由中出现不同值。自定义变量仍须在已发布的 Verification Contract 或 Producer 声明中存在，未声明变量继续 fail closed。

日志 selector 中的 `{{VAR}}` 必须保留到场景变量渲染阶段；Bundle 编译器只在 `-k/--keyword` 参数中对变量值执行正则字面量转义，普通 argv 和 stdout 仍使用原始变量值。编译器同时兼容历史 Bundle 中已转义的 `\{\{VAR\}\}`，避免运行时升级后旧制品失效。

因此，Draft 的可重复性由四项冻结事实共同保证：完整 Signal 路由/Intent 集合、C1 返回的 KBD/Signal/Tool/Policy digest、实际使用的模板/实例修订及摘要，以及统一变量池渲染后的 Manifest digest。生成算法变化时必须递增 `compiler_revision`（当前为 `bundle-factory-v4-fixture-assets`），让 Registry 为同一 KBD 派生新的 Draft，而不是复用旧算法产物。
