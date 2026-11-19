<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { FullScreen, Refresh } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'
import DshTicketSyncPanel from '@/components/DshTicketSyncPanel.vue'
import { useFullscreen } from '@/composables/useFullscreen'

// 新版 DSH 独立实例的 launch token 仍由网关以管理员身份代持下发；浏览器不直接
// 访问集群 Service，也不把 token 固化到前端静态资源。
const apiClient = createApiClient('/api', undefined, getAccessToken)

// 全屏作用于整个页面容器（含工具栏），退出入口始终可见
const pageRef = ref<HTMLElement | null>(null)
const { isFullscreen, supported: fullscreenSupported, toggle: toggleFullscreen } = useFullscreen(pageRef)
const dshUrl = ref('')
const dshReady = ref(false)
const dshLoading = ref(true)
const dshError = ref('')

async function loadDshUrl() {
  dshLoading.value = true
  dshError.value = ''
  try {
    const response = await apiClient.get('/dsh-offline/launch')
    dshUrl.value = response.data.url
    dshReady.value = true
  } catch (error: any) {
    const status = error?.response?.status
    if (status === 503) {
      dshError.value = 'HCI 单 Agent DSH 正在启动，尚未生成会话凭据，请稍后点击「重新连接」。'
    } else if (status === 403) {
      dshError.value = '当前账号不具备管理员权限，无法取得 HCI 单 Agent DSH 会话凭据。'
    } else {
      dshError.value = '未能取得 HCI 单 Agent DSH 会话入口，请稍后重试。'
    }
    dshReady.value = Boolean(dshUrl.value)
  } finally {
    dshLoading.value = false
  }
}

onMounted(loadDshUrl)
</script>

<template>
  <div ref="pageRef" class="dsh-offline-container">
    <DshTicketSyncPanel />
    <el-card class="dsh-offline-card">
      <div v-loading="dshLoading" class="dsh-offline-content">
        <div class="dsh-actions-bar">
          <div>
            <div class="dsh-title">HCI 单 Agent 离线诊断</div>
            <div class="dsh-subtitle">deepseek-harness-wecom + session-bi + HCI 离线诊断</div>
          </div>
          <div class="dsh-offline-actions">
            <el-button v-if="fullscreenSupported" size="small" @click="toggleFullscreen">
              <el-icon style="margin-right: 4px"><FullScreen /></el-icon>
              {{ isFullscreen ? '退出全屏' : '全屏' }}
            </el-button>
            <el-button type="primary" size="small" @click="loadDshUrl">
              <el-icon style="margin-right: 4px"><Refresh /></el-icon>
              重新连接
            </el-button>
          </div>
        </div>

        <div class="iframe-wrapper">
          <iframe
            v-if="dshReady"
            :src="dshUrl"
            referrerpolicy="no-referrer"
            width="100%"
            height="100%"
            frameborder="0"
            allow="clipboard-read; clipboard-write"
            title="HCI 单 Agent 离线诊断 DSH"
          />
        </div>

        <el-alert
          v-if="dshError"
          type="warning"
          :closable="false"
          class="dsh-alert"
          :description="dshError"
        />
        <el-alert
          v-else
          type="info"
          :closable="false"
          class="dsh-alert"
          description="该页面连接独立的 HCI 单 Agent DSH 实例，使用版本化知识、Session BI 和只读工单支持包进行离线诊断。"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
/* 高度链同 DshDiagnosisView：flex 逐级传递，保留外层滚动条作为溢出兜底。 */
.dsh-offline-container {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.dsh-offline-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-radius: 8px;
}

:deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.dsh-offline-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.dsh-actions-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 14px;
  margin-bottom: 12px;
  border-left: 4px solid #67c23a;
  background: #f8fff5;
  flex: none;
}

.dsh-title {
  color: #303133;
  font-size: 15px;
  font-weight: 600;
}

.dsh-subtitle {
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
}

.dsh-offline-actions {
  display: flex;
  gap: 8px;
  flex: none;
}

/* 原生全屏下容器脱离文档流，必须自带背景色 */
.dsh-offline-container:fullscreen {
  background: #f0f2f5;
  padding: 16px;
  box-sizing: border-box;
}

.iframe-wrapper {
  flex: 1;
  width: 100%;
  min-height: 520px;
  overflow: hidden;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  box-shadow: 0 2px 12px 0 rgb(0 0 0 / 5%);
}

.iframe-wrapper iframe {
  display: block;
  border: 0;
}

.dsh-alert {
  margin-top: 12px;
  flex: none;
}
</style>
