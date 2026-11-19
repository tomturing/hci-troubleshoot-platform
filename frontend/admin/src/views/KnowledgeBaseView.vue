<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

/**
 * WeKnora 知识库（同源内嵌）。
 *
 * 两个入口都由后端 /api/weknora/launch 下发同源相对路径：
 * - 知识问答：官方 embed 渠道，链接里带后端换来的 30 分钟 ems_ 短令牌；
 * - 知识库管理：WeKnora 完整控制台，需在 WeKnora 内单独登录（平台 JWT 不透传）。
 *
 * 不在这里拼 URL、也不接触任何长期令牌：publish token 只存在于集群 Secret。
 */
interface WeknoraLaunch {
  console_url: string
  embed_url: string | null
}

const apiClient = createApiClient('/api', undefined, getAccessToken)

const activeTab = ref<'chat' | 'console'>('chat')
const consoleUrl = ref('/weknora/')
const embedUrl = ref<string | null>(null)
const loading = ref(true)
const error = ref('')

/** 问答渠道未开通时后端返回 null，这是待配置状态，不是故障。 */
const embedMissing = computed(() => !loading.value && !error.value && !embedUrl.value)

async function loadLaunch() {
  loading.value = true
  error.value = ''
  try {
    const response = await apiClient.get<WeknoraLaunch>('/weknora/launch')
    consoleUrl.value = response.data.console_url || '/weknora/'
    embedUrl.value = response.data.embed_url
  } catch (err: any) {
    const status = err?.response?.status
    if (status === 503) {
      error.value = '知识库服务正在启动或问答渠道暂不可用，请稍后点击「重新连接」。'
    } else if (status === 403) {
      error.value = '当前账号不具备管理员权限，无法访问知识库。'
    } else {
      error.value = '未能取得知识库入口，请稍后重试。'
    }
    embedUrl.value = null
  } finally {
    loading.value = false
  }
}

onMounted(loadLaunch)
</script>

<template>
  <div class="weknora-container">
    <el-card class="weknora-card">
      <div class="weknora-actions-bar">
        <span class="weknora-info-text">WeKnora 知识库（文档检索问答 + 知识库管理）</span>
        <el-button type="primary" size="small" @click="loadLaunch">
          <el-icon style="margin-right: 4px"><Refresh /></el-icon>
          重新连接
        </el-button>
      </div>

      <el-alert
        v-if="error"
        type="warning"
        :closable="false"
        class="weknora-alert"
        :description="error"
      />

      <el-tabs v-model="activeTab" class="weknora-tabs">
        <el-tab-pane label="知识问答" name="chat">
          <div v-loading="loading" class="weknora-frame">
            <!-- 短令牌会过期，切页签时用 key 强制重建 iframe 走一次重新取址 -->
            <iframe
              v-if="embedUrl && activeTab === 'chat'"
              :key="embedUrl"
              :src="embedUrl"
              title="WeKnora 知识问答"
              referrerpolicy="no-referrer"
              width="100%"
              height="100%"
              frameborder="0"
            />
            <el-empty
              v-else-if="embedMissing"
              description="尚未创建知识库问答渠道。请在「知识库管理」里新建知识库并创建网页嵌入渠道。"
            />
          </div>
        </el-tab-pane>

        <el-tab-pane label="知识库管理" name="console">
          <div class="weknora-frame">
            <iframe
              v-if="activeTab === 'console'"
              :src="consoleUrl"
              title="WeKnora 知识库管理"
              referrerpolicy="no-referrer"
              width="100%"
              height="100%"
              frameborder="0"
              allow="clipboard-read; clipboard-write"
            />
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.weknora-container {
  padding: 20px;
}

.weknora-card {
  border-radius: 8px;
}

.weknora-actions-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  background-color: #f5f7fa;
  border-radius: 6px;
  border-left: 4px solid #409eff;
}

.weknora-info-text {
  font-size: 14px;
  color: #606266;
  font-weight: 500;
}

.weknora-alert {
  margin-top: 12px;
}

.weknora-tabs {
  margin-top: 8px;
}

.weknora-frame {
  width: 100%;
  height: calc(100vh - 360px);
  min-height: 480px;
  margin-top: 8px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 2px 12px 0 rgb(0 0 0 / 5%);
}

.weknora-frame iframe {
  display: block;
}
</style>
