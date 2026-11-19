<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { FullScreen, Refresh } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'
import { useFullscreen } from '@/composables/useFullscreen'

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

// 全屏作用于整个页面容器：工具栏与页签一起留在全屏里，退出入口不会消失。
const pageRef = ref<HTMLElement | null>(null)
const { isFullscreen, supported: fullscreenSupported, toggle: toggleFullscreen } = useFullscreen(pageRef)

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
  <div ref="pageRef" class="weknora-container">
    <el-card class="weknora-card">
      <div class="weknora-actions-bar">
        <span class="weknora-info-text">WeK知识库（文档检索问答 + 知识库管理）</span>
        <div class="weknora-actions">
          <el-button v-if="fullscreenSupported" size="small" @click="toggleFullscreen">
            <el-icon style="margin-right: 4px"><FullScreen /></el-icon>
            {{ isFullscreen ? '退出全屏' : '全屏' }}
          </el-button>
          <el-button type="primary" size="small" @click="loadLaunch">
            <el-icon style="margin-right: 4px"><Refresh /></el-icon>
            重新连接
          </el-button>
        </div>
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
/*
 * 高度链：el-main（flex:1，20px padding）→ 本容器 → el-card → tabs → iframe。
 *
 * 不用 calc(100vh - Npx)：N 是拍出来的魔法数，工具栏/页签/告警任一高度变化
 * 就会错位——要么底部留白，要么页面出现第二条滚动条（iframe 内部已有自己的
 * 滚动）。改成 flex 逐级传递可用高度后，iframe 恒等于「剩下的全部空间」。
 *
 * 注意 el-main 本身不能改成 overflow:hidden：同层还有大量普通表单页依赖它
 * 滚动，改全局布局会把长页面裁掉。这里只让内嵌页自己不吃满不溢出。
 */
.weknora-container {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.weknora-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-radius: 8px;
}

/* 必须限定到本页卡片：:deep 会穿透所有子组件，无前缀会连工具栏里嵌套的
   el-card（如工单同步面板）一起改掉 */
.weknora-card :deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  /* 默认 20px 的上下内边距在内嵌场景是纯浪费，收紧到 12px 直接还给 iframe */
  padding: 12px;
}

.weknora-actions-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  background-color: #f5f7fa;
  border-radius: 6px;
  border-left: 4px solid #409eff;
  flex: none;
}

.weknora-info-text {
  font-size: 14px;
  color: #606266;
  font-weight: 500;
}

.weknora-actions {
  display: flex;
  gap: 8px;
  flex: none;
}

/* 原生全屏下容器脱离文档流，必须自带背景色，否则透出浏览器默认黑/白底 */
.weknora-container:fullscreen {
  background: #f0f2f5;
  padding: 12px;
  box-sizing: border-box;
}

.weknora-alert {
  margin-top: 12px;
  flex: none;
}

.weknora-tabs {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  margin-top: 8px;
}

:deep(.weknora-tabs .el-tabs__header) {
  flex: none;
  margin-bottom: 0;
}

:deep(.weknora-tabs .el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

:deep(.weknora-tabs .el-tab-pane) {
  height: 100%;
}

.weknora-frame {
  width: 100%;
  height: 100%;
  min-height: 0;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 2px 12px 0 rgb(0 0 0 / 5%);
}

.weknora-frame iframe {
  display: block;
  border: 0;
}
</style>
