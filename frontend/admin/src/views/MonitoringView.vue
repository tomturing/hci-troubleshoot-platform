<script setup lang="ts">
import { ref, onMounted } from 'vue'

/**
 * 系统监控 - 嵌入 Grafana 仪表盘
 * Docker Compose: http://localhost:3000
 * K3s: kubectl port-forward svc/grafana 3002:3000 -n hci-observability
 */
const grafanaUrl = ref('')
const grafanaReady = ref(false)
const loading = ref(true)

/** 获取 Grafana 地址 */
async function detectGrafana() {
  const hostname = window.location.hostname
  const protocol = window.location.protocol
  const port = window.location.port ? `:${window.location.port}` : ''

  if (hostname.startsWith('admin.')) {
    // 有域名部署：admin.<domain> -> grafana.<domain>
    const grafanaHost = hostname.replace('admin.', 'grafana.')
    grafanaUrl.value = `${protocol}//${grafanaHost}`
  } else if (hostname === 'localhost' || hostname === '127.0.0.1') {
    // Docker Compose 本地开发
    grafanaUrl.value = 'http://localhost:3000'
  } else {
    // IP 直接访问（K3s 生产环境）：通过 /grafana subpath 路由
    grafanaUrl.value = `${protocol}//${hostname}${port}/grafana`
  }

  grafanaReady.value = true
  loading.value = false
}

function openGrafana() {
  window.open(grafanaUrl.value, '_blank')
}

onMounted(detectGrafana)
</script>

<template>
  <div class="monitoring">
    <el-card>
      <template #header>
        <div class="monitor-header">
          <span>系统监控</span>
          <el-button type="primary" link @click="openGrafana">
            <el-icon><Monitor /></el-icon>
            在新窗口打开 Grafana
          </el-button>
        </div>
      </template>
      <div class="iframe-container">
        <iframe
          :src="grafanaUrl"
          width="100%"
          height="100%"
          frameborder="0"
          allowfullscreen
        />
      </div>
      <el-alert
        type="info"
        :closable="false"
        style="margin-top: 12px"
        description="如果 Grafana 页面不能正常加载，请确保 Grafana 服务已启动且允许 iframe 嵌入。可在 grafana.ini 中设置 allow_embedding = true。"
      />
    </el-card>
  </div>
</template>

<style scoped>
.monitor-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.iframe-container {
  width: 100%;
  height: calc(100vh - 250px);
  min-height: 500px;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  overflow: hidden;
}

.iframe-container iframe {
  display: block;
}
</style>
