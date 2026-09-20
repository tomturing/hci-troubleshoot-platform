<script setup lang="ts">
/**
 * BridgeLogUploader —— terminal_bridge 本地日志补采入口
 *
 * 背景：自动回采链路（bridge → WebSocket → 浏览器 → 后端）依赖浏览器在线，
 * 断网 / 页面关闭 / 回采异常时桥侧证据会缺失，导致诊断失败无法定因
 * （工单 Q2026092010235）。本组件提供兜底通道：用户直接上传 bridge 写在本地
 * （Windows 桌面 HCI-TerminalBridge-Logs 目录）的 JSONL 日志。
 *
 * 该入口受后端开关控制（bridgeLogs.uploadEnabled），自动回采稳定后可整体屏蔽。
 */
import { ref, computed } from 'vue'
import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()

const dialogVisible = ref(false)
const uploading = ref(false)
const resultMessage = ref('')
const resultOk = ref<boolean | null>(null)
const selectedFiles = ref<File[]>([])
const fileInput = ref<HTMLInputElement | null>(null)

const hasCase = computed(() => Boolean(chatStore.currentCase?.case_id))

function openDialog() {
  resultMessage.value = ''
  resultOk.value = null
  selectedFiles.value = []
  dialogVisible.value = true
}

function pickFiles() {
  fileInput.value?.click()
}

function onFilesChanged(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFiles.value = Array.from(input.files || [])
}

function onDrop(event: DragEvent) {
  event.preventDefault()
  selectedFiles.value = Array.from(event.dataTransfer?.files || [])
}

function dismissHint() {
  chatStore.clearBridgeLogUploadHint()
}

async function submitUpload() {
  if (!selectedFiles.value.length) {
    resultOk.value = false
    resultMessage.value = '请先选择日志文件（桌面 HCI-TerminalBridge-Logs 目录下的 bridge-*.log）'
    return
  }
  uploading.value = true
  resultMessage.value = ''
  try {
    const result = await chatStore.uploadBridgeLogs(selectedFiles.value)
    resultOk.value = result.ok
    resultMessage.value = result.message
    if (result.ok) {
      chatStore.clearBridgeLogUploadHint()
    }
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <div class="bridge-log-uploader">
    <!-- 诊断未完成时的补采提醒 -->
    <el-alert
      v-if="chatStore.bridgeLogUploadHint"
      type="warning"
      show-icon
      :closable="true"
      title="诊断未完成，建议上传本地日志"
      class="upload-hint"
      @close="dismissHint"
    >
      <template #default>
        <div class="hint-body">
          <p>
            自动回采的 bridge 日志可能不完整，导致无法定位根因。请把 terminal_bridge 在本地
            （Windows 桌面 <code>HCI-TerminalBridge-Logs</code> 目录）生成的
            <code>bridge-*.log</code> 文件上传补齐。
          </p>
          <div class="hint-actions">
            <el-button size="small" type="primary" @click="openDialog">上传本地日志</el-button>
            <el-button size="small" @click="dismissHint">稍后处理</el-button>
          </div>
        </div>
      </template>
    </el-alert>

    <!-- 常驻入口（工单存在时可见，便于随时补采） -->
    <el-button
      v-if="!chatStore.bridgeLogUploadHint && hasCase"
      size="small"
      text
      class="upload-entry"
      @click="openDialog"
    >
      上传本地日志
    </el-button>

    <el-dialog v-model="dialogVisible" title="上传 terminal_bridge 本地日志" width="560px">
      <div class="uploader-body">
        <p class="tip">
          请选择 terminal_bridge 本地日志文件（默认位于桌面
          <code>HCI-TerminalBridge-Logs</code> 目录，文件名形如
          <code>bridge-20260920-abcd1234.log</code>），支持多选。
        </p>
        <div
          class="drop-area"
          @dragover.prevent
          @drop="onDrop"
          @click="pickFiles"
        >
          <span v-if="!selectedFiles.length">点击选择文件，或将日志文件拖拽到此处</span>
          <ul v-else class="file-list">
            <li v-for="file in selectedFiles" :key="file.name">
              {{ file.name }}（{{ (file.size / 1024).toFixed(1) }} KB）
            </li>
          </ul>
        </div>
        <input
          ref="fileInput"
          type="file"
          multiple
          accept=".log,.jsonl,.txt,text/plain"
          class="hidden-input"
          @change="onFilesChanged"
        />
        <p v-if="resultMessage" :class="['result', resultOk ? 'ok' : 'error']">
          {{ resultMessage }}
        </p>
      </div>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitUpload">
          开始上传
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.bridge-log-uploader {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.upload-hint {
  margin-bottom: 4px;
}

.hint-body p {
  margin: 0 0 8px;
  line-height: 1.6;
}

.hint-actions {
  display: flex;
  gap: 8px;
}

.tip {
  margin: 0 0 12px;
  line-height: 1.6;
  color: #606266;
}

.drop-area {
  border: 1px dashed #c0c4cc;
  border-radius: 6px;
  padding: 20px;
  text-align: center;
  color: #909399;
  cursor: pointer;
  min-height: 80px;
}

.drop-area:hover {
  border-color: #409eff;
  color: #409eff;
}

.file-list {
  list-style: none;
  margin: 0;
  padding: 0;
  text-align: left;
  color: #303133;
}

.file-list li {
  padding: 2px 0;
}

.hidden-input {
  display: none;
}

.result {
  margin: 12px 0 0;
  line-height: 1.6;
}

.result.ok {
  color: #67c23a;
}

.result.error {
  color: #f56c6c;
}

.upload-entry {
  align-self: flex-start;
  padding-left: 0;
}
</style>
