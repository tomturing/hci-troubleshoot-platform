<script setup lang="ts">
/**
 * 客户侧「一眼定界」卡：三段五组件 + 首个异常点 + S1-S8 阶段条。
 *
 * 数据全部来自服务端 derive() 派生的 display，组件不自行判断结论；
 * 独立成文件是为了让真浏览器验收页能直接挂载同一份产品 UI 逐场景截图，
 * 而不是另画一个与产品无关的示意页。
 */
import type { LogCollectDisplayView } from '@/api/logCollect'

defineProps<{ display?: LogCollectDisplayView | null }>()
</script>

<template>
  <!-- 客户只需读「哪一段 / 哪个组件 / 什么问题 / 下一步做什么」；
       调用链ID随卡给出，截图反馈后管理员可还原整条链路。 -->
  <div v-if="display?.fault_attribution" class="lc-fault-card">
    <div class="lc-fault-head">
      <el-tag size="small" effect="dark" type="warning">
        {{ display?.fault_attribution?.component_tag }}
      </el-tag>
      <span class="lc-fault-loc">
        {{ display?.fault_attribution?.scope_label }} ·
        {{ display?.fault_attribution?.stage }}
        {{ display?.fault_attribution?.stage_label }}
      </span>
    </div>
    <div class="lc-fault-text">{{ display?.fault_attribution?.customer_text }}</div>
    <div class="lc-fault-trace">
      调用链ID：{{ display?.trace_id || '—' }}（截图反馈请一并带上）
    </div>
  </div>
  <!-- S1-S8 阶段条：异常段高亮，客户一眼看清卡在哪一段 -->
  <div v-if="display?.stages?.length" class="lc-stage-bar">
    <div
      v-for="stage in display?.stages ?? []"
      :key="stage.key"
      class="lc-stage"
      :class="`lc-stage--${stage.status}`"
      :title="stage.criteria"
    >
      <div class="lc-stage-scope">{{ stage.scope_label }}</div>
      <div class="lc-stage-name">{{ stage.key }} {{ stage.label }}</div>
      <div class="lc-stage-components">
        <span v-for="item in stage.components" :key="item.key" class="lc-stage-component">{{ item.label }}</span>
      </div>
      <div class="lc-stage-status">{{ stage.status_label }}</div>
    </div>
  </div>
</template>

<style scoped>
.lc-fault-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color);
  border-left: 4px solid var(--el-color-warning);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
}
.lc-fault-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.lc-fault-loc,
.lc-fault-trace {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.lc-fault-text {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.6;
  word-break: break-word;
}
.lc-fault-trace {
  word-break: break-all;
}
.lc-stage-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.lc-stage {
  display: flex;
  flex: 1 1 110px;
  flex-direction: column;
  gap: 3px;
  min-width: 110px;
  padding: 6px 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-blank);
}
.lc-stage-scope,
.lc-stage-components,
.lc-stage-status {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.lc-stage-name {
  font-size: 12px;
  font-weight: 600;
}
.lc-stage-components {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.lc-stage--failed {
  border-color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}
.lc-stage--waiting {
  border-color: var(--el-color-warning);
  background: var(--el-color-warning-light-9);
}
.lc-stage--running {
  border-color: var(--el-color-primary);
}
.lc-stage--success .lc-stage-status { color: var(--el-color-success); }
.lc-stage--failed .lc-stage-status { color: var(--el-color-danger); font-weight: 600; }
.lc-stage--waiting .lc-stage-status { color: var(--el-color-warning); font-weight: 600; }
</style>
