<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'
import DshTicketSyncPanel from '@/components/DshTicketSyncPanel.vue'

const apiClient = createApiClient('/api', undefined, getAccessToken)
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
  <div class="dsh-container">
    <DshTicketSyncPanel />

    <el-card class="dsh-card">
      <div v-loading="dshLoading" class="dsh-content">
        <div class="dsh-actions-bar">
          <span class="dsh-info-text">嵌入式 DSH 诊断工作台（DeepSeek Harness）</span>
          <el-button type="primary" size="small" @click="loadDshUrl">
            <el-icon style="margin-right: 4px"><Refresh /></el-icon>
            重新连接
          </el-button>
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
.dsh-container {
  padding: 20px;
}

.dsh-card {
  border-radius: 8px;
}

.dsh-content {
  min-height: 620px;
}

.dsh-actions-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  background-color: #f5f7fa;
  border-radius: 6px;
  border-left: 4px solid #409eff;
}

.dsh-info-text {
  font-size: 14px;
  color: #606266;
  font-weight: 500;
}

.iframe-wrapper {
  width: 100%;
  height: calc(100vh - 375px);
  min-height: 480px;
  margin-top: 12px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 2px 12px 0 rgb(0 0 0 / 5%);
}

.iframe-wrapper iframe {
  display: block;
}

.dsh-alert {
  margin-top: 12px;
}
</style>
