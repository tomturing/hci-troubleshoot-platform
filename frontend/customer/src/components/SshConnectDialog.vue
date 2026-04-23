<script setup lang="ts">
/**
 * SshConnectDialog.vue
 * SSH 连接弹框 — 薄包装层
 *
 * 功能：作为统一 SSH 弹框的容器，内嵌 SshFlowPanel。
 * 两种模式均通过此弹框呈现：
 *   create-case   → 工单已创建，需要连接 SSH 并采集数据（由 createCaseAndOpenSsh 触发）
 *   terminal-only → 已有工单，用户点击 [SSH终端] 按钮触发
 */
import SshFlowPanel from './SshFlowPanel.vue'
import { useChatStore } from '@/stores/chat'
import { computed } from 'vue'

const chatStore = useChatStore()

const dialogTitle = computed(() =>
  chatStore.sshFlowDialogMode === 'create-case' ? '连接 SSH 并采集环境数据' : '连接 SSH 终端'
)

function handleSuccess() {
  chatStore.sshFlowDialogVisible = false
  // openTerminalSidebar 在 SshFlowPanel 内部已调用
}

async function handleCancel() {
  await chatStore.closeSshFlowDialog()
}
</script>

<template>
  <el-dialog
    v-model="chatStore.sshFlowDialogVisible"
    :title="dialogTitle"
    width="500px"
    :close-on-click-modal="false"
    align-center
    class="ssh-connect-dialog"
    @close="handleCancel"
  >
    <SshFlowPanel
      :mode="chatStore.sshFlowDialogMode"
      :case-id="chatStore.sshFlowDialogCaseId"
      @success="handleSuccess"
      @cancel="handleCancel"
    />
  </el-dialog>
</template>

<style scoped>
:deep(.el-dialog__body) {
  padding: 16px 24px 24px;
}
</style>
