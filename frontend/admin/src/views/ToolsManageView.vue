<script setup lang="ts">
/**
 * 工具管理（管理台）。
 *
 * 与「日志收集」一致：侧边栏只有一个父入口，页内用页签切换两个子页面。
 * - agent工具：ReAct 引擎可调用的 Agent 工具定义；
 * - acli插件：HCI acli 插件规格维护与下发。
 * 两者是不同注册表，只共享「工具」这一信息架构位置，不共享数据。
 */
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AcliPluginManageView from './AcliPluginManageView.vue'
import ToolManageView from './ToolManageView.vue'

const route = useRoute()
const router = useRouter()

const activeTab = ref<'agent' | 'acli'>(route.query.tab === 'acli' ? 'acli' : 'agent')

watch(activeTab, (value) => {
  const query = { ...route.query }
  if (value === 'acli') {
    query.tab = 'acli'
  } else {
    delete query.tab
  }
  router.replace({ query })
})
</script>

<template>
  <div class="tools-manage-page">
    <el-tabs v-model="activeTab" class="tools-manage-tabs">
      <el-tab-pane label="agent工具" name="agent" lazy>
        <ToolManageView />
      </el-tab-pane>
      <el-tab-pane label="acli插件" name="acli" lazy>
        <AcliPluginManageView />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.tools-manage-page {
  padding: 0;
}

.tools-manage-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}
</style>
