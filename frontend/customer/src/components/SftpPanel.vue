<script setup lang="ts">
/**
 * SftpPanel.vue
 * 右栏「文件下载」抽屉（对齐 Xshell / MobaXterm 的 SFTP 语义）。
 *
 * 定位：与「SSH 终端」平级的独立页签，不再嵌在终端底部。理由是二者都是
 * 「对着同一台主机的一次操作」，但查看文件列表时终端输出并不需要同时可见；
 * 塞在终端下发条里等于把一块独立工作区压成一条 24px 高的折叠条。
 *
 * 关键语义：文件从远端主机**直接**落到 terminal_bridge 所在机器
 * （桌面模式=工程师本机；集群模式=Pod 的持久卷），不经浏览器中转。
 * 因此这里只展示元数据：目录项、进度、落地路径，绝不搬运文件字节。
 *
 * 登录门禁在 store 的 openSftpPanel() 里（未登录先弹统一 SSH 登录框），
 * 本组件只兜底"进来之后会话掉了"的情况，仍然复用同一个登录入口。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  ArrowUp,
  CopyDocument,
  Document,
  Download,
  Folder,
  Refresh,
} from '@element-plus/icons-vue'
import { useChatStore } from '@/stores/chat'
import type { SftpEntry } from '@/api/terminal'

const chatStore = useChatStore()

// 路径输入框与“当前目录”分开维护：用户可以先把路径敲完再回车跳转，
// 若直接双向绑定到 sftpRemoteDir，列表刷新会打断正在输入的路径。
const pathInput = ref('')
watch(
  () => chatStore.sftpRemoteDir,
  (dir) => {
    pathInput.value = dir || ''
  },
  { immediate: true },
)

// 手动输入的文件绝对路径：SFTP 子系统被禁用的节点上，这是唯一可用的下载方式
const manualPath = ref('')

const entries = computed<SftpEntry[]>(() => chatStore.sftpEntries)
const transfer = computed(() => chatStore.sftpTransfer)
const listLoading = computed(() => chatStore.sftpListLoading)
const listError = computed(() => chatStore.sftpListError)

/** 会话是否在线：文件下载的一切动作都依赖它（bridge 复用同一个 SSH 连接） */
const connected = computed(() => chatStore.sshConnectionState === 'connected')

/** 页签是否真正可见：隐藏时不该发目录请求，也不该刷新列表 */
const active = computed(
  () => chatStore.isRightPanelOpen && chatStore.rightPanelTab === 'sftp',
)

/** 是否有下载正在进行（done 即终态，允许再次发起） */
const transferring = computed(() => Boolean(transfer.value) && transfer.value?.phase !== 'done')

// 首次进入页签（或重连后目录被清空）自动拉一次默认目录。
// 只监听「可见 + 连接状态」两个信号，不会因列表内容变化而循环请求。
watch(
  () => [active.value, connected.value] as const,
  ([visible, online]) => {
    if (visible && online && !chatStore.sftpEntries.length && !chatStore.sftpListLoading) {
      void chatStore.sftpList('')
    }
  },
  { immediate: true },
)

const transferStatusText = computed(() => {
  const current = transfer.value
  if (!current) return ''
  if (current.phase === 'preparing') return '准备中…'
  if (current.phase === 'downloading') return `下载中 ${current.percent}%`
  switch (current.status) {
    case 'ok':
      return '下载完成'
    case 'exists':
      return '本机已存在同名同大小文件，已跳过'
    case 'cancelled':
      return '已取消（已下载部分保留，可重试续传）'
    case 'error':
      return current.errorMessage || '下载失败'
    default:
      return '已结束'
  }
})

const transferFailed = computed(() => transfer.value?.status === 'error')

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '-'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`
}

function formatTime(raw?: string): string {
  if (!raw) return ''
  const parsed = new Date(raw)
  if (Number.isNaN(parsed.getTime())) return raw
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
}

function entryTooltip(entry: SftpEntry): string {
  const parts = [entry.path]
  if (entry.is_dir) parts.push('目录')
  else parts.push(formatSize(entry.size))
  if (entry.mode) parts.push(entry.mode)
  const time = formatTime(entry.mod_time)
  if (time) parts.push(time)
  return parts.join(' · ')
}

async function copyText(text: string, hint: string) {
  if (!text) return
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.cssText = 'position:fixed;opacity:0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success(hint)
  } catch {
    ElMessage.warning(`${hint}失败，请手动复制：${text}`)
  }
}

/**
 * 会话掉线后重新登录：直接复用 store 的登录门禁，
 * 与「SSH 终端」页签点击登录走的是同一个弹框，不另做一套表单。
 */
function requestLogin() {
  void chatStore.openSftpPanel()
}

function refresh() {
  void chatStore.sftpList(chatStore.sftpRemoteDir)
}

function goParent() {
  const parent = chatStore.sftpParentDir
  if (!parent) return
  void chatStore.sftpList(parent)
}

function goToPath() {
  const target = pathInput.value.trim()
  if (!target) {
    // 空路径让 bridge 用远端 home：用户清空输入框就能回到默认目录
    void chatStore.sftpList('')
    return
  }
  void chatStore.sftpList(target)
}

function openDir(entry: SftpEntry) {
  if (!entry.is_dir) return
  void chatStore.sftpList(entry.path)
}

function downloadEntry(entry: SftpEntry) {
  if (entry.is_dir) {
    openDir(entry)
    return
  }
  if (transferring.value) {
    ElMessage.warning('已有下载任务在进行，请等待完成或先取消')
    return
  }
  chatStore.sftpDownload(entry.path)
}

function downloadManual() {
  const target = manualPath.value.trim()
  if (!target) {
    ElMessage.warning('请输入远端文件绝对路径')
    return
  }
  if (transferring.value) {
    ElMessage.warning('已有下载任务在进行，请等待完成或先取消')
    return
  }
  chatStore.sftpDownload(target)
}

function cancelDownload() {
  chatStore.sftpCancelDownload()
}

function useRemoteDirAsManual() {
  const dir = chatStore.sftpRemoteDir
  if (!dir || manualPath.value.trim()) return
  // 只做前缀提示，不替用户猜文件名：猜错会直接报“文件不存在”
  manualPath.value = dir.endsWith('/') ? dir : `${dir}/`
}
</script>

<template>
  <div class="sftp-panel">
    <!-- 未登录兜底：入口处已用统一登录弹框拦截，这里覆盖"进来之后会话断了" -->
    <div v-if="!connected" class="sftp-offline">
      <el-icon class="sftp-offline__icon"><Download /></el-icon>
      <p class="sftp-offline__title">文件下载需要先登录 SSH</p>
      <p class="sftp-offline__hint">下载走的是同一条 SSH 会话，不额外认证</p>
      <el-button type="primary" @click="requestLogin">连接 SSH</el-button>
    </div>

    <template v-else>
      <!-- 落地目录：本功能唯一的交付凭据，必须一直可见 -->
      <div class="sftp-landing">
        <span class="sftp-landing__label">保存到本机</span>
        <span class="sftp-landing__path" :title="chatStore.sftpDownloadDir">
          {{ chatStore.sftpDownloadDir || '由 bridge 决定（连接后自动获取）' }}
        </span>
        <el-button
          text
          size="small"
          :disabled="!chatStore.sftpDownloadDir"
          title="复制落地目录"
          @click="copyText(chatStore.sftpDownloadDir, '落地目录已复制')"
        >
          <el-icon><CopyDocument /></el-icon>
        </el-button>
        <el-button text size="small" :loading="listLoading" title="刷新当前目录" @click="refresh">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>

      <div class="sftp-body">
        <!-- 路径导航 -->
        <div class="sftp-pathbar">
          <el-input
            v-model="pathInput"
            size="small"
            class="sftp-pathbar__input"
            placeholder="远端目录，留空=登录用户 home"
            @keydown.enter.prevent="goToPath"
          />
          <el-button
            text
            size="small"
            :disabled="!chatStore.sftpParentDir"
            title="上级目录"
            @click="goParent"
          >
            <el-icon><ArrowUp /></el-icon>
          </el-button>
          <el-button text size="small" @click="goToPath">转到</el-button>
        </div>

        <!-- 手动路径下载：SFTP 子系统不可用时，这是唯一入口 -->
        <div class="sftp-manual">
          <el-input
            v-model="manualPath"
            size="small"
            class="sftp-manual__input"
            placeholder="或直接输入文件绝对路径，如 /var/log/messages"
            @keydown.enter.prevent="downloadManual"
            @focus="useRemoteDirAsManual"
          />
          <el-button type="primary" size="small" :disabled="transferring" @click="downloadManual">
            下载
          </el-button>
        </div>

        <p v-if="listError" class="sftp-error">{{ listError }}</p>

        <!-- 传输状态：进度条 + 落地路径 + 取消 -->
        <div v-if="transfer" class="sftp-transfer" :class="{ 'is-error': transferFailed }">
          <div class="sftp-transfer__head">
            <span class="sftp-transfer__status">{{ transferStatusText }}</span>
            <el-tag v-if="transfer.transport" size="small" effect="plain" type="info">
              {{ transfer.transport === 'sftp' ? 'SFTP' : 'tail 回退' }}
            </el-tag>
            <el-button
              v-if="transferring"
              text
              size="small"
              class="sftp-transfer__cancel"
              @click="cancelDownload"
            >
              取消
            </el-button>
          </div>
          <el-progress
            :percentage="transfer.percent"
            :stroke-width="6"
            :show-text="false"
            :status="transferFailed ? 'exception' : transfer.status === 'ok' ? 'success' : undefined"
          />
          <div class="sftp-transfer__numbers">
            <span>{{ formatSize(transfer.bytes) }} / {{ formatSize(transfer.totalBytes) }}</span>
            <span v-if="transfer.sha256" class="sftp-transfer__sha" :title="transfer.sha256">
              sha256 {{ transfer.sha256.slice(0, 12) }}…
            </span>
          </div>
          <div v-if="transfer.localPath" class="sftp-transfer__local">
            <span class="sftp-transfer__local-label">落地文件</span>
            <span class="sftp-transfer__local-path" :title="transfer.localPath">
              {{ transfer.localPath }}
            </span>
            <el-button
              text
              size="small"
              title="复制落地文件路径"
              @click="copyText(transfer.localPath, '文件路径已复制')"
            >
              <el-icon><CopyDocument /></el-icon>
            </el-button>
          </div>
        </div>

        <!-- 目录列表 -->
        <div class="sftp-list">
          <div v-if="listLoading && !entries.length" class="sftp-list__empty">正在读取目录…</div>
          <div v-else-if="!entries.length" class="sftp-list__empty">
            {{ listError ? '目录未加载' : '目录为空，或请输入绝对路径' }}
          </div>
          <div
            v-for="entry in entries"
            :key="entry.path"
            class="sftp-row"
            :class="{ 'is-dir': entry.is_dir }"
          >
            <el-tooltip :content="entryTooltip(entry)" placement="top" :show-after="400">
              <button
                type="button"
                class="sftp-row__name"
                @click="entry.is_dir ? openDir(entry) : downloadEntry(entry)"
              >
                <el-icon class="sftp-row__icon">
                  <Folder v-if="entry.is_dir" />
                  <Document v-else />
                </el-icon>
                <span class="sftp-row__text">{{ entry.name }}</span>
              </button>
            </el-tooltip>
            <span class="sftp-row__size">{{ entry.is_dir ? '' : formatSize(entry.size) }}</span>
            <el-button
              v-if="!entry.is_dir"
              text
              size="small"
              class="sftp-row__download"
              :disabled="transferring"
              title="下载到本机"
              @click="downloadEntry(entry)"
            >
              <el-icon><Download /></el-icon>
            </el-button>
          </div>
          <p v-if="chatStore.sftpListTruncated" class="sftp-list__truncated">
            目录项过多，仅显示前 2000 项；建议直接输入目标文件路径下载
          </p>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/*
 * 面板占满整个右栏主体（原来是终端下方的可折叠条）。
 * 深色是右栏的身份色，与 SSH 终端保持一致，不随页签切换而闪烁。
 */
.sftp-panel {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #2d2d2d;
  color: #cfcfcf;
  font-size: 12px;
}

.sftp-offline {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 24px;
  text-align: center;
}

.sftp-offline__icon {
  font-size: 32px;
  color: #6b6b6b;
}

.sftp-offline__title {
  margin: 0;
  font-size: 14px;
  color: #e0e0e0;
}

.sftp-offline__hint {
  margin: 0 0 4px;
  color: #8a8a8a;
}

.sftp-landing {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px;
  border-bottom: 1px solid #333;
  flex-shrink: 0;
}

.sftp-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px 10px;
  /* 目录列表可能与视口等高：自己滚动，不参与外层 flex 抢高度 */
  overflow-y: auto;
}

.sftp-pathbar,
.sftp-manual {
  display: flex;
  align-items: center;
  gap: 4px;
}

.sftp-landing__label,
.sftp-transfer__local-label {
  color: #8a8a8a;
  flex-shrink: 0;
}

.sftp-landing__path,
.sftp-transfer__local-path {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #ce9178;
  /* 不用 direction: rtl 把末尾露出来：路径以 / 开头的符号会在双向算法里错位 */
  text-align: left;
}

.sftp-pathbar__input,
.sftp-manual__input {
  flex: 1;
  min-width: 0;
}

.sftp-pathbar :deep(.el-input__wrapper),
.sftp-manual :deep(.el-input__wrapper) {
  background: #3a3a3a;
  box-shadow: none;
  border: 1px solid #555;
}

.sftp-pathbar :deep(.el-input__inner),
.sftp-manual :deep(.el-input__inner) {
  color: #e0e0e0;
  font-size: 12px;
}

.sftp-error {
  margin: 0;
  color: #f56c6c;
  line-height: 1.4;
}

.sftp-transfer {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 8px;
  border: 1px solid #3c3c3c;
  border-radius: 6px;
  background: #262626;
}

.sftp-transfer.is-error {
  border-color: #7a3b3b;
}

.sftp-transfer__head {
  display: flex;
  align-items: center;
  gap: 6px;
}

.sftp-transfer__status {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sftp-transfer.is-error .sftp-transfer__status {
  color: #f56c6c;
}

.sftp-transfer__numbers {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: #8a8a8a;
}

.sftp-transfer__sha {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sftp-transfer__local {
  display: flex;
  align-items: center;
  gap: 4px;
}

.sftp-list {
  display: flex;
  flex-direction: column;
}

.sftp-list__empty {
  padding: 8px 0;
  color: #8a8a8a;
  text-align: center;
}

.sftp-list__truncated {
  margin: 4px 0 0;
  color: #d7ba7d;
  line-height: 1.4;
}

.sftp-row {
  display: flex;
  align-items: center;
  gap: 6px;
  border-bottom: 1px solid #333;
}

.sftp-row:last-child {
  border-bottom: none;
}

.sftp-row__name {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  padding: 4px 0;
  background: transparent;
  border: none;
  color: inherit;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}

.sftp-row__name:hover .sftp-row__text {
  color: #9cdcfe;
  text-decoration: underline;
}

.sftp-row.is-dir .sftp-row__icon {
  color: #d7ba7d;
}

.sftp-row__icon {
  color: #8a8a8a;
  flex-shrink: 0;
}

.sftp-row__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sftp-row__size {
  color: #8a8a8a;
  flex-shrink: 0;
}

.sftp-row__download {
  color: #9cdcfe;
  padding: 0 4px;
}
</style>
