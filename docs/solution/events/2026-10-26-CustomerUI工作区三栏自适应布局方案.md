---
status: active
category: solution
audience: developer
last_updated: 2026-10-26
owner: team
---

# Customer UI 工作区三栏自适应布局方案

## 背景与需求

见 [需求事件文档](../../requirement/events/2026-10-26-CustomerUI工作区三栏自适应布局需求.md)。
一句话：左右抽屉同时展开时宽度按"边角料"分配，导致左表格逐字换行、右终端列数不足；
需要按抽屉数量在展开 / 侧滑 / 压缩三态间切换，并让抽屉内部随容器宽度换密度。

## 方案（WHAT）

### 1. 用纯函数求解布局，取代 `getDefaultPanelWidth()`

新增 `frontend/customer/src/utils/workspaceLayout.ts`：

```ts
export type WorkspaceMode = 'expanded' | 'slide' | 'compressed' | 'overlay'

export interface WorkspaceLayoutInput {
  viewport: number        // window.innerWidth
  leftOpen: boolean       // 日志收集 或 历史工单
  rightOpen: boolean      // SSH 终端
  leftPreferred: number   // 用户拖拽后的左侧偏好宽度
  rightPreferred: number  // 用户拖拽后的右侧偏好宽度
}

export interface WorkspaceLayout {
  mode: WorkspaceMode
  leftWidth: number
  rightWidth: number
  centerMaxWidth: number  // 中央会话区在剩余空间内的最大宽度
  leftOffset: number      // 主区需让出的左侧像素
  rightOffset: number     // 主区需让出的右侧像素
}
```

常量取值为硬约束（来源见「决策依据」）：

| 常量 | 值 | 依据 |
|------|----|------|
| `CENTER_MAX` | 900 | 现状阅读宽度，不劣化 |
| `CENTER_PREF` | 560 | 双开时优先保留的会话宽度 |
| `CENTER_FLOOR` | 460 | 气泡 + 输入框的最低可读宽度 |
| `LEFT_MIN` / `LEFT_MAX` | 420 / 720 | 单列描述表下限 / 超过后信息密度无收益 |
| `RIGHT_MIN` / `RIGHT_MAX` | 560 / 820 | xterm 约 66 列下限 / 90 列上限 |
| `NARROW_VIEWPORT` | 1024 | 低于此值单开也走覆盖式 |

求解顺序：

1. `viewport < 1024` → `overlay`：两侧都不让位，抽屉宽度 `clamp(86% 视口, 320, 560)`；
2. 双开：以 `CENTER_PREF` 为起点分配，`left = 0.42 × (viewport - CENTER_PREF)`，
   `right` 吃掉剩余并 clamp 到 `[RIGHT_MIN, RIGHT_MAX]`；若得到的中点宽度 `< CENTER_FLOOR`，
   退化为两侧各取下限重算；仍不足（`viewport < 1440`）→ `overlay`；
3. 单开：抽屉宽度 `clamp(preferred, min, min(max, viewport - CENTER_PREF))`，
   `centerMaxWidth = min(CENTER_MAX, viewport - drawerWidth)`，即剩余空间够宽时中央仍是 900 居中。

`0.42 / 0.58` 的权重来自内容宽度差：终端需要 66~90 列（约 560~820px），
左侧表格单列约 420px 即可读，右侧天然更吃宽度。

### 2. 主区用 CSS 变量让位，抽屉仍用 `el-drawer`

`App.vue` 外层新增全宽 shell，用左/右 padding 表示抽屉占位：

```css
.workspace-shell {
  --ws-left: 0px;
  --ws-right: 0px;
  padding-left: var(--ws-left);
  padding-right: var(--ws-right);
  transition: padding 0.3s var(--el-transition-function-ease-in-out-bezier);
}
.workspace-shell.dragging { transition: none; }
.app-container { max-width: 900px; margin: 0 auto; }
```

抽屉本身仍是 `el-drawer`（保留 ESC、焦点、动效、`append-to-body`），但：

- push 态 `:modal="false"`，不再渲染遮罩，主区可正常交互；
- overlay 态 `:modal="true"`，沿用现有遮罩与"点外部关闭"；
- **取消「钉住」入口**：push 态本来就不遮挡，钉住失去语义；抽屉头部只保留「关闭」。

### 3. 抽屉内部按容器宽度切换密度

新增 `useContainerWidth` 组合式（ResizeObserver 返回容器 px 宽度）：

- `LogCollectPanel`：`el-descriptions` 的 `:column` 由 `width >= 600 ? 2 : 1` 决定；
  `el-form` 在 `width < 480` 时切 `label-position="top"`；描述表内容允许 `word-break` 但
  字段名 `white-space: nowrap`，避免"一字一行"；
- `ChatWindow` 历史工单：列表项标题已 ellipsis，不变；详情区宽度跟随容器；
- `TerminalPanel`：已有 ResizeObserver + `fitTerminal()`，为宽度动画期间的重排加 60ms 防抖，
  避免拖动时每一帧都重算 xterm 尺寸。

### 4. 左侧互斥与拖拽约束

- `openLogCollect()` 同时 `showHistoryDrawer = false`；`openHistoryDrawer()` 同时 `logCollectVisible = false`；
- 左拖上限 `LEFT_MAX`、右拖上限 `RIGHT_MAX`，下限取各自 `*_MIN`，与求解器常量同源，
  拖拽结果只写 `preferred`，不直接写"当前宽度"。

## 决策依据（WHY）

### 方案选择

| 方案 | 优点 | 缺点 | 评分 |
|------|------|------|------|
| A. 求解器 + 主区让位（选中） | 改动集中在布局层；保留 el-drawer 无障碍与动效；纯函数可测 | 抽屉是 fixed，动画期间靠 CSS 变量对齐 | ★★★★☆ |
| B. 自研三栏 grid + 普通 div 列 | 语义最纯，push 天然 | 三个抽屉容器全部重写，焦点/ESC/遮罩需自实现，现有 6 个测试用例需重写 | ★★★☆☆ |
| C. 只调大 `getDefaultPanelWidth()` 的基准值 | 一行改动 | 抽屉变宽会盖住中央，必须靠"钉住"才能共存，本质问题未解 | ★★☆☆☆ |
| D. 纯 CSS 容器查询 + 断点 | 无 JS | 断点只能看视口，无法看"抽屉是否打开"，无法表达三态 | ★★☆☆☆ |

### 为什么选 A

第一性原理：屏幕宽度是唯一稀缺资源，分配必须由**内容最小宽度**正向推导，
而不是由"中央固定 900、剩下给抽屉"这种惯性写法反推。A 把分配逻辑收敛到一个纯函数，
其余都是渲染层适配，改动面最小且可回归。

不选 B 的关键理由：`el-drawer` 提供的 ESC、焦点陷阱、动效、`append-to-body` 目前都在用，
自研要重新实现且必须通过等价测试；本次需求是宽度分配，不是交互范式重构，收益不匹配风险。

不选 C 的理由：把宽度调大后抽屉会覆盖中央会话（fixed 定位），
必须依赖「钉住」才可共存，用户仍要在"看得见会话"和"看得清表格"之间二选一。

不选 D 的理由：CSS 容器查询只能感知容器自身宽度，无法感知"另一个抽屉是否打开"，
三态的判定必须有一个全局求解点。

### 取消「钉住」的取舍

现状「钉住」= 去掉遮罩 + 主区可点，本质是用户手动把覆盖式改成推挤式。
方案 A 让推挤成为默认行为后，「钉住」只剩一个状态，保留按钮会让用户误以为还有"未钉住"的另一种模式。
因此统一移除，抽屉头部只保留「关闭」。代价是老用户需要一个适应期，收益是少一个心智模型。

### 权衡与妥协

- 1440px 及以下双开时中央只有 460px，仍会换行，这是物理约束，不再强行压缩左抽屉；
- 1366px 及以下双开直接降级为覆盖式，不做"三栏硬塞"；
- 抽屉偏好不持久化到 localStorage，刷新后回到默认值，与现状一致（避免引入新的状态契约）。

## 影响范围

### 受影响的模块

- `frontend/customer/src/utils/workspaceLayout.ts`：新增求解器（替代旧宽度工具）
- 旧宽度工具（`getDefaultPanelWidth`）：本次删除，调用点全部改为求解器结果
- `frontend/customer/src/stores/chat.ts`：面板可见性聚合、左右偏好宽度、左侧互斥、视口宽度
- `frontend/customer/src/App.vue`：外壳让位与 CSS 变量
- `frontend/customer/src/components/ChatWindow.vue`：两个抽屉的 size/modal 绑定、拖拽、移除 pin
- `frontend/customer/src/components/LogCollectPanel.vue`：抽屉绑定、描述表列数、表单 label 位置
- `frontend/customer/src/styles/workspace-drawer.css`：push 态样式
- `frontend/customer/src/components/__tests__/LogCollectPanel.spec.ts`：宽度与 pin 断言更新
- 新增 `frontend/customer/src/utils/__tests__/workspaceLayout.spec.ts`

### 需要更新的文档

- [x] `docs/solution/custom-ui/客户端设计.md`（新增「工作区三栏布局」章节 + 变更历史）
- [x] 需求 / 方案 / 任务事件文档

### API 兼容性

无后端接口变更，无破坏性契约变更。

## 实施计划

T1 求解器与单测 → T2 store 接入 → T3 App 外壳让位 → T4 三抽屉绑定与降级 →
T5 抽屉内部响应式 → T6 左侧互斥与拖拽 → T7 端到端验证与文档。

## 风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| 抽屉动画与 padding 过渡不同步，出现缝隙 | 中 | 中 | 统一 0.3s 同曲线；拖拽时禁用过渡 |
| `el-drawer` 在 `modal=false` 时 overlay 容器仍拦截点击 | 高 | 中 | 实测 DOM；必要时用现有 `.drawer-unblocked-overlay` 兜底 |
| 终端在宽度动画中反复 fit 造成抖动 | 中 | 高 | 60ms 防抖 + 动画结束再 fit |
| 移除钉住后用户找不到"边看边操作"的方式 | 中 | 低 | push 态默认不遮挡，操作体验不降级 |

## 测试策略

- 求解器单测：viewport × (0/1/2 抽屉) 组合，断言 mode、左右宽度、中点宽度与不重叠；
- 组件测试：日志面板在 420 / 560 / 900 宽度下的描述表列数与 pin 入口移除；
- 浏览器端到端：Playwright 在 1920/1600/1440/1366 四种视口下截图，人工比对换行与遮罩；
- 回归：`pnpm --filter @hci/customer test`、`pnpm --filter @hci/customer build`。

## 验收标准

- [ ] 需求文档全部验收标准满足
- [ ] 求解器单测覆盖 5 种视口 × 3 种开合状态
- [ ] 端到端截图证明三种状态视觉符合预期
- [ ] 无新增 lint / 类型错误
