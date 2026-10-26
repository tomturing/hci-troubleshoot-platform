---
status: active
category: task
audience: developer
last_updated: 2026-10-26
owner: team
---

# Customer UI 工作区三栏自适应布局任务

## 关联方案

[方案事件文档](../../solution/events/2026-10-26-CustomerUI工作区三栏自适应布局方案.md)

## 任务清单

### T1: 布局求解器与单元测试

- **描述**：新增 `utils/workspaceLayout.ts`，实现 `resolveWorkspaceLayout()` 纯函数与常量；
  覆盖 5 种视口 × 0/1/2 抽屉组合，断言三态、宽度区间、左右不重叠、中区不低于下限。
- **文件变更**：新增 `src/utils/workspaceLayout.ts`、`src/utils/__tests__/workspaceLayout.spec.ts`
- **验收标准**：`pnpm --filter @hci/customer test workspaceLayout` 全绿
- **依赖**：无
- **预计耗时**：2h
- **状态**：待开始

### T2: store 布局状态接入

- **描述**：`chat.ts` 增加 `viewportWidth`、`leftPanelPreferred`、`rightPanelPreferred`，
  以及 `isLeftPanelOpen`（日志收集 ∨ 历史工单）与 `isRightPanelOpen`（终端）聚合状态；
  `sidePanelWidth` 改为由求解器派生，保留兼容导出。
- **文件变更**：`src/stores/chat.ts`
- **验收标准**：store 导出类型通过 `vue-tsc`；旧字段引用全部改为派生值
- **依赖**：T1
- **预计耗时**：2h
- **状态**：待开始

### T3: 外壳三态让位

- **描述**：`App.vue` 拆出全宽 `.workspace-shell`，用 `--ws-left/--ws-right` 驱动 padding，
  传入求解结果；展开态零让位、侧滑态单侧让位、压缩态双侧让位，过渡 300ms。
- **文件变更**：`src/App.vue`
- **验收标准**：拖拽/开合抽屉时中央区平滑移动无跳动；0 抽屉时与现状像素级一致
- **依赖**：T2
- **预计耗时**：2h
- **状态**：待开始

### T4: 三个抽屉统一绑定求解宽度

- **描述**：`ChatWindow.vue`（历史工单、SSH 终端）与 `LogCollectPanel.vue`（日志收集）
  的 `size` 改为求解值；push 态 `modal=false`，overlay 态保留遮罩；移除「钉住」入口。
- **文件变更**：`ChatWindow.vue`、`LogCollectPanel.vue`、`styles/workspace-drawer.css`
- **验收标准**：1920 双开无重叠；1366 双开自动降级为覆盖式；抽屉可被 ESC/关闭按钮收起
- **依赖**：T3
- **预计耗时**：3h
- **状态**：待开始

### T5: 抽屉内部宽度自适应

- **描述**：新增 `useContainerWidth`；日志面板描述表列数随容器切换 1/2 列，表单窄时 label 置顶；
  终端 fit 加 60ms 防抖。
- **文件变更**：新增 `src/composables/useContainerWidth.ts`、`LogCollectPanel.vue`、`TerminalPanel.vue`
- **验收标准**：420px 宽时日志字段名不出现逐字换行；拖拽宽度变化后终端列数正确
- **依赖**：T4
- **预计耗时**：3h
- **状态**：待开始

### T6: 左侧互斥与拖拽约束

- **描述**：打开日志收集时关闭历史工单，反之亦然；拖拽只写 preferred 并 clamp 到常量区间。
- **文件变更**：`chat.ts`、`ChatWindow.vue`、`LogCollectPanel.vue`
- **验收标准**：左侧永不重叠；拖到极限时中央区不低于下限
- **依赖**：T4
- **预计耗时**：1h
- **状态**：待开始

### T7: 端到端验证与文档

- **描述**：Playwright 在 1920/1600/1440/1366 视口下验证 0/1/2 抽屉三态截图；
  更新 `docs/solution/custom-ui/客户端设计.md` 与 README 第一屏。
- **文件变更**：`docs/**`、`README.md`
- **验收标准**：截图与验收清单逐条对应；文档门禁通过
- **依赖**：T5、T6
- **预计耗时**：2h
- **状态**：待开始

## 任务依赖图

```
T1 → T2 → T3 → T4 → T5 → T7
                  ↘ T6 ↗
```

## 执行顺序建议

1. 第一批：T1
2. 第二批：T2
3. 第三批：T3
4. 第四批：T4
5. 第五批（可并行）：T5、T6
6. 第六批：T7

## 文档更新计划

- [ ] `docs/solution/custom-ui/客户端设计.md` - 新增布局章节与变更历史
- [ ] `README.md` 第一屏 - 更新已实现能力描述（如涉及）

## 测试计划

- 单元测试：求解器全分支；日志面板列数切换
- 集成测试：App 外壳让位与抽屉 size 一致
- 人工测试：四种视口下的三态截图比对
