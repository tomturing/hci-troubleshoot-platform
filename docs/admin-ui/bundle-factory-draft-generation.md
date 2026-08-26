# Bundle 工厂 KBD Draft 生成约束

Bundle 工厂生成 Draft 前，Gateway 读取 KBD 的 C1 active snapshot。Resolver 必须为已发布 KBD 的每一个 Signal 解析出确定的执行表示：普通采集生成可执行路由，`qkv_vm_console`、`qkv_effect` 等条件型生产者冻结为专用 Intent。除这些受限例外外，只要存在未解析 Signal，就返回 `SYNTHETIC_ROUTE_UNRESOLVED`，不会生成部分覆盖的 Draft。

QKV Producer 的样例 stdout 不写入固定的 `SIM-*` 值，而是写入 `{{VARIABLE}}` 模板。Go Bundle 编译器随后使用同一组 Scenario Variables 渲染 Producer stdout 与 Consumer argv，避免同一变量在两条路由中出现不同值。自定义变量仍须在已发布的 Verification Contract 或 Producer 声明中存在，未声明变量继续 fail closed。

日志 selector 中的 `{{VAR}}` 必须保留到场景变量渲染阶段；Bundle 编译器只在 `-k/--keyword` 参数中对变量值执行正则字面量转义，普通 argv 和 stdout 仍使用原始变量值。编译器同时兼容历史 Bundle 中已转义的 `\{\{VAR\}\}`，避免运行时升级后旧制品失效。

因此，Draft 的可重复性由三项冻结事实共同保证：完整 Signal 路由/Intent 集合、C1 返回的 KBD/Signal/Tool/Policy digest，以及统一变量池渲染后的 Manifest digest。生成算法变化时必须递增 `compiler_revision`（当前为 `bundle-factory-v3`），让 Registry 为同一 KBD 派生新的 Draft，而不是复用旧算法产物。
