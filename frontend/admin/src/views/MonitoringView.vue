<script setup lang="ts">
/**
 * 系统监控 - 嵌入 Grafana 仪表盘
 * Grafana 默认部署在 localhost:3000
 */
const grafanaUrl = 'http://localhost:3000'

/** 在新窗口打开 Grafana（Vue template 中不能直接访问 window） */
function openGrafana() {
  window.open(grafanaUrl, '_blank')
}
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
