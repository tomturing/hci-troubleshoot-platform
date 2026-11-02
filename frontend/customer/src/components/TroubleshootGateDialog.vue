<script setup lang="ts">
/**
 * TroubleshootGateDialog.vue
 * 排障前置门禁弹框（新增/继续工单排障的第一道关）
 *
 * 存在理由（第一性原理）：
 * 排障 = Agent 通过 terminal_bridge 的 SSH 会话在节点上执行命令。缺「bridge 在跑」
 * 或「本工单已 SSH 连接」任一前提，消息仍会被后端接受并把 SOP 推到"执行中"，
 * 然后永远等不到节点回包（刷新后直接点「继续处理」踩到的就是这个坑）。
 * 所以门禁必须挡在动作之前，且只给三条出路：
 *   1) 下载并运行 terminal_bridge（本机没有桥）
 *   2) SSH 登录（桥有了但没有到 HCI 的会话）
 *   3) 无 SSH 排障（跳离线诊断，平台不连现场）
 *
 * 状态与探测逻辑全部在 chat store（`openTroubleshootGate` 等），本组件只负责呈现，
 * 这样「刷新恢复 / 切换工单 / 发消息」四个入口共用同一个弹框与同一份探测结果。
 */
import { Download, Loading, Refresh } from '@element-plus/icons-vue'
import { useChatStore } from '@/stores/chat'
import SshConnectSection from './SshConnectSection.vue'

const chatStore = useChatStore()

/**
 * 触发 terminal_bridge.exe 下载。
 * 用隐藏 <a download> 而不是 window.open：后者会被浏览器弹窗拦截策略挡掉。
 */
function handleDownloadBridge() {
  const url = import.meta.env.VITE_BRIDGE_DOWNLOAD_URL || '/downloads/terminal_bridge.exe'
  const a = document.createElement('a')
  a.href = url
  a.download = 'terminal_bridge.exe'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  setTimeout(() => document.body.removeChild(a), 200)
}
</script>

<template>
  <el-dialog
    v-model="chatStore.troubleshootGateVisible"
    title="开始排障前检查"
    width="520px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    align-center
    class="troubleshoot-gate-dialog"
  >
    <!-- 触发原因：让用户知道"为什么突然要我连 SSH" -->
    <p class="gate-reason">{{ chatStore.troubleshootGateReasonText }}</p>

    <!-- ===== 检查中 ===== -->
    <div v-if="chatStore.troubleshootGateStage === 'checking'" class="gate-checking">
      <el-icon class="is-loading gate-spin"><Loading /></el-icon>
      <span>正在检查 terminal_bridge 是否可用…</span>
    </div>

    <!-- ===== Bridge 未运行 ===== -->
    <div v-else-if="chatStore.troubleshootGateStage === 'bridge-down'" class="gate-section">
      <el-alert type="warning" :closable="false" class="gate-alert">
        <template #title>
          <strong>未检测到 terminal_bridge</strong>
        </template>
        <p class="gate-desc">
          排障需要在客户机本机运行 terminal_bridge（提供到 HCI 的 SSH 通道）。
          下载并打开后回到本页点击「已启动，重新检测」。
        </p>
      </el-alert>

      <div class="gate-download">
        <el-button type="primary" class="gate-download-action" @click="handleDownloadBridge">
          <el-icon><Download /></el-icon>
          下载 terminal_bridge（Windows）
        </el-button>
      </div>

      <div class="gate-recheck">
        <el-button :loading="chatStore.troubleshootGateBridgeStatus === 'checking'" @click="chatStore.recheckTroubleshootGateBridge()">
          <el-icon><Refresh /></el-icon>
          已启动，重新检测
        </el-button>
      </div>

      <!-- 探测地址是客户排"端口被占用"的唯一线索，必须露出来 -->
      <p class="gate-endpoint">
        本次探测地址：<code>{{ chatStore.troubleshootGateBridgeUrl || '未获取（浏览器未注入桥地址）' }}</code>
      </p>
    </div>

    <!-- ===== Bridge 就绪，缺 SSH 会话 ===== -->
    <div v-else-if="chatStore.troubleshootGateStage === 'ssh-required'" class="gate-section">
      <el-alert type="info" :closable="false" class="gate-alert">
        <template #title>
          <strong>terminal_bridge 已就绪，但尚未连接 HCI 的 SSH</strong>
        </template>
        <p class="gate-desc">
          排障命令、SOP 步骤与日志采集都要通过这条 SSH 会话执行。登录成功后会自动继续刚才的操作。
        </p>
      </el-alert>

      <SshConnectSection
        bridge-status="running"
        mode="inline"
        :case-id="chatStore.troubleshootGateCaseId"
        :show-connect-button="true"
        :open-terminal="false"
        @connected="chatStore.confirmTroubleshootGate()"
      />
    </div>

    <template #footer>
      <div class="gate-footer">
        <el-button
          text
          type="primary"
          class="gate-offline-btn"
          @click="chatStore.chooseOfflineTroubleshoot()"
        >
          无 SSH 排障（离线诊断）
        </el-button>
        <el-button @click="chatStore.cancelTroubleshootGate()">暂不排障</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.gate-reason {
  margin: 0 0 12px;
  font-size: 13px;
  color: #606266;
}

.gate-checking {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px 0;
  color: #606266;
  font-size: 14px;
}

.gate-spin {
  animation: gate-spin 1s linear infinite;
}

@keyframes gate-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.gate-alert {
  margin-bottom: 16px;
}

.gate-desc {
  margin-top: 8px;
  line-height: 1.7;
}

/* 块内只有一个动作：按钮占满整块，避免"小按钮 + 右侧留白"的失衡观感 */
.gate-download {
  padding: 10px;
  background: #f5f7fa;
  border-radius: 8px;
  margin-bottom: 12px;
}

.gate-download-action {
  width: 100%;
  height: 40px;
  font-size: 14px;
}

.gate-recheck {
  text-align: center;
  margin-bottom: 8px;
}

.gate-endpoint {
  margin: 0;
  font-size: 12px;
  color: #909399;
  word-break: break-all;
}

.gate-endpoint code {
  color: #606266;
}

.gate-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

/* 「无 SSH 排障」是特性选项而不是降级：靠左、文字按钮，不与主流程抢视觉权重 */
.gate-offline-btn {
  padding-left: 0;
}
</style>
