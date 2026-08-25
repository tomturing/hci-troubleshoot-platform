# KBD 变量声明与 Synthetic 绑定职责

## 设计结论

变量名不是 hci-sim 的固定白名单。变量只需满足统一格式 `^[A-Z][A-Z0-9_]*$`；变量是否可执行由 KBD 发布契约和 Scenario 绑定共同决定。

职责分层如下：

1. KBD 发布门禁负责变量语法、`produces`/`requires` 声明、Verification Contract 外部变量声明、Producer/Consumer 依赖图、循环依赖和不可达链检查。
2. C1 Resolver 负责把已发布 Signal 编译为路由，并完整传递 Verification Contract。
3. Scenario/Bundle 编译负责为已声明变量绑定确定性 Synthetic 值，校验类型/作用域/参数安全，并保证 argv、sample output 和 Matcher 使用同一变量池。
4. Runtime 只执行已经完成渲染且通过校验的 argv，不负责推断变量定义。

## 绑定规则

- 内置变量（例如 `HOST`、`VM_ID`、`START`）使用既有语义提供器。
- 已发布 Contract 声明但没有内置语义的变量使用确定性 Scenario 值，当前值形如 `SIM-<变量名>-<support_id>`，仅表示 Synthetic 合约数据。
- 未出现在 Producer 或 Verification Contract 声明中的变量仍然阻断编译。
- 使用场景画像时，画像变量是显式绑定；缺少绑定不得回退到隐式默认值。

Bundle Manifest 必须标记 `SYNTHETIC=true`，并保留变量绑定与 Bundle digest，使重复运行和审计可以复现。

## 18906 兼容性

若 Signal `expert_1787040047488_900be1862f6d` 的 `VM_DISK_ID` 已存在于发布后的 `verification_contract.variables`，Bundle 编译器可以为其生成确定性 Synthetic 绑定；若该变量既没有 Producer，也没有外部声明，阻断应在 KBD 发布门禁发生，而不是由编译器把它误报为“变量名不在白名单”。
