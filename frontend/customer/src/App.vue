<script setup lang="ts">
import { onMounted } from 'vue'
import { useChatStore } from '@/stores/chat'
import { getClientId } from '@/utils/clientId'
import ChatWindow from '@/components/ChatWindow.vue'

const chatStore = useChatStore()
const clientId = getClientId()

// Bridge 下载地址，替换为实际托管地址
const BRIDGE_DOWNLOAD_URL = '/downloads/terminal_bridge.exe'

onMounted(() => {
  chatStore.initialize()
})

function handleDownloadBridge() {
  window.open(BRIDGE_DOWNLOAD_URL, '_blank')
  chatStore.closeBridgeDownload()
}
</script>

<template>
  <div class="app-container">
    <header class="app-header">
      <div class="header-content">
        <h1>HCI 故障排查助手</h1>
        <div class="header-badges">
          <!-- 终端按钮：点击先检测 Bridge -->
          <el-button
            size="small"
            round
            class="terminal-btn"
            :loading="chatStore.bridgeStatus === 'checking'"
            @click="chatStore.checkAndOpenTerminal()"
          >
            <el-icon v-if="chatStore.bridgeStatus !== 'checking'"><i class="el-icon-monitor" /></el-icon>
            终端
          </el-button>
          <el-button
            v-if="chatStore.hasActiveCase"
            size="small"
            round
            class="close-btn"
            @click="chatStore.handleCloseCase()"
          >
            ✅ 关闭工单
          </el-button>
          <el-button
            size="small"
            round
            class="history-btn"
            @click="chatStore.openHistoryDrawer()"
          >
            📋 历史工单
          </el-button>
          <span class="client-badge" :title="clientId">
            ID: {{ clientId.substring(0, 15) }}...
          </span>
          <span v-if="chatStore.currentCase" class="case-badge">
            工单: {{ chatStore.currentCase.case_id }}
          </span>
        </div>
      </div>
    </header>
    <main class="app-main">
      <ChatWindow />
    </main>

    <!-- Bridge 未运行时的下载提示弹窗 -->
    <el-dialog
      v-model="chatStore.showBridgeDownload"
      title="SSH 终端插件"
      width="420px"
      :close-on-click-modal="true"
      class="bridge-dialog"
    >
      <div class="bridge-prompt">
        <div class="bridge-icon">🖥️</div>
        <p class="bridge-title">生效 SSH 终端插件</p>
        <p class="bridge-desc">
          SSH 终端需要本地 Bridge 组件支持，检测到当前未运行。<br />
          请点击下载并打开，启动后再次点击「终端」按钮即可使用。
        </p>
      </div>
      <template #footer>
        <el-button @click="chatStore.closeBridgeDownload()">取消</el-button>
        <el-button type="primary" @click="handleDownloadBridge">
          <el-icon style="margin-right: 4px"><i class="el-icon-download" /></el-icon>
          下载并打开
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
    sans-serif;
  background: #f5f7fa;
}
</style>

<style scoped>
.app-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  max-width: 900px;
  margin: 0 auto;
}

.app-header {
  background: linear-gradient(135deg, #409eff, #337ecc);
  color: #fff;
  padding: 12px 24px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}

.header-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-content h1 {
  font-size: 18px;
  font-weight: 600;
}

.case-badge {
  background: rgba(255, 255, 255, 0.2);
  padding: 4px 12px;
  border-radius: 12px;
  font-size: 13px;
}

.header-badges {
  display: flex;
  align-items: center;
  gap: 8px;
}

.client-badge {
  background: rgba(255, 255, 255, 0.15);
  padding: 4px 10px;
  border-radius: 12px;
  font-size: 12px;
  cursor: pointer;
  user-select: all;
  opacity: 0.85;
}

.history-btn {
  background: rgba(255, 255, 255, 0.2) !important;
  border: 1px solid rgba(255, 255, 255, 0.3) !important;
  color: #fff !important;
  font-size: 12px !important;
}

.history-btn:hover {
  background: rgba(255, 255, 255, 0.35) !important;
}

.terminal-btn {
  background: rgba(103, 194, 58, 0.3) !important;
  border: 1px solid rgba(103, 194, 58, 0.5) !important;
  color: #fff !important;
  font-size: 12px !important;
}

.terminal-btn:hover {
  background: rgba(103, 194, 58, 0.5) !important;
}

.terminal-btn :deep(.el-icon) {
  margin-right: 4px;
}

.close-btn {
  background: rgba(255, 255, 255, 0.9) !important;
  border: 1px solid rgba(255, 255, 255, 0.8) !important;
  color: #337ecc !important;
  font-size: 12px !important;
  font-weight: 600 !important;
}

.close-btn:hover {
  background: #fff !important;
  color: #409eff !important;
}

.app-main {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* Bridge 下载提示弹窗 */
.bridge-prompt {
  text-align: center;
  padding: 8px 0 16px;
}

.bridge-icon {
  font-size: 48px;
  margin-bottom: 12px;
}

.bridge-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 10px;
}

.bridge-desc {
  font-size: 13px;
  color: #606266;
  line-height: 1.8;
}
</style>
