<script setup lang="ts">
/**
 * SshConnectDialog.vue
 * SSH 连接弹框（薄壳）
 *
 * 模板、状态机、bridge 检测全部收敛到 SshConnectSection：
 * - 与「日志收集 → 指定工单号」弹框内的内嵌登录共用同一份实现；
 * - 本组件只负责 el-dialog 的开关与取消后的清理语义。
 */
import { useChatStore } from '@/stores/chat'
import SshConnectSection from './SshConnectSection.vue'

const props = defineProps<{
  bridgeStatus: 'running' | 'not-running' | 'checking'
}>()

const chatStore = useChatStore()

async function handleCancelled() {
  // 未建立连接时取消：清理正在连接的 bridge 会话；已连接则保持，避免误断终端
  if (chatStore.sshConnectionState !== 'connected') {
    await chatStore.disconnectSSH()
  }
  await chatStore.closeSshFlowDialog()
}
</script>

<template>
  <el-dialog
    v-model="chatStore.sshFlowDialogVisible"
    :title="
      chatStore.sshFlowDialogMode === 'create-case' ? '连接 SSH 并采集环境数据' : '连接 SSH 终端'
    "
    width="500px"
    :close-on-click-modal="false"
    align-center
    class="ssh-connect-dialog"
    @close="handleCancelled"
  >
    <SshConnectSection
      :bridge-status="props.bridgeStatus"
      mode="dialog"
      :case-id="chatStore.sshFlowDialogCaseId"
      auto-close
    />
  </el-dialog>
</template>
