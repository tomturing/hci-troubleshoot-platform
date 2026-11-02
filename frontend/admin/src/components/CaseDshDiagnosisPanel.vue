/**
 * 工单详情 · DSH 诊断记录。
 *
 * 记录从哪来：DSH 没有给平台留 API，也没有回调。平台侧唯一的通道是它家目录下的
 * 会话文件（`$DSH_HOME/sessions/**/session.v4.jsonl.zstd`，多帧 zstd），
 * 后端解析后落到 `dsh_diagnosis_session`，这里只做展示。
 *
 * 展示顺序刻意是「结论 → 对话」：复盘时先要答案，再决定要不要看过程。
 * 工具调用不折叠成一行是什么意思？它保留「AI 真的读过哪些日志」这条证据链，
 * 只截断单条长度（后端已截），不做隐藏。
 */
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { DshMessage, DshSession, DshSessionListResponse } from '@/api/caseInsights'

const props = defineProps<{
  caseId: string
  data: DshSessionListResponse | null
  loading: boolean
}>()

const emit = defineEmits<{ refresh: [] }>()

const selectedId = ref<string>('')

watch(
  () => props.data,
  (value) => {
    const items = value?.items ?? []
    if (!items.some((item) => item.session_id === selectedId.value)) {
      selectedId.value = items[0]?.session_id ?? ''
    }
  },
  { immediate: true },
)

const sessions = computed<DshSession[]>(() => props.data?.items ?? [])
const selected = computed<DshSession | null>(
  () => sessions.value.find((item) => item.session_id === selectedId.value) ?? sessions.value[0] ?? null,
)
const rootMissing = computed(() => props.data?.ingest?.root_missing === true)

const bindingLabels: Record<string, string> = {
  wecom_marker: 'WeCom 日志任务目录',
  cwd: '会话工作目录',
  first_user: '首条消息中的工单号',
  title: '会话标题中的工单号',
  none: '未识别',
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN')
}

function renderMarkdown(text: string): string {
  return DOMPurify.sanitize(marked.parse(text) as string)
}

function roleLabel(role: DshMessage['role']): string {
  if (role === 'user') return '用户'
  if (role === 'assistant') return 'AI'
  if (role === 'tool') return '工具'
  return '系统'
}

function roleTagType(role: DshMessage['role']): 'success' | 'warning' | 'info' | 'danger' {
  if (role === 'user') return 'warning'
  if (role === 'assistant') return 'success'
  if (role === 'tool') return 'info'
  return 'danger'
}
</script>

<template>
  <div v-loading="loading" class="dsh-panel">
    <div class="dsh-toolbar">
      <span class="dsh-summary">
        共 <strong>{{ sessions.length }}</strong> 条 DSH 诊断会话
        <span class="dsh-muted">（记录来自 DSH 会话文件归档，不是平台重新推理的结果）</span>
      </span>
      <el-button size="small" :loading="loading" @click="emit('refresh')">刷新并归档</el-button>
    </div>

    <el-alert
      v-if="rootMissing"
      type="warning"
      :closable="false"
      show-icon
      title="未读到 DSH 会话目录"
      description="case-service 未挂载 DSH 家目录（caseService.dshSessions.enabled=false 或宿主目录不存在），因此这里只能显示历史归档记录。"
      style="margin-bottom: 12px"
    />

    <el-empty
      v-if="sessions.length === 0"
      description="该工单还没有 DSH 诊断记录（在 DSH 工作台诊断并产生会话后，点「刷新并归档」即可看到）"
    />

    <div v-else class="dsh-body">
      <aside class="dsh-sessions">
        <div
          v-for="(session, index) in sessions"
          :key="session.session_id"
          class="dsh-session-item"
          :class="{ active: session.session_id === (selected?.session_id ?? '') }"
          @click="selectedId = session.session_id"
        >
          <div class="dsh-session-title">{{ session.title || `未命名会话 #${index + 1}` }}</div>
          <div class="dsh-session-meta">
            <span>{{ formatDate(session.started_at) }}</span>
            <span>对话 {{ session.message_count }} 条</span>
            <span>工具 {{ session.tool_call_count }} 次</span>
          </div>
          <div class="dsh-session-tags">
            <el-tag size="small" type="info">{{ bindingLabels[session.binding_source] || session.binding_source }}</el-tag>
            <el-tag v-if="session.truncated" size="small" type="warning">已截断</el-tag>
          </div>
        </div>
      </aside>

      <section v-if="selected" class="dsh-detail">
        <div class="dsh-detail-head">
          <div>
            <div class="dsh-detail-title">{{ selected.title || '未命名会话' }}</div>
            <div class="dsh-session-meta">
              <span>会话 {{ selected.session_id }}</span>
              <span>{{ formatDate(selected.started_at) }}</span>
              <span>最后活动 {{ formatDate(selected.last_activity_at) }}</span>
              <span v-if="selected.workspace">工作目录 {{ selected.workspace }}</span>
            </div>
          </div>
        </div>

        <!-- 结论优先 -->
        <div class="dsh-conclusion">
          <div class="dsh-conclusion-head">
            <span class="dsh-conclusion-title">最终结论</span>
            <span v-if="!selected.conclusion_markdown" class="dsh-muted">会话尚未产出结论（可能仍在进行）</span>
          </div>
          <div
            v-if="selected.conclusion_markdown"
            class="dsh-markdown"
            v-html="renderMarkdown(selected.conclusion_markdown)"
          ></div>
        </div>

        <!-- 对话过程 -->
        <el-collapse class="dsh-transcript">
          <el-collapse-item :title="`完整对话（${selected.messages.length} 条事件）`" name="transcript">
            <div v-for="(msg, index) in selected.messages" :key="index" class="dsh-msg" :class="`dsh-msg-${msg.role}`">
              <div class="dsh-msg-head">
                <el-tag :type="roleTagType(msg.role)" size="small">{{ roleLabel(msg.role) }}</el-tag>
                <span v-if="msg.name" class="dsh-msg-name">{{ msg.name }}</span>
                <el-tag v-if="msg.is_error" type="danger" size="small">失败</el-tag>
                <span class="dsh-muted">{{ formatDate(msg.time) }}</span>
              </div>
              <div v-if="msg.role === 'assistant' && msg.text" class="dsh-markdown" v-html="renderMarkdown(msg.text)"></div>
              <pre v-else-if="msg.text" class="dsh-pre">{{ msg.text }}</pre>
              <div v-if="msg.tool_calls?.length" class="dsh-tool-calls">
                <div v-for="(call, callIndex) in msg.tool_calls" :key="callIndex" class="dsh-tool-call">
                  <span class="dsh-tool-name">{{ call.name }}</span>
                  <code>{{ call.arguments }}</code>
                </div>
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dsh-panel {
  min-height: 200px;
}

.dsh-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.dsh-summary {
  font-size: 13px;
  color: #606266;
}

.dsh-muted {
  color: #909399;
  font-size: 12px;
  margin-left: 8px;
}

.dsh-body {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
}

.dsh-sessions {
  max-height: 640px;
  overflow-y: auto;
  border-right: 1px solid #ebeef5;
  padding-right: 8px;
}

.dsh-session-item {
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 8px;
  border: 1px solid transparent;
}

.dsh-session-item:hover {
  background: #f5f7fa;
}

.dsh-session-item.active {
  background: #ecf5ff;
  border-color: #b3d8ff;
}

.dsh-session-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dsh-session-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
  color: #909399;
}

.dsh-session-tags {
  margin-top: 6px;
  display: flex;
  gap: 6px;
}

.dsh-detail {
  min-width: 0;
}

.dsh-detail-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 4px;
}

.dsh-conclusion {
  margin: 16px 0;
  padding: 14px 16px;
  background: #f0f9eb;
  border: 1px solid #e1f3d8;
  border-radius: 6px;
}

.dsh-conclusion-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.dsh-conclusion-title {
  font-size: 14px;
  font-weight: 600;
  color: #529b2e;
}

.dsh-markdown {
  font-size: 13px;
  line-height: 1.7;
  color: #303133;
  word-break: break-word;
}

.dsh-markdown :deep(pre) {
  background: #f5f7fa;
  padding: 10px;
  border-radius: 4px;
  overflow-x: auto;
}

.dsh-markdown :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
}

.dsh-markdown :deep(th),
.dsh-markdown :deep(td) {
  border: 1px solid #ebeef5;
  padding: 6px 8px;
  text-align: left;
}

.dsh-transcript {
  margin-top: 8px;
}

.dsh-msg {
  padding: 10px 0;
  border-bottom: 1px solid #f2f2f2;
}

.dsh-msg:last-child {
  border-bottom: none;
}

.dsh-msg-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.dsh-msg-name {
  font-size: 12px;
  font-weight: 600;
  color: #606266;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.dsh-pre {
  margin: 0;
  padding: 8px 10px;
  background: #fafafa;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.5;
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.dsh-msg-tool .dsh-pre {
  background: #f5f7fa;
  color: #606266;
}

.dsh-msg-user .dsh-pre {
  background: #ecf5ff;
}

.dsh-tool-calls {
  margin-top: 6px;
}

.dsh-tool-call {
  font-size: 12px;
  color: #606266;
  word-break: break-all;
  margin-bottom: 4px;
}

.dsh-tool-name {
  font-weight: 600;
  margin-right: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

@media (max-width: 1200px) {
  .dsh-body {
    grid-template-columns: 1fr;
  }

  .dsh-sessions {
    border-right: none;
    max-height: 260px;
  }
}
</style>
