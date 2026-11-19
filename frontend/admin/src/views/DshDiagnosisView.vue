<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { FullScreen, Refresh } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'
import DshTicketSyncPanel from '@/components/DshTicketSyncPanel.vue'
import { useFullscreen } from '@/composables/useFullscreen'

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
    const response = await apiClient.get('/dsh/launch')
    dshUrl.value = response.data.url
    dshReady.value = true
  } catch (error: any) {
    const status = error?.response?.status
    if (status === 503) {
      dshError.value = 'DSH 服务正在启动，尚未生成会话凭据，请稍后点击「重新连接」。'
    } else if (status === 403) {
      dshError.value = '当前账号不具备管理员权限，无法取得 DSH 会话凭据。'
    } else {
      dshError.value = '未能取得 DSH 会话入口，请稍后重试。'
    }
    dshReady.value = Boolean(dshUrl.value)
  } finally {
    dshLoading.value = false
  }
}

onMounted(loadDshUrl)
</script>

<template>
  <div ref="pageRef" class="dsh-container">
    <DshTicketSyncPanel />

    <el-card class="dsh-card">
      <div v-loading="dshLoading" class="dsh-content">
        <div class="dsh-actions-bar">
          <span class="dsh-info-text">嵌入式 DSH 诊断工作台（DeepSeek Harness）</span>
          <div class="dsh-actions">
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
            allowfullscreen
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
          description="DSH 以只读方式挂载宿主机 /sf/log，用于分析客户 HCI 故障日志压缩包。会话凭据由管理端下发且具备完整操作权限，请勿向外部分享页面地址。"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
/*
 * 高度链与知识库页一致：容器 → 卡片 → 内容 → iframe 逐级 flex，取代
 * calc(100vh - Npx) 的魔法数（工具栏高度一改就错位）。
 * 与知识库页的差别：外层滚动条保留（用户明确要求），因此这里不隐藏溢出，
 * min-height 只用于兜底，避免 iframe 在高度未解析时塌成 0。
 */
.dsh-container {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.dsh-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-radius: 8px;
}

/* 限定到本页卡片：:deep 会穿透子组件，无前缀会改到工单同步面板里的 el-card */
.dsh-card :deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.dsh-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.dsh-actions-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  background-color: #f5f7fa;
  border-radius: 6px;
  border-left: 4px solid #409eff;
  flex: none;
}

.dsh-info-text {
  font-size: 14px;
  color: #606266;
  font-weight: 500;
}

.dsh-actions {
  display: flex;
  gap: 8px;
  flex: none;
}

/* 原生全屏下容器脱离文档流，必须自带背景色 */
.dsh-container:fullscreen {
  background: #f0f2f5;
  padding: 16px;
  box-sizing: border-box;
}

.iframe-wrapper {
  flex: 1;
  width: 100%;
  min-height: 480px;
  margin-top: 12px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  overflow: hidden;
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
