---
title: hci-sim 多 Bundle 发布闭环修复
status: implemented
category: solution
audience: sre, qa, architect
last_updated: 2026-08-16
owner: team
---

# hci-sim 多 Bundle 发布闭环修复

## 1. 问题

KBD23821 的 fixture 修复合并后，ArgoCD 已同步 Git revision，但运行时仍使用旧 digest `sha256:4d97d28c...`。原因是 Helm 只渲染 KBD27123，KBD23821 文件是未进入 Kubernetes 对象的 chart 资产；运行环境又通过手工 `hostPath` 二进制从数据库加载旧 Bundle，形成 Git、数据库和进程内存三份权威状态。

调用链 `46a32903268ed78b6a5eb4142af4434c` 证明 Agent 已生成正确的 `info\ block\-jobs`，但 exec `ff2490cf-d43d-5368-a028-a86cac0f7a81` 返回 127；运行时 capability 同时证明 KBD23821 仍绑定旧 digest。

## 2. 设计约束

1. 已发布 manifest 与 digest 必须经过 PR 审查，Runtime 不接受编辑态或任意 URL。
2. 同一 `support_id` 在单次发布集合中只能有一个 Bundle，禁止按加载顺序覆盖。
3. 部署声明与进程实际加载集合必须完全一致，缺失、夹带、损坏均失败关闭。
4. SSH 租约必须同时绑定 `support_id`、Bundle digest、KBD revision、Tool revision 和 Policy revision。
5. Bundle 内容变化必须改变 Pod template，不能依赖人工重启或 ConfigMap 最终一致更新。
6. Runtime 开放 HTTP/SSH 前必须将已加载集合原子同步到 Registry；同步失败时不得进入 ready。

## 3. 实现

- Helm `fixture.manifestFiles` 明确列出 KBD27123 与 KBD23821 的正式 published manifest。
- ConfigMap 渲染全部 manifest，Deployment 注入由清单自动生成的 `HCI_SIM_REQUIRED_BUNDLES`。
- Pod template 使用 `checksum/fixture-bundles`，内容变化自动滚动更新。
- Runtime 启动时构造只读 `BundlePool`，对全部 manifest 执行 schema、digest、重复 support_id 与必需集合校验。
- HTTP capability/build 和 SSH 数据面均按 `support_id` 精确选择 Router，不提供默认回退。
- Runtime 启动时以唯一 `trace_id` 原子激活当前 digest、将同 support ID 的旧 published Bundle 及其 Scenario 标为 stale，并写入发布/失效审计；Registry 不可用或身份冲突时启动失败。
- 能力矩阵只读取 `scenario.status=published` 且 `bundle.status=published` 的最新记录，避免旧 revision 或 stale Bundle 被误报为可运行。
- CI 同时校验正式 KBD23821 digest、Helm 渲染后的两个 digest、必需集合环境变量和 rollout checksum。

## 4. 对抗性门禁

- 两个文件声明同一 `support_id` 时启动失败。
- `HCI_SIM_REQUIRED_BUNDLES` digest 错误、缺项或多项时启动失败。
- HMAC 合法但交叉绑定其他 KBD digest 的租约在 SSH 握手阶段拒绝。
- 未加载的 KBD capability 返回 `kbd_not_loaded`，不能回退到默认 fixture。
- 同一 digest 绑定其他 support ID、revision、variant 或指纹时，Registry 同步整体回滚。
- 重复同步同一发布集合保持幂等，不重复写发布审计。

## 5. 部署注意事项

当前 dev 环境存在已删除宿主文件对应的 `hostPath` 二进制覆盖。正式候选镜像已由受控 CI 发布为 `sha256:6b608c44c80d6af70f8c625e2ecde1ba5b2e434d26eccc3642e61f9f1283e215`；部署新版本时必须由 ArgoCD 移除 `binary-override` volume/volumeMount，再验证 `/status` 和 `/v1/simulations/capabilities/23821` 返回 `sha256:c1976465fa8b1fe5226e684c04be7b2415c4578785cbf1adb07793f3e74965af`。
