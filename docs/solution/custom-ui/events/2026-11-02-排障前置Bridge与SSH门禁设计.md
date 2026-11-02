# 排障前置 Bridge 与 SSH 门禁设计

> 版本：v1.0
> 日期：2026-11-02
> 关联代码：`frontend/customer/src/stores/chat.ts`、`frontend/customer/src/components/TroubleshootGateDialog.vue`、`frontend/customer/src/components/ChatWindow.vue`、`frontend/customer/src/components/MessageBubble.vue`
> 关联文档：[日志收集 SSH 统一登录与探测式准备阶段](../../events/2026-10-23-日志收集SSH统一登录与探测式准备阶段.md)、[离线诊断模式业务设计](../../diagnosis/离线诊断模式业务设计.md)

---

## 一、现场缺陷

2026-10-29 工单 `Q2026102901522`（检测到硬盘 / 主机 172.28.24.67）：

1. 客户刷新 customer-ui 页面；
2. 页面提示「发现未关闭的工单」→ 点「继续处理」；
3. 前端**没有做任何 bridge / SSH 检查**，直接恢复会话并允许发送消息；
4. 客户发送消息后，后端把 SOP 步骤 `sop_request_variable` 推到「执行中」，
   而节点一侧根本没有 SSH 会话 → 界面停在「正在等待输出」，直到超时。

同一时刻对照「日志收集」入口：它会先探测 bridge，未连接时弹统一的 SSH 登录框，
因此**同样缺会话的两个入口表现完全不同**——这正是本缺陷的可见形态。

## 二、根因（第一性原理）

排障的物理本质是：**Agent 通过 terminal_bridge 的 SSH 会话在 HCI 节点上执行命令**
（SOP 步骤、`acli` 命令、日志采集、文件传输全部走这一条链路）。

这条链路成立只有两个可观测前提：

| 前提 | 可观测事实 | 观测方式 |
|---|---|---|
| F1 | 客户机本机的 terminal_bridge 进程在跑 | WebSocket 握手能收到 `bridge_hello`/`bridge_ready`（`checkBridgeBeforeOpen`，桌面双端口 9999 / 47324） |
| F2 | 本工单已建立 SSH 会话 | 前端 `sshConnectionState === 'connected'`（`ssh_connect` 收到 `ssh_connected` 后置位） |

缺任一前提时，**消息仍然会被后端接受**：会话服务不校验节点可达性，
Agent 把工具调用交给 bridge 后只能等一个永远不会来的回包。
也就是说，失败点在「SOP 已开始」之后才暴露，此时用户的感知是「卡住」而不是「没连上」。

结论：门禁必须挡在**排障动作之前**，并且要在前端可见地告诉用户「缺哪一半」。

### 为什么不复用 bridge 侧残留会话

bridge 的会话表键是 `caseID@nodeIP`，且**没有查询协议**（协议里只有 `ssh_connect` /
`ssh_input` / `ssh_disconnect` / `log_collect` / `sftp_op` 等动作，没有 `session_status`）。
刷新后前端拿不到「桥侧是否还持有本工单会话」这个事实，只能二选一：

- 假设它还在 → 与现场事实相反（页面刷新后 WS 已断，输入无法路由）；
- 要求重新登录 → 可验证、可复现，且 bridge 在 `ssh_connect` 同键覆盖时走
  `destroyOrDetachSession`，不会腰斩进行中的长任务。

本设计选后者：**刷新后一律按「未连接」处理**。真要做「免登录续用会话」，
需要先给 bridge 增加只读查询消息类型，属于后续独立迭代。

## 三、设计

### 3.1 门禁状态机

```
进入排障动作
   │
   ├─ sshConnectionState === 'connected' ──────────────→ ready（零探测，直接放行）
   │
   └─ 否则 openTroubleshootGate()
          │  stage=checking → probe bridge（≤3s）
          ├─ bridge 未运行 → stage=bridge-down
          │     出路：下载并运行 terminal_bridge / 已启动·重新检测 / 无 SSH 排障 / 暂不排障
          └─ bridge 运行中 → stage=ssh-required
                出路：内嵌 SSH 登录（SshConnectSection，唯一模板） / 无 SSH 排障 / 暂不排障
                       │
                       └─ 收到 ssh_connected（或别处先连上）→ ready
```

门禁结论是一个三分枚举，直接决定调用方的动作：

| 结论 | 含义 | 调用方行为 |
|---|---|---|
| `ready` | 两个前提都成立 | 继续原动作（发消息 / 恢复会话 / 切换工单） |
| `offline` | 用户选择「无 SSH 排障」 | 跳 `/offline-diagnosis?case_id=<当前工单>` |
| `cancelled` | 用户选择「暂不排障」 | 中止本次动作，不改变工单状态 |

### 3.2 四个入口，一个实现

| 入口 | 时机 | 是否阻塞 |
|---|---|---|
| `resumePendingCase()` | 刷新后「继续处理」未关闭工单 | 先加载历史，再异步弹门禁（不 await，保证历史先呈现） |
| `switchToCase()` | 历史工单「切换到此工单」 | 同上 |
| `sendMessage()`（已有工单） | 发送消息 = 让 Agent 开始执行 | **await 阻塞**：门禁未通过不得落消息、不得发请求 |
| 新建工单 | 首条消息触发 `CaseCreateDialog` | 由既有弹框承担（bridge 前置检测 + SSH 表单 + 无 SSH 创建） |

四个入口共用一个 store 状态与同一份探测结果：

- 已连接 SSH 时**不探测**——会话本身就是「bridge 存活 + 会话可用」的充分证据，
  再 probe 一次只是白等 1.5~3s，且终端抖动时会误判成「bridge 未运行」；
- 并发入口（恢复 + 发消息）共用同一个 Promise，**不会叠两层遮罩**；
- 探测自身抛错/超时一律按「未运行」处理。门禁语义是「无法证明可用就不能开始」，
  不是把异常抛给刷新恢复路径（那样用户只看到页面没反应）。

### 3.3 「无 SSH 排障」是特性选项，不是降级

离线诊断是独立能力（见 [离线诊断模式业务设计](../../diagnosis/离线诊断模式业务设计.md)）：
平台不连现场，客户用离线采集工具取包后上传，工程师审核后给报告。
因此门禁把「无 SSH 排障」作为**并列出路**放在弹框左下角，而不是藏在错误提示里。

### 3.4 弹框呈现要点

- 无关闭按钮、禁用 ESC 与遮罩点击：必须做出一次明确选择（三条出路之一）；
- bridge 阶段暴露**本次探测地址**（如 `ws://localhost:9999`）：客户排「端口被别的进程占用」时这是唯一线索；
- SSH 阶段复用 `SshConnectSection`（唯一 SSH 模板），不另造表单；
- 触发原因写在标题下方（「继续处理未关闭的工单」/「切换到历史工单继续排障」/「继续排障」），
  避免用户困惑「为什么突然要我连 SSH」。

## 四、与消息契约的衔接

交互卡片（SOP 变量、`sop_fallback`、推荐值确认）把 `sendMessage(...) === false`
一律解释成「流式期间被守卫吞掉」，会提示「诊断仍在进行中，请稍后再选择」。
门禁拦下时这句话与事实相反，因此增加 `troubleshootGateBlockedSend` 标记：

- `sendMessage` 每次进入门禁前清零，被门禁拦下时置位；
- 上述四处提示改为 `sent === false && !troubleshootGateBlockedSend` 才提示，
  门禁场景的说明交给门禁弹框本身。

普通输入框路径则在取消时把内容回填到草稿（`assistantDraftText`），用户不必重打。

## 五、对抗性审查：这套设计在什么条件下会失效

| 失效条件 | 现状 | 处置 |
|---|---|---|
| bridge 探测成功但实际无法建会话（防火墙只放行 WS 端口） | 检测不出 | 由 SSH 登录失败态兜底（`ssh_error` 会展示 bridge 给出的原因） |
| 刷新后 bridge 侧仍有可用会话，用户被迫重新登录 | 已知成本 | 重新登录走 `destroyOrDetachSession`，不腰斩长任务；免登录续用需 bridge 增加查询协议 |
| 门禁通过后 SSH 会话在发送途中断开 | 检测不出（只在发送前检查） | 由 bridge 的 `ssh_input_rejected` 与流式错误兜底；下一次发送会重新门禁 |
| 用户在门禁内取消后再次发送 | 会再次弹门禁 | 符合预期：没有会话就不能排障 |
| 只读需求（例如单纯查看历史对话）被拦住 | 不会：历史列表与消息预览不经过门禁 | 只有「切换到此工单」「继续处理」才算继续排障 |

## 六、验收标准

| 编号 | 场景 | 期望 |
|---|---|---|
| V1 | 已连接 SSH，在工单内发消息 | 直接发送，不弹门禁，且不调用 `checkBridgeBeforeOpen` |
| V2 | bridge 未运行，工单内发消息 | 消息不落库、不请求；弹 bridge 阶段；取消后内容回到输入框 |
| V3 | bridge 运行但未连 SSH | 弹 SSH 阶段；登录成功后自动继续发送原消息 |
| V4 | 选择「无 SSH 排障」 | 跳离线诊断页，带当前工单号；本次消息不发 |
| V5 | 刷新后点「继续处理」 | 历史先呈现，随后弹门禁（未连接时） |
| V6 | 两个入口并发触发 | 只弹一个弹框，`checkBridgeBeforeOpen` 只调用一次 |
| V7 | 门禁内连上（或别处先连上） | 门禁自动关闭并继续原动作 |

上述 V1~V7 由 `frontend/customer/src/stores/__tests__/chat.troubleshoot-gate.spec.ts` 覆盖；
弹框三段视图与三条出路由 `frontend/customer/src/components/__tests__/TroubleshootGateDialog.spec.ts` 覆盖。
反向验证：把 `sendMessage` 中的门禁调用改为恒真后，V2~V4 与并发用例共 4 条立即变红。

## 七、遗留

- 真机 UI 走查未执行（本机无 playwright 依赖），只有 happy-dom 组件测试；
  上线前需在浏览器中实测一次「刷新 → 继续处理 → 门禁 → 登录 → 自动继续」的完整链路。
- 新建工单弹框内的同类按钮文案仍为「无 SSH 创建工单」，语义与门禁的
  「无 SSH 排障（离线诊断）」一致但措辞不同，后续统一。
