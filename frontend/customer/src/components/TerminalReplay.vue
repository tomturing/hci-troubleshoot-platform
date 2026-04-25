<script setup lang="ts">
/**
 * TerminalReplay.vue
 * 终端操作回放组件
 * Task 42: 终端操作录制功能
 *
 * 功能：
 * - 时间轴回放（播放/暂停/速度控制）
 * - xterm.js 渲染终端输出
 * - 操作详情卡片
 * - 复制/引用功能
 */
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'
import { ElMessage } from 'element-plus'

import {
  listOperations,
  stripAnsi,
  type TerminalOperationResponse,
  type OperationDirection,
} from '@/api/terminal-recording'

// ============================================================
// Props & Emits
// ============================================================

const props = defineProps<{
  caseId: string
  conversationId?: string
}>()

const emit = defineEmits<{
  (e: 'quote-to-chat', content: string): void
}>()

// ============================================================
// 状态
// ============================================================

const loading = ref(false)
const operations = ref<TerminalOperationResponse[]>([])
const totalOps = ref(0)
const currentSeq = ref(0)

// 时间轴控制
const isPlaying = ref(false)
const speed = ref(1)
const playTimer: Ref<ReturnType<typeof setTimeout> | null> = ref(null)

// 视图模式
const viewMode = ref<'timeline' | 'list'>('timeline')

// 搜索/过滤
const searchKeyword = ref('')
const filterStage = ref('')
const filterDirection = ref<OperationDirection | ''>('')

// xterm.js 实例
let xterm: Terminal | null = null
let fitAddon: FitAddon | null = null
const terminalContainer = ref<HTMLElement | null>(null)
let resizeObserver: ResizeObserver | null = null

// ============================================================
// 计算属性
// ============================================================

const currentOp = computed(() => {
  return operations.value.find((op) => op.seq_number === currentSeq.value)
})

const progressPercent = computed(() => {
  if (totalOps.value === 0) return 0
  return Math.round((currentSeq.value / totalOps.value) * 100)
})

const inputOps = computed(() => {
  return operations.value.filter((op) => op.direction === 'input')
})

// ============================================================
// 数据加载
// ============================================================

async function loadOperations() {
  if (!props.caseId) return

  loading.value = true
  try {
    const result = await listOperations(props.caseId, {
      stage: filterStage.value || undefined,
      search: searchKeyword.value || undefined,
      direction: filterDirection.value || undefined,
      order: 'asc',
      limit: 500,
    })

    operations.value = result.operations
    totalOps.value = result.total

    if (result.operations.length > 0) {
      currentSeq.value = result.operations[0].seq_number
    }

    // 初始化 xterm 并渲染第一条
    await nextTick()
    initTerminal()
    renderCurrentOperation()
  } catch (e) {
    console.error('[TerminalReplay] 加载操作记录失败:', e)
    ElMessage.error('加载终端操作记录失败')
  } finally {
    loading.value = false
  }
}

// ============================================================
// xterm.js 初始化与渲染
// ============================================================

function initTerminal() {
  if (!terminalContainer.value || xterm) return

  xterm = new Terminal({
    convertEol: true,
    disableStdin: true,
    fontFamily: 'Consolas, Monaco, monospace',
    fontSize: 13,
    lineHeight: 1.35,
    cursorBlink: false,
    theme: {
      background: '#1e1e1e',
      foreground: '#d4d4d4',
      cursor: '#d4d4d4',
      black: '#1e1e1e',
      red: '#f56c6c',
      green: '#67c23a',
      yellow: '#e6a23c',
      blue: '#409eff',
      magenta: '#c678dd',
      cyan: '#56b6c2',
      white: '#d4d4d4',
      brightBlack: '#5c6370',
      brightRed: '#ff7b72',
      brightGreen: '#8bc34a',
      brightYellow: '#fbc02d',
      brightBlue: '#61afef',
      brightMagenta: '#d19a66',
      brightCyan: '#56b6c2',
      brightWhite: '#ffffff',
    },
    scrollback: 5000,
  })

  fitAddon = new FitAddon()
  xterm.loadAddon(fitAddon)
  xterm.open(terminalContainer.value)
  fitAddon.fit()

  resizeObserver = new ResizeObserver(() => {
    if (fitAddon) fitAddon.fit()
  })
  resizeObserver.observe(terminalContainer.value)
}

function disposeTerminal() {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  if (xterm) {
    xterm.dispose()
    xterm = null
  }
  fitAddon = null
}

function clearTerminal() {
  if (xterm) {
    xterm.clear()
  }
}

function normalizeTerminalText(text: string): string {
  return text.replace(/\r?\n/g, '\r\n')
}

/**
 * 渲染当前操作到终端
 */
function renderCurrentOperation() {
  if (!xterm || !currentOp.value) return

  const op = currentOp.value

  // 输入命令用绿色高亮
  if (op.direction === 'input') {
    xterm.write(`\x1b[32m$ ${op.command || op.content}\x1b[0m\r\n`)
  } else {
    // 输出内容（含 ANSI 码，保留原始）
    xterm.write(normalizeTerminalText(op.content))
    xterm.write('\r\n')

    // 显示退出码（如果有）
    if (op.exit_code !== undefined && op.exit_code !== null) {
      if (op.exit_code === 0) {
        xterm.write(`\x1b[32m[退出码: 0]\x1b[0m\r\n`)
      } else {
        xterm.write(`\x1b[31m[退出码: ${op.exit_code}]\x1b[0m\r\n`)
      }
    }
  }
}

/**
 * 渲染全部操作到终端（静态视图）
 */
function renderAllOperations() {
  clearTerminal()
  if (!xterm) return

  for (const op of operations.value) {
    // 时间戳和阶段标记
    xterm.write(`\x1b[90m# ${op.created_at.slice(0, 19)}${op.diagnostic_stage ? ` [${op.diagnostic_stage}]` : ''}\x1b[0m\r\n`)

    if (op.direction === 'input') {
      xterm.write(`\x1b[32m$ ${op.command || op.content}\x1b[0m\r\n`)
    } else {
      xterm.write(normalizeTerminalText(op.content))
      xterm.write('\r\n')
      if (op.exit_code !== undefined && op.exit_code !== null && op.exit_code !== 0) {
        xterm.write(`\x1b[31m[退出码: ${op.exit_code}]\x1b[0m\r\n`)
      }
    }
    xterm.write('\r\n')
  }
}

// ============================================================
// 时间轴控制
// ============================================================

function play() {
  if (isPlaying.value) return
  isPlaying.value = true
  scheduleNextPlay()
}

function pause() {
  isPlaying.value = false
  if (playTimer.value) {
    clearTimeout(playTimer.value)
    playTimer.value = null
  }
}

function scheduleNextPlay() {
  if (!isPlaying.value) return

  // 计算延迟（根据速度）
  const baseDelay = 300
  const delay = baseDelay / speed.value

  playTimer.value = setTimeout(() => {
    nextOperation()
    if (currentSeq.value < totalOps.value) {
      scheduleNextPlay()
    } else {
      // 播放完毕
      pause()
      ElMessage.success('回放完成')
    }
  }, delay)
}

function nextOperation() {
  const nextSeq = currentSeq.value + 1
  if (nextSeq <= totalOps.value) {
    currentSeq.value = nextSeq
    renderCurrentOperation()
  }
}

function prevOperation() {
  const prevSeq = currentSeq.value - 1
  if (prevSeq >= 1) {
    currentSeq.value = prevSeq
    renderCurrentOperation()
  }
}

function jumpToSeq(seq: number) {
  if (seq >= 1 && seq <= totalOps.value) {
    currentSeq.value = seq
    renderCurrentOperation()
  }
}

function jumpToInput(index: number) {
  const inputOp = inputOps.value[index]
  if (inputOp) {
    jumpToSeq(inputOp.seq_number)
  }
}

// ============================================================
// 复制/引用功能
// ============================================================

async function copyCommand() {
  if (!currentOp.value?.command) return
  await copyToClipboard(currentOp.value.command)
  ElMessage.success('命令已复制')
}

async function copyOutput() {
  if (!currentOp.value?.content) return
  const cleanOutput = stripAnsi(currentOp.value.content)
  await copyToClipboard(cleanOutput)
  ElMessage.success('输出已复制')
}

async function copyToClipboard(text: string) {
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
}

function quoteToChat() {
  if (!currentOp.value) return
  const content = currentOp.value.direction === 'input'
    ? `命令: ${currentOp.value.command}`
    : `输出:\n${stripAnsi(currentOp.value.content)}`
  emit('quote-to-chat', content)
}

// ============================================================
// 搜索/过滤
// ============================================================

async function applyFilter() {
  pause()
  clearTerminal()
  await loadOperations()
}

function clearFilter() {
  searchKeyword.value = ''
  filterStage.value = ''
  filterDirection.value = ''
  applyFilter()
}

// ============================================================
// 导出功能
// ============================================================

function exportAsText() {
  const lines = operations.value.map((op) => {
    const time = op.created_at.slice(0, 19)
    const stage = op.diagnostic_stage ? ` [${op.diagnostic_stage}]` : ''
    if (op.direction === 'input') {
      return `${time}${stage}\n$ ${op.command || op.content}\n`
    } else {
      return `${time}${stage}\n${stripAnsi(op.content)}\n[退出码: ${op.exit_code ?? 'N/A'}]\n`
    }
  })
  const text = lines.join('\n---\n')

  downloadFile(`terminal_${props.caseId}.txt`, text)
}

function exportAsJson() {
  const json = JSON.stringify(operations.value, null, 2)
  downloadFile(`terminal_${props.caseId}.json`, json)
}

function downloadFile(filename: string, content: string) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

// ============================================================
// 生命周期
// ============================================================

onMounted(() => {
  loadOperations()
})

onBeforeUnmount(() => {
  pause()
  disposeTerminal()
})

// 监听视图模式切换
watch(viewMode, (mode) => {
  if (mode === 'list') {
    pause()
    renderAllOperations()
  } else {
    clearTerminal()
    renderCurrentOperation()
  }
})

// 监听 caseId 变化
watch(() => props.caseId, () => {
  pause()
  clearTerminal()
  loadOperations()
})
</script>

<template>
  <div class="terminal-replay">
    <!-- 过滤条件 -->
    <div class="filter-section">
      <el-input
        v-model="searchKeyword"
        placeholder="搜索关键词"
        clearable
        style="width: 200px"
        @keyup.enter="applyFilter"
      />
      <el-select v-model="filterStage" placeholder="诊断阶段" clearable style="width: 120px">
        <el-option label="S0" value="S0" />
        <el-option label="S1" value="S1" />
        <el-option label="S2" value="S2" />
        <el-option label="S3" value="S3" />
        <el-option label="S4" value="S4" />
        <el-option label="S5" value="S5" />
        <el-option label="S6" value="S6" />
      </el-select>
      <el-select v-model="filterDirection" placeholder="方向" clearable style="width: 100px">
        <el-option label="输入" value="input" />
        <el-option label="输出" value="output" />
      </el-select>
      <el-button @click="applyFilter">筛选</el-button>
      <el-button @click="clearFilter">清空</el-button>
    </div>

    <!-- 视图模式切换 -->
    <div class="view-mode-switch">
      <el-radio-group v-model="viewMode" size="small">
        <el-radio-button label="timeline">时间轴回放</el-radio-button>
        <el-radio-button label="list">静态列表</el-radio-button>
      </el-radio-group>
    </div>

    <!-- 时间轴控制 -->
    <div v-if="viewMode === 'timeline'" class="timeline-control">
      <el-button-group>
        <el-button :disabled="currentSeq <= 1" @click="prevOperation">◀</el-button>
        <el-button v-if="isPlaying" type="primary" @click="pause">⏸ 暂停</el-button>
        <el-button v-else type="primary" :disabled="currentSeq >= totalOps" @click="play">▶ 播放</el-button>
        <el-button :disabled="currentSeq >= totalOps" @click="nextOperation">▶</el-button>
      </el-button-group>

      <el-select v-model="speed" style="width: 80px">
        <el-option label="1x" :value="1" />
        <el-option label="2x" :value="2" />
        <el-option label="5x" :value="5" />
      </el-select>

      <span class="progress-text">进度: {{ currentSeq }}/{{ totalOps }}</span>

      <el-progress :percentage="progressPercent" :show-text="false" style="width: 200px" />
    </div>

    <!-- xterm.js 终端回放区 -->
    <div ref="terminalContainer" class="terminal-canvas" />

    <!-- 操作详情卡片 -->
    <div v-if="currentOp && viewMode === 'timeline'" class="operation-detail">
      <div class="detail-header">
        <el-tag :type="currentOp.direction === 'input' ? 'success' : 'info'" size="small">
          {{ currentOp.direction === 'input' ? '输入' : '输出' }}
        </el-tag>
        <span class="detail-time">{{ currentOp.created_at.slice(0, 19) }}</span>
        <el-tag v-if="currentOp.diagnostic_stage" size="small" effect="plain">
          {{ currentOp.diagnostic_stage }}
        </el-tag>
      </div>

      <div class="detail-content">
        <div v-if="currentOp.direction === 'input'" class="detail-command">
          <strong>命令:</strong> {{ currentOp.command }}
        </div>
        <div v-else class="detail-output">
          <strong>输出长度:</strong> {{ currentOp.content.length }} 字符
          <strong>退出码:</strong>
          <el-tag :type="currentOp.exit_code === 0 ? 'success' : 'danger'" size="small">
            {{ currentOp.exit_code ?? 'N/A' }}
          </el-tag>
        </div>
      </div>

      <div class="detail-actions">
        <el-button size="small" :disabled="!currentOp.command" @click="copyCommand">复制命令</el-button>
        <el-button size="small" :disabled="currentOp.direction !== 'output'" @click="copyOutput">复制输出</el-button>
        <el-button size="small" type="primary" @click="quoteToChat">引用到对话</el-button>
      </div>
    </div>

    <!-- 命令列表（快速跳转） -->
    <div v-if="viewMode === 'timeline'" class="command-list">
      <div class="list-header">命令列表（点击跳转）</div>
      <div class="list-items">
        <div
          v-for="(op, index) in inputOps"
          :key="op.seq_number"
          class="list-item"
          :class="{ active: currentSeq === op.seq_number }"
          @click="jumpToInput(index)"
        >
          <span class="item-seq">{{ op.seq_number }}</span>
          <span class="item-cmd">{{ op.command || op.content }}</span>
          <span class="item-time">{{ op.created_at.slice(11, 19) }}</span>
        </div>
      </div>
    </div>

    <!-- 导出功能 -->
    <div class="export-section">
      <el-button size="small" @click="exportAsText">导出为文本</el-button>
      <el-button size="small" @click="exportAsJson">导出为 JSON</el-button>
    </div>
  </div>
</template>

<style scoped>
.terminal-replay {
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: #1e1e1e;
  padding: 16px;
  border-radius: 8px;
}

.filter-section {
  display: flex;
  gap: 8px;
  align-items: center;
}

.view-mode-switch {
  display: flex;
  justify-content: center;
}

.timeline-control {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 0;
}

.progress-text {
  color: #d4d4d4;
  font-size: 13px;
}

.terminal-canvas {
  flex: 1;
  min-height: 300px;
  background: #1e1e1e;
  border: 1px solid #333;
  border-radius: 6px;
  overflow: hidden;
}

.terminal-canvas :deep(.xterm) {
  height: 100%;
  padding: 8px;
}

.terminal-canvas :deep(.xterm-cursor),
.terminal-canvas :deep(.xterm-cursor-layer) {
  display: none !important;
}

.operation-detail {
  background: #252526;
  border: 1px solid #333;
  border-radius: 6px;
  padding: 12px;
}

.detail-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.detail-time {
  color: #d4d4d4;
  font-size: 13px;
}

.detail-content {
  color: #d4d4d4;
  font-size: 13px;
  margin-bottom: 8px;
}

.detail-command {
  font-family: Consolas, Monaco, monospace;
}

.detail-output {
  display: flex;
  gap: 16px;
}

.detail-actions {
  display: flex;
  gap: 8px;
}

.command-list {
  background: #252526;
  border: 1px solid #333;
  border-radius: 6px;
  max-height: 200px;
  overflow-y: auto;
}

.list-header {
  padding: 8px 12px;
  color: #d4d4d4;
  font-size: 13px;
  border-bottom: 1px solid #333;
}

.list-items {
  padding: 4px;
}

.list-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  color: #d4d4d4;
  font-size: 12px;
  cursor: pointer;
  border-radius: 4px;
}

.list-item:hover {
  background: #2d2d30;
}

.list-item.active {
  background: #37373d;
}

.item-seq {
  color: #56b6c2;
  width: 30px;
}

.item-cmd {
  flex: 1;
  font-family: Consolas, Monaco, monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item-time {
  color: #909399;
  width: 60px;
}

.export-section {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
</style>