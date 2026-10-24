<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { Finished, FolderOpened, Monitor, Tickets } from '@element-plus/icons-vue'
import { useChatStore } from '@/stores/chat'
import { getClientId } from '@/utils/clientId'
import ChatWindow from '@/components/ChatWindow.vue'
import LogCollectCaseDialog from '@/components/LogCollectCaseDialog.vue'
import LogCollectPanel from '@/components/LogCollectPanel.vue'
import SshConnectDialog from '@/components/SshConnectDialog.vue'

const chatStore = useChatStore()
const clientId = getClientId()
// 抽屉开合收敛到 store：建单后的引导卡片也通过同一状态打开面板，
// 避免 App.vue 与 ChatWindow 各持一份本地 ref 导致两处入口行为不一致。
const logCollectVisible = computed({
  get: () => chatStore.logCollectVisible,
  set: (visible: boolean) => (visible ? chatStore.openLogCollect() : chatStore.closeLogCollect()),
})

// Bridge 下载地址：优先读环境变量，回退到相对路径
// 部署时在 .env 中配置 VITE_BRIDGE_DOWNLOAD_URL 指向实际文件
const BRIDGE_DOWNLOAD_URL =
  import.meta.env.VITE_BRIDGE_DOWNLOAD_URL || '/downloads/terminal_bridge.exe'

onMounted(() => {
  chatStore.initialize()
})

/**
 * 触发 terminal_bridge.exe 下载
 * 用隐藏 <a download> 替代 window.open，避免浏览器弹窗拦截策略
 */
function handleDownloadBridge() {
  chatStore.closeBridgeDownload()

  const a = document.createElement('a')
  a.href = BRIDGE_DOWNLOAD_URL
  a.download = 'terminal_bridge.exe'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  // 延迟移除，确保点击事件已触发
  setTimeout(() => document.body.removeChild(a), 200)
}

</script>

<template>
  <div class="app-container">
    <header class="app-header">
      <div class="header-content">
        <h1>HCI 故障排查助手</h1>
        <div class="header-badges">
          <!-- 工单信息：历史工单入口 + ID/工单号展示 -->
          <el-popover
            placement="bottom-end"
            :width="260"
            trigger="click"
          >
            <template #reference>
              <el-button
                size="small"
                round
                class="header-btn case-info-btn"
              >
                <el-icon><Tickets /></el-icon>
                工单信息
              </el-button>
            </template>
            <div class="case-info-popover">
              <div class="case-info-row">
                <span class="case-info-label">客户端 ID</span>
                <span class="case-info-value selectable" :title="clientId">{{ clientId }}</span>
              </div>
              <div v-if="chatStore.currentCase" class="case-info-row">
                <span class="case-info-label">当前工单</span>
                <span class="case-info-value selectable">{{ chatStore.currentCase.case_id }}</span>
              </div>
              <div v-else class="case-info-row">
                <span class="case-info-label">当前工单</span>
                <span class="case-info-value muted">暂无</span>
              </div>
              <el-divider style="margin: 10px 0" />
              <el-button
                size="small"
                style="width: 100%"
                @click="chatStore.openHistoryDrawer()"
              >
                <el-icon><Tickets /></el-icon>
                查看历史工单
              </el-button>
            </div>
          </el-popover>

          <!-- 关闭工单 -->
          <el-button
            v-if="chatStore.hasActiveCase"
            size="small"
            round
            class="header-btn close-btn"
            @click="chatStore.handleCloseCase()"
          >
            <el-icon><Finished /></el-icon>
            关闭工单
          </el-button>

          <!-- 日志收集：常驻入口；无活跃工单时由对话框要求手工指定工单号 -->
          <el-button
            size="small"
            round
            class="header-btn log-collect-btn header-action-btn"
            @click="chatStore.openLogCollect()"
          >
            <el-icon><FolderOpened /></el-icon>
            日志收集
          </el-button>

          <!-- SSH终端按钮：点击先检测 Bridge -->
          <el-button
            size="small"
            round
            class="header-btn terminal-btn header-action-btn"
            :loading="chatStore.bridgeStatus === 'checking'"
            @click="chatStore.checkAndOpenTerminal()"
          >
            <el-icon v-if="chatStore.bridgeStatus !== 'checking'"><Monitor /></el-icon>
            SSH终端
          </el-button>
        </div>
      </div>
    </header>
    <main class="app-main">
      <ChatWindow />
    </main>

    <!-- SSH 连接弹框（create-case / terminal-only 统一入口） -->
    <SshConnectDialog :bridge-status="chatStore.sshConnectDialogBridgeStatus" />

    <!-- 日志收集抽屉：复用终端已建立的 SSH 会话执行 acli log_collect -->
    <LogCollectPanel
      v-if="chatStore.logCollectCaseId"
      v-model="logCollectVisible"
      :case-id="chatStore.logCollectCaseId"
    />

    <!-- 无活跃工单时人工指定归档工单号 -->
    <LogCollectCaseDialog />

    <!-- Bridge 未运行时的下载提示弹窗 -->
    <el-dialog
      v-model="chatStore.showBridgeDownload"
      title="SSH 终端插件"
      width="420px"
      :close-on-click-modal="true"
      class="bridge-dialog"
    >
      <div class="bridge-prompt">
        <el-icon class="bridge-icon"><Monitor /></el-icon>
        <p class="bridge-title">生效 SSH 终端插件</p>
        <p class="bridge-desc">
          SSH 终端需要本地 Bridge 组件（2.5M）支持，检测到当前未运行。<br />
          请点击下载并打开，启动后再次点击「终端」按钮即可使用。
        </p>
      </div>
      <template #footer>
        <el-button @click="chatStore.closeBridgeDownload()">取消</el-button>
        <el-button type="primary" class="bridge-download-btn" @click="handleDownloadBridge">
          <el-icon><FolderOpened /></el-icon>
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

.case-info-popover {
  padding: 4px 0;
}

.case-info-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  line-height: 1.5;
}

.case-info-label {
  flex-shrink: 0;
  color: #909399;
  width: 64px;
}

.case-info-value {
  color: #303133;
  word-break: break-all;
}

.case-info-value.selectable {
  cursor: text;
  user-select: all;
}

.case-info-value.muted {
  color: #c0c4cc;
  font-style: italic;
}

/*
 * 顶栏按钮统一规范（工单信息 / 关闭工单 / 日志收集 / SSH终端 共用）
 *
 * 为什么用实色白底而不是半透明白：
 * 顶栏背景是 #409eff → #337ecc 的横向渐变，半透明层叠在渐变上，
 * 同一个 rgba 值在左右两侧会合成出不同的颜色——即使四个按钮写的是同一个
 * rgba，肉眼看到的仍是四种底色。只有实色才真正一致。
 */
.header-btn {
  background: #ffffff !important;
  border: 1px solid #ffffff !important;
  color: #2b6cb0 !important;
  font-size: 12px !important;
  font-weight: 500 !important;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

/* 悬停色只绑定 :hover。不要绑定 :focus / :focus-visible：
   按钮被点过以后会保留焦点，Element Plus 也会给 :focus 上 hover 色，
   结果就是"点过的那个按钮一直和另外三个颜色不一样"。 */
.header-btn:hover {
  background: #ecf5ff !important;
  border-color: #ecf5ff !important;
  color: #2b6cb0 !important;
}

/* 键盘可达性只加描边：白色描边在蓝色顶栏上足够可见，且不改变底色。
   Element Plus 自身的 :focus 着色不带 !important，压不过上面的白底 !important，
   所以点过的按钮移开鼠标后仍会回到白色。 */
.header-btn:focus-visible {
  outline: 2px solid #ffffff;
  outline-offset: 2px;
}

/* 图标与文字间距统一交给 el-button 自身渲染，避免个别按钮多 4px 造成不齐 */
.header-btn .el-icon {
  margin-right: 0;
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
  color: var(--el-color-primary);
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

.bridge-download-btn :deep(span) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}

/* 终端历史按钮 */
.terminal-history-btn {
  background: rgba(64, 158, 255, 0.3) !important;
  border: 1px solid rgba(64, 158, 255, 0.5) !important;
  color: #fff !important;
  font-size: 12px !important;
}

.terminal-history-btn:hover {
  background: rgba(64, 158, 255, 0.5) !important;
}

/* 终端回放弹窗 */
.terminal-replay-dialog :deep(.el-dialog__body) {
  padding: 12px;
  max-height: 80vh;
  overflow: hidden;
}

.terminal-replay-dialog :deep(.el-dialog) {
  max-width: 1200px;
}
</style>
