# Bundle 工厂 Draft 编辑：命令参数（argv）可编辑

Bundle 工厂的 draft 编辑弹窗中，每条 route 的命令参数（`route_key.argv`）现在支持在界面内直接编辑，便于修正命令中的错误参数。

## 变更背景

此前 draft 编辑弹窗将每条 route 的命令参数以只读形式展示，无法在工厂界面修正命令中的错误参数（例如 KBD18906 实例中 `-d SIM-vm-disk-id-18906` 这类错误值）。后端 `ReviseDraft` 本就支持按新 `argv` 重新派生 digest，本次在 UI 层放开编辑，无需后端改动。

## 编辑方式

- 每条 route 下新增「命令参数（可编辑，每行一个）」多行文本框，每行对应一个 argv token。
- 编辑实时写回 `route_key.argv`，下方预览框同步展示完整命令。
- 提供「还原」按钮，回退到 `openEditor` 深拷贝时的原始 `argv`（不会污染 store 中的 manifest）。

## 提交行为

- 点击「生成新 Draft」复用 `saveRevision`，将整个 manifest 提交至 `PUT /bundles/:digest/revise`。
- 后端 `ReviseDraft` 按新 `argv` 重新执行 `ComputeBundleDigest`，派生出新的 draft revision（digest 变化即代表命令已变更）。

## 约束

- 仅放开 `argv` 编辑；命令的程序名 / 子命令（`route_key.program`、`route_key.subcommand`）与整体 manifest 结构仍由 KBD capability 权威定义，不在工厂界面手改。
- 单条 route 内的 argv 仍受 fixture 层 `RouteKey` 精确路由与 `DisallowUnknownFields` 校验约束。
