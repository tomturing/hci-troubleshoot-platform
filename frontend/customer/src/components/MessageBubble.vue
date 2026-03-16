<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ChatMessage } from '@/stores/chat'
import { renderMarkdown, isCommandLanguage } from '@/utils/markdown'
import CommandBlock from './CommandBlock.vue'

const props = defineProps<{ message: ChatMessage }>()

const isUser = computed(() => props.message.role === 'user')
const isSystem = computed(() => props.message.role === 'system')
const isAssistant = computed(() => props.message.role === 'assistant')
const isDivider = computed(() => isSystem.value && props.message.content.includes('────'))

/** 已复制状态 */
const copiedMessage = ref(false)
async function copyContent() {
  const text = props.message.content
  const copied = await copyToClipboard(text)
  if (!copied) {
    return
  }
  copiedMessage.value = true
  setTimeout(() => (copiedMessage.value = false), 2000)
}

/** 复制到剪贴板（兼容非安全上下文） */
async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch (e) {
      console.warn('复制失败:', e)
      return false
    }
  } else {
    // 非安全上下文降级：使用 textarea + execCommand
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    try {
      document.execCommand('copy')
      return true
    } catch (e) {
      console.warn('复制失败:', e)
      return false
    }
    finally {
      document.body.removeChild(textarea)
    }
  }
}

/** 格式化时间 */
function formatTime(d: Date) {
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

/**
 * 提取消息中的命令块并拆分内容
 * 返回片段数组，每个片段可以是普通文本或命令块
 */
interface ContentSegment {
  id: string
  type: 'text' | 'command'
  content: string
  language?: string
  commandIndex?: number
}

const contentSegments = computed<ContentSegment[]>(() => {
  if (!props.message.content) return []

  const segments: ContentSegment[] = []
  let lastIndex = 0
  let commandIndex = 0

  // 正则匹配代码块（兼容流式输出时未闭合的情况）：```language\ncode(```|EOF)
  const codeBlockRegex = /```(?:[ \t]*)([\w-]*)(?:\r?\n)([\s\S]*?)(?:```|$)/g
  let match

  while ((match = codeBlockRegex.exec(props.message.content)) !== null) {
    const fullMatch = match[0]
    const language = match[1] || 'plaintext'
    const code = match[2]
    const matchStart = match.index
    const matchEnd = matchStart + fullMatch.length

    // 添加代码块前的普通文本
    if (matchStart > lastIndex) {
      const textBefore = props.message.content.substring(lastIndex, matchStart)
      if (textBefore.trim()) {
        segments.push({
          id: `text-${matchStart}-${segments.length}`,
          type: 'text',
          content: textBefore,
        })
      }
    }

    // 判断是否为命令块
    if (isCommandLanguage(language)) {
      // 是命令块，需要进一步拆分多命令
      const commands = splitCommands(code)
      for (const cmd of commands) {
        segments.push({
          id: `command-${matchStart}-${commandIndex}`,
          type: 'command',
          content: cmd,
          language,
          commandIndex,
        })
        commandIndex += 1
      }
    } else {
      // 非命令代码块，作为普通文本渲染（但保留代码块标记）
      segments.push({
        id: `text-code-${matchStart}-${segments.length}`,
        type: 'text',
        content: fullMatch,
      })
    }

    lastIndex = matchEnd
  }

  // 添加剩余的普通文本
  if (lastIndex < props.message.content.length) {
    const textAfter = props.message.content.substring(lastIndex)
    if (textAfter.trim()) {
      segments.push({
        id: `text-tail-${lastIndex}-${segments.length}`,
        type: 'text',
        content: textAfter,
      })
    }
  }

  return segments
})

/**
 * 拆分命令块
 * 为避免误拆分多行脚本，默认保持整块发送
 * @param code 原始代码内容
 * @returns 命令数组
 */
function splitCommands(code: string): string[] {
  const normalized = code.trimEnd()
  return normalized ? [normalized] : []
}

/**
 * 检测风险提示级别
 * 根据命令内容和上下文判断风险等级
 * @param command 命令内容
 * @returns 风险等级
 */
function detectRiskLevel(command: string): 'none' | 'readonly' | 'caution' | 'danger' {
  const lowerCmd = command.toLowerCase()

  // 高危命令检测
  const dangerPatterns = [
    'rm -rf', 'rm -r /', 'dd if=/dev/zero', 'mkfs.', 'fdisk',
    '> /dev/sda', 'shutdown', 'reboot', 'poweroff', 'init 0',
    'systemctl stop', 'kill -9', ':(){ :|:& };:' // fork bomb
  ]
  if (dangerPatterns.some(p => lowerCmd.includes(p))) {
    return 'danger'
  }

  // 谨慎命令检测
  const cautionPatterns = [
    'apt remove', 'yum remove', 'pip uninstall', 'npm uninstall',
    'docker rm', 'docker rmi', 'kubectl delete',
    'chmod 777', 'chown -R'
  ]
  if (cautionPatterns.some(p => lowerCmd.includes(p))) {
    return 'caution'
  }

  // 只读命令检测
  const readonlyPatterns = [
    'cat ', 'ls ', 'ps ', 'top', 'htop', 'df ', 'du ', 'free',
    'uptime', 'who', 'w', 'last', 'dmesg', 'journalctl',
    'cat\t', 'ls\t', 'ps\t', 'df\t', 'du\t'
  ]
  if (readonlyPatterns.some(p => lowerCmd.startsWith(p) || lowerCmd.includes(' ' + p))) {
    return 'readonly'
  }

  // 默认安全
  return 'none'
}

/**
 * 生成命令描述
 * 基于命令内容生成简单说明
 * @param command 命令内容
 * @returns 命令描述
 */
function generateDescription(command: string): string {
  const lowerCmd = command.toLowerCase().trim()
  const firstWord = lowerCmd.split(/\s+/)[0]

  // 常见命令描述映射
  const descriptions: Record<string, string> = {
    'ls': '列出目录内容',
    'cat': '查看文件内容',
    'ps': '查看进程状态',
    'top': '实时查看系统进程',
    'df': '查看磁盘空间使用情况',
    'du': '查看目录/文件大小',
    'free': '查看内存使用情况',
    'uptime': '查看系统运行时间',
    'dmesg': '查看内核消息',
    'journalctl': '查看系统日志',
    'systemctl': '管理系统服务',
    'service': '管理系统服务',
    'docker': '管理 Docker 容器/镜像',
    'kubectl': '管理 Kubernetes 资源',
    'ping': '测试网络连通性',
    'curl': '发送 HTTP 请求',
    'wget': '下载文件',
    'ssh': '远程登录',
    'scp': '安全复制文件',
    'tar': '打包/解包文件',
    'grep': '文本搜索',
    'awk': '文本处理',
    'sed': '流编辑器',
    'find': '查找文件',
    'chmod': '修改文件权限',
    'chown': '修改文件所有者',
    'rm': '删除文件/目录',
    'cp': '复制文件/目录',
    'mv': '移动/重命名文件',
    'mkdir': '创建目录',
    'rmdir': '删除空目录',
    'touch': '创建空文件/更新时间戳',
    'head': '查看文件开头',
    'tail': '查看文件末尾',
    'less': '分页查看文件',
    'more': '分页查看文件',
    'netstat': '查看网络连接',
    'ss': '查看 socket 统计',
    'lsof': '列出打开的文件',
    'iostat': '查看 IO 统计',
    'vmstat': '查看虚拟内存统计',
    'mpstat': '查看 CPU 统计',
    'sar': '系统活动报告',
  }

  return descriptions[firstWord] || ''
}

/**
 * 渲染普通文本片段
 * 将 Markdown 转换为 HTML
 * @param content 文本内容
 * @returns 渲染后的 HTML
 */
function renderTextSegment(content: string): string {
  return renderMarkdown(content)
}
</script>

<template>
  <div
    class="message-bubble"
    :class="{
      'is-user': isUser,
      'is-system': isSystem,
      'is-assistant': isAssistant,
      'is-streaming': message.isStreaming,
      'is-divider': isDivider,
    }"
  >
    <!-- 系统消息：居中小字 -->
    <div v-if="isSystem" class="system-msg">
      {{ message.content }}
    </div>

    <!-- 用户/AI消息 -->
    <template v-else>
      <div class="avatar">
        <span v-if="isUser">我</span>
        <span v-else>AI</span>
      </div>
      <div class="bubble-content">
        <div
          class="bubble-body"
          :class="{ 'is-command-available': !message.isStreaming && contentSegments.some(s => s.type === 'command') }"
        >
          <!-- 阶段1：Thinking 状态（流式中且内容为空） -->
          <template v-if="message.isStreaming && !message.content">
            <div class="thinking-indicator">
              <span class="thinking-dot" />
              <span class="thinking-dot" />
              <span class="thinking-dot" />
              <span class="thinking-label">思考中</span>
            </div>
          </template>

          <!-- 阶段2：流式输出中（内容非空），使用纯Markdown避免未闭合代码块 -->
          <template v-else-if="message.isStreaming && message.content">
            <div class="text-segment" v-html="renderTextSegment(message.content)" />
          </template>

          <!-- 阶段3：完整输出后，将命令块升级为交互式 CommandBlock -->
          <!-- 注意：每个 segment 必须是单根真实 DOM 元素，不能用 <template> 包裹 -->
          <!-- Vue 3 的 <transition-group> 不支持 fragment(template) 作为子节点 -->
          <template v-else>
            <div
              v-for="segment in contentSegments"
              :key="segment.id"
              class="segment-item"
            >
              <!-- 命令块使用 CommandBlock 组件 -->
              <CommandBlock
                v-if="segment.type === 'command'"
                class="stage3-item"
                :command="segment.content"
                :language="segment.language || 'bash'"
                :description="generateDescription(segment.content)"
                :risk-level="detectRiskLevel(segment.content)"
                :index="segment.commandIndex || 0"
              />
              <!-- 普通文本使用 Markdown 渲染 -->
              <div
                v-else
                class="text-segment stage3-item"
                v-html="renderTextSegment(segment.content)"
              />
            </div>
          </template>
        </div>
        <div class="bubble-meta">
          <span class="bubble-time">{{ formatTime(message.timestamp) }}</span>
          <el-button
            v-if="isAssistant && message.content"
            size="small"
            text
            @click="copyContent"
          >
            {{ copiedMessage ? '已复制' : '复制' }}
          </el-button>
        </div>
        <!-- 流式输出光标 -->
        <span v-if="message.isStreaming && message.content" class="streaming-cursor">▊</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.message-bubble {
  display: flex;
  gap: 10px;
  max-width: 85%;
}

.message-bubble.is-user {
  align-self: flex-end;
  flex-direction: row-reverse;
}

.message-bubble.is-system {
  align-self: center;
  max-width: 100%;
}

.system-msg {
  font-size: 12px;
  color: #909399;
  background: rgba(0, 0, 0, 0.04);
  padding: 6px 16px;
  border-radius: 12px;
  text-align: center;
}

/* 工单分割线样式 */
.message-bubble.is-divider {
  max-width: 100%;
  width: 100%;
}

.message-bubble.is-divider .system-msg {
  background: transparent;
  border-radius: 0;
  padding: 12px 0;
  margin: 4px 0;
  color: #c0c4cc;
  font-size: 12px;
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
}

.message-bubble.is-divider .system-msg::before,
.message-bubble.is-divider .system-msg::after {
  content: '';
  flex: 1;
  height: 1px;
  background: #dcdfe6;
}

.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
}

.is-user .avatar {
  background: #409eff;
  color: #fff;
}

.is-assistant .avatar {
  background: #67c23a;
  color: #fff;
}

.bubble-content {
  background: #fff;
  border-radius: 12px;
  padding: 12px 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  position: relative;
  line-height: 1.7;
  font-size: 14px;
  overflow: hidden;
  max-width: 100%;
}

.is-user .bubble-content {
  background: #409eff;
  color: #fff;
}

.bubble-meta {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 6px;
}

.bubble-time {
  font-size: 11px;
  color: #c0c4cc;
}

.is-user .bubble-time {
  color: rgba(255, 255, 255, 0.6);
}

.streaming-cursor {
  animation: blink 1s infinite;
  color: #409eff;
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}

/* ========================================
   Thinking 动画
   ======================================== */
.thinking-indicator {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 4px 2px;
}

.thinking-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #409eff;
  opacity: 0.3;
  animation: thinking-pulse 1.4s ease-in-out infinite;
}

.thinking-dot:nth-child(1) { animation-delay: 0s; }
.thinking-dot:nth-child(2) { animation-delay: 0.22s; }
.thinking-dot:nth-child(3) { animation-delay: 0.44s; }

@keyframes thinking-pulse {
  0%, 80%, 100% {
    opacity: 0.2;
    transform: scale(0.85);
  }
  40% {
    opacity: 1;
    transform: scale(1.15);
  }
}

.thinking-label {
  font-size: 12px;
  color: #909399;
  margin-left: 4px;
  letter-spacing: 0.5px;
}

/* ========================================
   阶段3 升级渲染：每个 segment 用单根 div.segment-item 包裹
   display: contents 让包裹层对布局透明不影响内部元素排列
   动画加在内部实际元素上（避免 display:contents 动画兼容性问题）
   ======================================== */
.segment-item {
  display: contents;
}

/* 动画施加在阶段3的实际内容元素上 */
.stage3-item {
  animation: segment-enter 0.35s ease both;
}

@keyframes segment-enter {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* ========================================
   文本片段样式（Markdown 渲染）
   ======================================== */

.bubble-body {
  word-break: break-word;
}

.text-segment {
  word-break: break-word;
}

/* 标题样式 */
.text-segment :deep(h1) {
  font-size: 1.5em;
  font-weight: 600;
  margin: 16px 0 8px 0;
  padding-bottom: 6px;
  border-bottom: 1px solid #ebeef5;
  line-height: 1.4;
}

.text-segment :deep(h2) {
  font-size: 1.3em;
  font-weight: 600;
  margin: 14px 0 6px 0;
  line-height: 1.4;
}

.text-segment :deep(h3) {
  font-size: 1.15em;
  font-weight: 600;
  margin: 12px 0 4px 0;
  line-height: 1.4;
}

.text-segment :deep(h4),
.text-segment :deep(h5),
.text-segment :deep(h6) {
  font-size: 1em;
  font-weight: 600;
  margin: 10px 0 4px 0;
  line-height: 1.4;
}

/* 段落样式 */
.text-segment :deep(p) {
  margin: 8px 0;
  line-height: 1.7;
}

.text-segment :deep(p:first-child) {
  margin-top: 0;
}

.text-segment :deep(p:last-child) {
  margin-bottom: 0;
}

/* 列表样式 */
.text-segment :deep(ul),
.text-segment :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.text-segment :deep(li) {
  margin: 4px 0;
  line-height: 1.6;
}

.text-segment :deep(ul) {
  list-style-type: disc;
}

.text-segment :deep(ol) {
  list-style-type: decimal;
}

/* 引用样式 */
.text-segment :deep(blockquote) {
  margin: 10px 0;
  padding: 8px 16px;
  border-left: 4px solid #409eff;
  background: #f5f7fa;
  color: #606266;
  border-radius: 0 4px 4px 0;
}

.text-segment :deep(blockquote p) {
  margin: 4px 0;
}

.is-user .text-segment :deep(blockquote) {
  border-left-color: rgba(255, 255, 255, 0.6);
  background: rgba(255, 255, 255, 0.1);
  color: rgba(255, 255, 255, 0.9);
}

/* 粗体和斜体 */
.text-segment :deep(strong),
.text-segment :deep(b) {
  font-weight: 600;
}

.text-segment :deep(em),
.text-segment :deep(i) {
  font-style: italic;
}

/* 链接样式 */
.text-segment :deep(a) {
  color: #409eff;
  text-decoration: none;
  border-bottom: 1px solid transparent;
  transition: border-color 0.2s;
}

.text-segment :deep(a:hover) {
  border-bottom-color: #409eff;
}

.is-user .text-segment :deep(a) {
  color: rgba(255, 255, 255, 0.9);
  border-bottom-color: rgba(255, 255, 255, 0.3);
}

.is-user .text-segment :deep(a:hover) {
  border-bottom-color: rgba(255, 255, 255, 0.9);
}

/* 分隔线 */
.text-segment :deep(hr) {
  border: none;
  border-top: 1px solid #ebeef5;
  margin: 16px 0;
}

/* 表格样式 */
.text-segment :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 13px;
}

.text-segment :deep(th),
.text-segment :deep(td) {
  border: 1px solid #ebeef5;
  padding: 8px 12px;
  text-align: left;
}

.text-segment :deep(th) {
  background: #f5f7fa;
  font-weight: 600;
}

/* ========================================
   非命令代码块样式
   ======================================== */

.text-segment :deep(pre) {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px 16px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 10px 0;
  font-size: 13px;
  line-height: 1.5;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}

.text-segment :deep(code) {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}

/* 行内代码 */
.text-segment :deep(code:not(pre code)) {
  background: rgba(0, 0, 0, 0.06);
  color: #e6a23c;
  padding: 2px 6px;
  border-radius: 3px;
  font-size: 13px;
}

.is-user .text-segment :deep(code:not(pre code)) {
  background: rgba(255, 255, 255, 0.2);
  color: rgba(255, 255, 255, 0.9);
}

.is-user .text-segment :deep(pre) {
  background: rgba(0, 0, 0, 0.15);
  color: rgba(255, 255, 255, 0.95);
}

/* 命令块可用标记 */
.bubble-body.is-command-available {
  /* 预留扩展点 */
}
</style>
