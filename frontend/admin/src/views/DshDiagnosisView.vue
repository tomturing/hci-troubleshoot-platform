<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

// 管理端 axios 客户端注入登录 JWT（axios 不经 window.fetch 全局拦截器，须显式传入）
const apiClient = createApiClient('/api', undefined, getAccessToken)

// ==========================================
// DSH（DeepSeek Harness）诊断工作台
// ==========================================
// 与 Grafana / Langfuse 的关键差异：DSH 没有匿名访问模式，进入其 Web UI 必须
// 携带一次性的 launch token 完成会话交换，而该 token 由 DSH 进程随机生成、
// 每次重建都会变化，因此不能像 Grafana 那样在前端按 hostname 推导，
// 必须向后端（经管理员 JWT 鉴权）实时索取。
const dshUrl = ref('')
const dshReady = ref(false)
const dshLoading = ref(true)
const dshError = ref('')

async function loadDshUrl() {
  dshLoading.value = true
  dshError.value = ''
  try {
    const res = await apiClient.get('/dsh/launch')
    dshUrl.value = res.data.url
    dshReady.value = true
  } catch (error: any) {
    const status = error?.response?.status
    if (status === 503) {
      // DSH 进程刚重建时尚未打印就绪行、token 还没落盘，属于可恢复的中间态
      dshError.value = 'DSH 服务正在启动，尚未生成会话凭据，请稍后点击「重新连接」。'
    } else if (status === 403) {
      dshError.value = '当前账号不具备管理员权限，无法取得 DSH 会话凭据。'
    } else {
      dshError.value = '未能取得 DSH 会话入口，请稍后重试。'
    }
    // 保留旧地址不清空：让 iframe 继续展示上一次的会话，避免页面整体塌陷
    dshReady.value = Boolean(dshUrl.value)
  } finally {
    dshLoading.value = false
  }
}

onMounted(loadDshUrl)
</script>

<template>
  <div class="dsh-container">
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
          <!--
            referrerpolicy=no-referrer：会话入口地址内含 DSH 的 launch token，
            该凭据等价于 Agent 的完整控制权，必须阻止浏览器经 Referer 头
            把它带给任何第三方。
          -->
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

/* 操作条样式与 ObservabilityView 保持一致，保证视觉连贯 */
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
  height: calc(100vh - 260px);
  min-height: 520px;
  margin-top: 12px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.05);
}

.iframe-wrapper iframe {
  display: block;
}

.dsh-alert {
  margin-top: 12px;
}
</style>
