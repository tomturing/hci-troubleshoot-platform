<script setup lang="ts">
import { onMounted } from 'vue'
import { useChatStore } from '@/stores/chat'
import { getClientId } from '@/utils/clientId'
import ChatWindow from '@/components/ChatWindow.vue'

const chatStore = useChatStore()
const clientId = getClientId()

onMounted(() => {
  chatStore.initialize()
})
</script>

<template>
  <div class="app-container">
    <header class="app-header">
      <div class="header-content">
        <h1>HCI 故障排查助手</h1>
        <div class="header-badges">
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

.app-main {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
</style>
