<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Download, Finished, FolderOpened, Monitor, Tickets } from '@element-plus/icons-vue'
import { useChatStore } from '@/stores/chat'
import { getClientId } from '@/utils/clientId'
import ChatWindow from '@/components/ChatWindow.vue'
import HistoryPanel from '@/components/HistoryPanel.vue'
import LogCollectCaseDialog from '@/components/LogCollectCaseDialog.vue'
import LogCollectPanel from '@/components/LogCollectPanel.vue'
import PanelShell from '@/components/PanelShell.vue'
import SftpPanel from '@/components/SftpPanel.vue'
import SshConnectDialog from '@/components/SshConnectDialog.vue'
import TerminalPanel from '@/components/TerminalPanel.vue'

const chatStore = useChatStore()
const clientId = getClientId()

/**
 * 工作区外壳。
 *
 * 三块内容是 flex 兄弟节点（面板 / 会话 / 面板），宽度直接来自求解器：
 * 没有抽屉、没有主区 padding 让位、没有遮罩穿透的 hack。
 * 外壳只提供一个变量——中央内容列的可读宽度上限。
 */
const shellStyle = computed(() => ({
  '--ws-center-max': `${chatStore.workspaceLayout.centerMaxWidth}px`,
}))

/** 窄屏降级：面板改为覆盖式，此时才需要遮罩 */
const isOverlayMode = computed(() => chatStore.workspaceLayout.mode === 'overlay')

/**
 * 顶栏紧凑态。
 *
 * 只看视口宽度，不看中央会话宽度：顶栏是「应用框架」，横跨三栏、宽度恒定，
 * 而 centerMaxWidth 会随面板开合在 460~900 之间变化。早先用它做判定，
 * 结果是打开两个面板（中央 560）时顶栏按钮全部退化成图标——
 * 明明窗口还有 1920px 可用，纯属把内容列的约束错套到了框架上。
 */
const isHeaderCompact = computed(() => chatStore.viewportWidth < 900)

/**
 * 拖拽调宽：只写"偏好宽度"，由求解器重新算当前宽度。
 *
 * 直接写当前宽度会在窗口变窄后留下一个越界固定值，把中央会话挤到不可读；
 * 写偏好则始终能被 clamp 回合法区间。
 */
function startPanelResize(side: 'left' | 'right', e: MouseEvent) {
  e.preventDefault()
  const startX = e.clientX
  const startWidth = side === 'left' ? chatStore.leftPanelWidth : chatStore.rightPanelWidth
  const apply = side === 'left' ? chatStore.setLeftPanelPreferred : chatStore.setRightPanelPreferred
  chatStore.setPanelResizing(true)
  const onMouseUp = () => {
    chatStore.setPanelResizing(false)
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
  }
  const onMouseMove = (moveE: MouseEvent) => {
    // 鼠标在窗口外松开时收不到 mouseup，靠按键状态兜底复位，否则过渡会被永久关闭
    if (moveE.buttons === 0) {
      onMouseUp()
      return
    }
    // 左侧手柄往右拖是变宽，右侧手柄往左拖是变宽
    const delta = side === 'left' ? moveE.clientX - startX : startX - moveE.clientX
    apply(startWidth + delta)
  }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
}

/** 窄屏覆盖态点空白处：先收右侧（终端在上层），没有再收左侧 */
function collapseOverlayPanel() {
  if (chatStore.isRightPanelOpen) {
    chatStore.closeTerminalSidebar()
    return
  }
  chatStore.collapseLeftPanel()
}

/** 从右栏终端打开当前工单的终端历史回放 */
function openCurrentCaseTerminalHistory() {
  // 优先当前聊天工单，其次日志收集绑定的工单：无聊天工单时也能看采集会话的回放
  const caseId = chatStore.currentCase?.case_id ?? chatStore.logCollectCaseId ?? null
  chatStore.openTerminalReplay(caseId)
}

// 视口变化时重新求解；rAF 合并同一帧内的连续 resize，避免布局抖动
const viewportResizing = ref(false)
let resizeFrame = 0
let resizeIdleTimer = 0
function handleViewportResize() {
  // 拖动窗口边缘时抽屉宽度是瞬变的（el-drawer 的 size 没有过渡），
  // 若主区让位仍跑 300ms 过渡，两者会持续错位，所以窗口 resize 期间也关掉过渡。
  viewportResizing.value = true
  if (resizeIdleTimer) window.clearTimeout(resizeIdleTimer)
  resizeIdleTimer = window.setTimeout(() => {
    viewportResizing.value = false
  }, 160)
  if (resizeFrame) return
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = 0
    chatStore.setViewportWidth(window.innerWidth)
  })
}
// 抽屉开合收敛到 store：建单后的引导卡片也通过同一状态打开面板，
// 避免 App.vue 与 ChatWindow 各持一份本地 ref 导致两处入口行为不一致。
const logCollectVisible = computed({
  get: () => chatStore.logCollectVisible,
  set: (visible: boolean) => (visible ? chatStore.openLogCollect() : chatStore.closeLogCollect()),
})

// Bridge 下载地址：优先读环境变量，回退到相对路径
// 部署时在 .env 中配置 VITE_BRIDGE_DOWNLOAD_URL 指向实际文件
const BRIDGE_DOWNLOAD_URL =
  import.meta.env.VITE_BRIDGE_DOWNLOAD_URL || '/downloads/terminal_bridge.exe'

onMounted(() => {
  // 首帧就要有正确的视口宽度，否则抽屉会按 SSR 兜底值布局一次再跳变
  chatStore.setViewportWidth(window.innerWidth)
  chatStore.initialize()
  window.addEventListener('resize', handleViewportResize)
})

onBeforeUnmount(() => {
  if (resizeFrame) cancelAnimationFrame(resizeFrame)
  if (resizeIdleTimer) window.clearTimeout(resizeIdleTimer)
  window.removeEventListener('resize', handleViewportResize)
})

/**
 * 触发 terminal_bridge.exe 下载
 * 用隐藏 <a download> 替代 window.open，避免浏览器弹窗拦截策略
 */
function handleDownloadBridge() {
  chatStore.closeBridgeDownload()

  const a = document.createElement('a')
  a.href = BRIDGE_DOWNLOAD_URL
  a.download = 'terminal_bridge.exe'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  // 延迟移除，确保点击事件已触发
  setTimeout(() => document.body.removeChild(a), 200)
}

</script>

<template>
  <div
    class="workspace-shell"
    :class="{ 'is-resizing': chatStore.isPanelResizing || viewportResizing }"
    :style="shellStyle"
  >
    <header class="app-header" :class="{ 'is-compact': isHeaderCompact }">
      <!-- 品牌区：通栏左对齐。顶栏横跨三栏，不跟随中央内容列限宽 -->
      <div class="header-brand">
        <!--
          品牌图标与标题必须同义：本产品是「助手」，图标要指向"助手/AI"而不是"人工操作"。

          FirstAidKit 是急救箱（医疗语义）、Tools 是齿轮（设置语义）、Search 是放大镜（搜索语义），
          三者都在描述"人做的事"，与「助手」这个主体对不上；用户明确要求换成机器人。

          Element Plus 没有机器人图标，这里内联一支 24 栅格的机器人头：
          天线 + 圆角头部 + 两颗眼睛 + 嘴线，线宽 1.8，与同栏 Element Plus 线性图标描边一致。
          不套 el-icon：它的 `svg { fill: currentColor }` 会把 stroke 图标填成一团黑。
        -->
        <span class="brand-mark" aria-hidden="true">
          <svg
            class="brand-mark__bot"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M12 6V3.6" />
            <circle cx="12" cy="2.6" r="1.1" fill="currentColor" stroke="none" />
            <rect x="4" y="6" width="16" height="12" rx="3.6" />
            <path d="M9.6 10.6v2.2M14.4 10.6v2.2" />
            <path d="M10.2 15.4h3.6" />
            <path d="M2.6 10.8v3.2M21.4 10.8v3.2" />
          </svg>
        </span>
        <h1>HCI 故障排查助手</h1>
        <span v-if="chatStore.currentCase" class="brand-case">
          {{ chatStore.currentCase.case_id }}
        </span>
      </div>
      <div class="header-badges">
        <!-- 工单信息：历史工单入口 + ID/工单号展示 -->
        <el-popover
          placement="bottom-end"
          :width="260"
          trigger="click"
        >
          <template #reference>
            <el-button
              size="small"
              round
              class="header-btn case-info-btn"
            >
              <el-icon><Tickets /></el-icon>
              工单信息
            </el-button>
          </template>
          <div class="case-info-popover">
            <div class="case-info-row">
              <span class="case-info-label">客户端 ID</span>
              <span class="case-info-value selectable" :title="clientId">{{ clientId }}</span>
            </div>
            <div v-if="chatStore.currentCase" class="case-info-row">
              <span class="case-info-label">当前工单</span>
              <span class="case-info-value selectable">{{ chatStore.currentCase.case_id }}</span>
            </div>
            <div v-else class="case-info-row">
              <span class="case-info-label">当前工单</span>
              <span class="case-info-value muted">暂无</span>
            </div>
            <el-divider style="margin: 10px 0" />
            <el-button
              size="small"
              style="width: 100%"
              @click="chatStore.openHistoryDrawer()"
            >
              <el-icon><Tickets /></el-icon>
              查看历史工单
            </el-button>
          </div>
        </el-popover>

        <!-- 关闭工单 -->
        <el-button
          v-if="chatStore.hasActiveCase"
          size="small"
          round
          class="header-btn close-btn"
          @click="chatStore.handleCloseCase()"
        >
          <el-icon><Finished /></el-icon>
          关闭工单
        </el-button>

        <!-- 日志收集：常驻入口；无活跃工单时由对话框要求手工指定工单号 -->
        <el-button
          size="small"
          round
          class="header-btn log-collect-btn header-action-btn"
          @click="chatStore.openLogCollect()"
        >
          <el-icon><FolderOpened /></el-icon>
          日志收集
        </el-button>

        <!-- SSH终端按钮：点击先检测 Bridge -->
        <el-button
          size="small"
          round
          class="header-btn terminal-btn header-action-btn"
          :loading="chatStore.bridgeStatus === 'checking'"
          @click="chatStore.checkAndOpenTerminal()"
        >
          <el-icon v-if="chatStore.bridgeStatus !== 'checking'"><Monitor /></el-icon>
          SSH终端
        </el-button>
      </div>
    </header>

    <!-- 三栏：左面板 / 中央会话 / 右终端。全部是 flex 子项，无抽屉、无遮罩穿透 -->
    <div class="workspace-body">
      <PanelShell
        side="left"
        :open="chatStore.isLeftPanelOpen"
        :width="chatStore.leftPanelWidth"
        :overlay="isOverlayMode"
        :resizing="chatStore.isPanelResizing"
        @collapse="chatStore.collapseLeftPanel()"
        @resize-start="startPanelResize('left', $event)"
      >
        <template #rail>
          <el-tooltip content="日志收集" placement="right" :show-after="400">
            <button
              type="button"
              class="rail-btn"
              :class="{ 'is-active': chatStore.leftPanelTab === 'log' }"
              @click="chatStore.openLogCollect()"
            >
              <el-icon><FolderOpened /></el-icon>
            </button>
          </el-tooltip>
          <el-tooltip content="历史工单" placement="right" :show-after="400">
            <button
              type="button"
              class="rail-btn"
              :class="{ 'is-active': chatStore.leftPanelTab === 'history' }"
              @click="chatStore.openHistoryDrawer()"
            >
              <el-icon><Tickets /></el-icon>
            </button>
          </el-tooltip>
        </template>
        <template #title>
          <el-icon><component :is="chatStore.leftPanelTab === 'log' ? FolderOpened : Tickets" /></el-icon>
          <span>{{ chatStore.leftPanelTab === 'log' ? '日志收集' : '历史工单' }}</span>
        </template>

        <!-- v-show 保留实例：收起再展开不该重新探测环境，更不该断开 SSH 会话 -->
        <LogCollectPanel
          v-if="chatStore.logCollectCaseId"
          v-show="chatStore.logCollectVisible"
          v-model="logCollectVisible"
          :case-id="chatStore.logCollectCaseId"
        />
        <HistoryPanel
          v-if="chatStore.leftPanelTab === 'history'"
          v-show="chatStore.showHistoryDrawer"
        />
      </PanelShell>

      <main class="app-main">
        <!--
          中央会话的标题栏：高度与两侧面板的 panel-head 一致（48px），
          三栏顶部形成一条贯通的分隔线。之前中央没有这一条，
          双开时两侧 48px、中间直接是消息区，看起来就是「两边大、中间小」。
        -->
        <div class="main-head">
          <div class="main-head-inner">
            <span class="main-head-title">
              {{ chatStore.currentCase?.case_id || '未绑定工单' }}
            </span>
            <span v-if="chatStore.currentCase?.title" class="main-head-sub">
              {{ chatStore.currentCase.title }}
            </span>
          </div>
        </div>
        <ChatWindow />
      </main>

      <PanelShell
        side="right"
        :open="chatStore.isRightPanelOpen"
        :width="chatStore.rightPanelWidth"
        :overlay="isOverlayMode"
        :resizing="chatStore.isPanelResizing"
        @collapse="chatStore.closeTerminalSidebar()"
        @resize-start="startPanelResize('right', $event)"
      >
        <template #rail>
          <el-tooltip content="SSH 终端" placement="left" :show-after="400">
            <button
              type="button"
              class="rail-btn"
              :class="{
                'is-active': chatStore.isRightPanelOpen && chatStore.rightPanelTab === 'terminal',
              }"
              @click="chatStore.checkAndOpenTerminal()"
            >
              <el-icon><Monitor /></el-icon>
            </button>
          </el-tooltip>
          <!--
            文件下载与 SSH 终端平级：共用右栏这一块位置，靠页签切换。
            未登录时 openSftpPanel 会先弹统一的 SSH 登录框（同一个模板），
            登录成功后自动落到本页签。
          -->
          <el-tooltip content="文件下载" placement="left" :show-after="400">
            <button
              type="button"
              class="rail-btn"
              :class="{
                'is-active': chatStore.isRightPanelOpen && chatStore.rightPanelTab === 'sftp',
              }"
              @click="chatStore.openSftpPanel()"
            >
              <el-icon><Download /></el-icon>
            </button>
          </el-tooltip>
        </template>
        <template #title>
          <el-icon>
            <component :is="chatStore.rightPanelTab === 'terminal' ? Monitor : Download" />
          </el-icon>
          <span>{{ chatStore.rightPanelTab === 'terminal' ? 'SSH 终端' : '文件下载' }}</span>
        </template>

        <!--
          v-show 而不是 v-if：切页签不能销毁 xterm 实例，更不能断开 SSH 会话
          （文件下载用的就是这条会话）。
        -->
        <TerminalPanel
          v-show="chatStore.rightPanelTab === 'terminal'"
          @open-terminal-history="openCurrentCaseTerminalHistory"
        />
        <SftpPanel v-show="chatStore.rightPanelTab === 'sftp'" />
      </PanelShell>
    </div>

    <!-- 窄屏降级：面板覆盖在中央区之上，点空白处收起 -->
    <div
      v-if="isOverlayMode && (chatStore.isLeftPanelOpen || chatStore.isRightPanelOpen)"
      class="panel-scrim"
      @click="collapseOverlayPanel()"
    ></div>

<!-- SSH 连接弹框（create-case / terminal-only 统一入口） -->
    <SshConnectDialog :bridge-status="chatStore.sshConnectDialogBridgeStatus" />

    <!-- 无活跃工单时人工指定归档工单号 -->
    <LogCollectCaseDialog />

    <!-- Bridge 未运行时的下载提示弹窗 -->
    <el-dialog
      v-model="chatStore.showBridgeDownload"
      title="SSH 终端插件"
      width="420px"
      :close-on-click-modal="true"
      class="bridge-dialog"
    >
      <div class="bridge-prompt">
        <el-icon class="bridge-icon"><Monitor /></el-icon>
        <p class="bridge-title">生效 SSH 终端插件</p>
        <p class="bridge-desc">
          SSH 终端需要本地 Bridge 组件（2.5M）支持，检测到当前未运行。<br />
          请点击下载并打开，启动后再次点击「终端」按钮即可使用。
        </p>
      </div>
      <template #footer>
        <el-button @click="chatStore.closeBridgeDownload()">取消</el-button>
        <el-button type="primary" class="bridge-download-btn" @click="handleDownloadBridge">
          <el-icon><FolderOpened /></el-icon>
          下载并打开
        </el-button>
      </template>
    </el-dialog>
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
/*
 * 工作区外壳：顶部通栏应用栏 + 下方三栏。
 *
 * 三栏是 flex 兄弟节点，宽度由求解器直接给出；面板收窄/展开走宽度过渡，
 * 中央区自然吃到剩余空间——不需要再给主区补 padding 去"模拟"推挤。
 */
.workspace-shell {
  height: 100vh;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}

.workspace-body {
  flex: 1;
  min-height: 0;
  display: flex;
  position: relative;
  align-items: stretch;
}

.app-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #f0f2f5;
}

/* 与 PanelShell 的 .panel-head 同高，三栏顶部对齐 */
.main-head {
  flex: 0 0 48px;
  height: 48px;
  display: flex;
  align-items: center;
  padding: 0 16px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
}

/* 标题栏同属框架条：通栏左对齐，不再跟着内容列在中间'飘' */
.main-head-inner {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.main-head-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
}

.main-head-sub {
  font-size: 13px;
  color: #909399;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 窄屏覆盖态的遮罩：唯一还会出现遮罩的场景 */
.panel-scrim {
  position: fixed;
  inset: 0;
  z-index: 29;
  background: rgba(0, 0, 0, 0.35);
}

.app-header {
  /*
   * 通栏应用栏 = 应用框架，不是内容列。
   *
   * 它横跨三栏、宽度恒定，所以绝不能吃 --ws-center-max：那个变量会随面板开合
   * 在 460~900 之间跳，顶栏会跟着忽宽忽窄，1920 屏上表现为「一大条蓝带中间
   * 挤着一段标题+四个小圆点」。框架用固定内边距、两端对齐即可。
   */
  flex: 0 0 auto;
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 20px;
  background: linear-gradient(135deg, #409eff, #337ecc);
  color: #fff;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  z-index: 1;
}

/* 品牌区：图标 + 标题 + 当前工单号，始终贴左 */
.header-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.brand-mark {
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.18);
  font-size: 16px;
}

/* 机器人头在 28px 方块里留 4px 呼吸位，线宽固定由 SVG 自己控制 */
.brand-mark__bot {
  width: 18px;
  height: 18px;
  display: block;
}

.header-brand h1 {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.2px;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.brand-case {
  flex: 0 0 auto;
  padding: 2px 9px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.18);
  font-size: 12px;
  white-space: nowrap;
}

/* 按钮组永不压缩：压缩态优先让标题省略，按钮仍要完整可点 */
.header-badges {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

/*
 * 压缩态顶栏：只在视口确实窄（<900px）时生效，此时按钮文字会把标题挤到溢出，
 * 退化为纯图标（图标本体字号要单独设回去，因为它也吃 font-size）。
 */
.app-header.is-compact {
  height: 52px;
  padding: 0 12px;
  gap: 8px;
}

.app-header.is-compact .header-brand h1 {
  display: none;
}

.app-header.is-compact .header-badges {
  gap: 6px;
}

.app-header.is-compact .header-badges .el-button {
  font-size: 0 !important;
  padding: 6px 8px !important;
}

.app-header.is-compact .header-badges .el-button .el-icon {
  font-size: 14px !important;
  margin-right: 0 !important;
}

.case-badge {
  background: rgba(255, 255, 255, 0.2);
  padding: 4px 12px;
  border-radius: 12px;
  font-size: 13px;
}

.case-info-popover {
  padding: 4px 0;
}

.case-info-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  line-height: 1.5;
}

.case-info-label {
  flex-shrink: 0;
  color: #909399;
  width: 64px;
}

.case-info-value {
  color: #303133;
  word-break: break-all;
}

.case-info-value.selectable {
  cursor: text;
  user-select: all;
}

.case-info-value.muted {
  color: #c0c4cc;
  font-style: italic;
}

/*
 * 顶栏按钮统一规范（工单信息 / 关闭工单 / 日志收集 / SSH终端 共用）
 *
 * 为什么用实色白底而不是半透明白：
 * 顶栏背景是 #409eff → #337ecc 的横向渐变，半透明层叠在渐变上，
 * 同一个 rgba 值在左右两侧会合成出不同的颜色——即使四个按钮写的是同一个
 * rgba，肉眼看到的仍是四种底色。只有实色才真正一致。
 */
/* 顶栏按钮：不透明白底。
   半透明底叠在 #409eff→#337ecc 渐变上，每个按钮会呈现不同深浅——看着像四种颜色。 */
.header-btn {
  background: #ffffff !important;
  border: 1px solid #ffffff !important;
  color: #2b6cb0 !important;
  font-size: 12px !important;
  font-weight: 500 !important;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

/* 悬停色只绑定 :hover。不要绑定 :focus / :focus-visible：
   按钮被点过以后会保留焦点，Element Plus 也会给 :focus 上 hover 色，
   结果就是"点过的那个按钮一直和另外三个颜色不一样"。 */
.header-btn:hover {
  background: #ecf5ff !important;
  border-color: #ecf5ff !important;
  color: #2b6cb0 !important;
}

/* 键盘可达性只加描边：白色描边在蓝色顶栏上足够可见，且不改变底色。
   Element Plus 自身的 :focus 着色不带 !important，压不过上面的白底 !important，
   所以点过的按钮移开鼠标后仍会回到白色。 */
.header-btn:focus-visible {
  outline: 2px solid #ffffff;
  outline-offset: 2px;
}

/* 图标与文字间距统一交给 el-button 自身渲染，避免个别按钮多 4px 造成不齐 */
.header-btn .el-icon {
  margin-right: 0;
}

.app-main {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* Bridge 下载提示弹窗 */
.bridge-prompt {
  text-align: center;
  padding: 8px 0 16px;
}

.bridge-icon {
  font-size: 48px;
  margin-bottom: 12px;
  color: var(--el-color-primary);
}

.bridge-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 10px;
}

.bridge-desc {
  font-size: 13px;
  color: #606266;
  line-height: 1.8;
}

.bridge-download-btn :deep(span) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}

/* 终端历史按钮 */
.terminal-history-btn {
  background: rgba(64, 158, 255, 0.3) !important;
  border: 1px solid rgba(64, 158, 255, 0.5) !important;
  color: #fff !important;
  font-size: 12px !important;
}

.terminal-history-btn:hover {
  background: rgba(64, 158, 255, 0.5) !important;
}

/* 终端回放弹窗 */
.terminal-replay-dialog :deep(.el-dialog__body) {
  padding: 12px;
  max-height: 80vh;
  overflow: hidden;
}

.terminal-replay-dialog :deep(.el-dialog) {
  max-width: 1200px;
}
</style>
