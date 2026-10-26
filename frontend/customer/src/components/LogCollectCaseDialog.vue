<script setup lang="ts">
/**
 * 人工指定工单号（日志收集）
 *
 * 为什么需要：
 * - 日志任务必须挂在平台工单上（`log_collect_task.case_id` 是必填外键），而入口已改为
 *   常驻，客户在"无活跃工单"（刚进页面 / 工单已关闭）时也需要能补采日志。
 * - 归属边界不变：只允许当前客户端名下工单，校验完全交给服务端
 *   `GET /api/cases/{case_id}`（`verify_case_ownership` 对不存在/非归属统一 404）。
 *   前端不做存在性判断，避免把这里变成枚举他人工单的接口。
 *
 * 交互（2026-10-26 收敛为一步）：弹框内不再有独立的「连接」按钮，也不再弹右侧
 * SSH 终端抽屉。用户填完工单号 + SSH 信息后只点「确定」：
 *   ① 本地校验工单号 → ② 未登录则先登录（内嵌登录区就地反馈进度/失败）
 *   → ③ 登录成功才提交工单 → ④ 直接打开左侧「日志收集」面板。
 */
import { computed, ref, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import { checkBridgeBeforeOpen } from '@/api/terminal'
import SshConnectSection from './SshConnectSection.vue'

const chatStore = useChatStore()

const caseId = ref('')
const errorMessage = ref('')
const bridgeStatus = ref<'running' | 'not-running' | 'checking'>('checking')
/** 「确定」的进行中状态：一次点击可能包含 SSH 登录 + 工单解析两段等待 */
const submitting = ref(false)
/** 内嵌登录区：由「确定」驱动登录（连接按钮已移除） */
const sshSectionRef = ref<InstanceType<typeof SshConnectSection> | null>(null)

const sshConnected = computed(() => chatStore.sshConnectionState === 'connected')

/** 进行中的 bridge 检测：并发点击「确定」/ 打开弹框时复用同一次探测，不叠加 3s 超时 */
let bridgeCheckPromise: Promise<void> | null = null

/**
 * 与 SSH 终端一致的 bridge 前置检测（3s 超时），已连接时跳过。
 *
 * 返回 Promise 是为了让「确定」能等出结论：用户填完就点下去时探测可能还在飞，
 * 同步读 bridgeStatus 只会读到 'checking' 并误报"Bridge 未运行"。
 */
function detectBridge(): Promise<void> {
  if (sshConnected.value) return Promise.resolve()
  if (bridgeCheckPromise) return bridgeCheckPromise
  bridgeStatus.value = 'checking'
  bridgeCheckPromise = (async () => {
    try {
      bridgeStatus.value = (await checkBridgeBeforeOpen()) === 'running' ? 'running' : 'not-running'
    } catch {
      bridgeStatus.value = 'not-running'
    } finally {
      bridgeCheckPromise = null
    }
  })()
  return bridgeCheckPromise
}

// 直接列出当前客户端名下的最近工单（含已关闭），避免用户手输时把外部 ICare 单号
// 或别的环境的号填进来。列表来源是 initialize 已加载的 existingCases，不额外发请求。
const recentCases = computed(() => chatStore.existingCases.slice(0, 8))

const STATUS_TEXT: Record<string, string> = {
  created: '新建',
  in_progress: '处理中',
  confirmed: '已确认',
  resolved: '已解决',
  closed: '已关闭',
  cancelled: '已取消',
}

function statusText(status: string): string {
  return STATUS_TEXT[status] || status
}

// 与自动生成规则一致：Q + 8 位日期 + 5 位序号
const CASE_ID_PATTERN = /^Q\d{8}\d{5}$/

/** 日期段必须真实存在（20261032 这类"格式合法但日期非法"的号本地就拦掉）。 */
function isValidCaseId(value: string): boolean {
  if (!CASE_ID_PATTERN.test(value)) return false
  const year = Number(value.slice(1, 5))
  const month = Number(value.slice(5, 7))
  const day = Number(value.slice(7, 9))
  const parsed = new Date(Date.UTC(year, month - 1, day))
  return (
    parsed.getUTCFullYear() === year &&
    parsed.getUTCMonth() === month - 1 &&
    parsed.getUTCDate() === day
  )
}

watch(
  () => chatStore.logCollectCaseDialogVisible,
  (open) => {
    if (!open) return
    caseId.value = chatStore.logCollectCaseId ?? ''
    errorMessage.value = ''
    void detectBridge()
  },
)

/**
 * 「确定」= 登录 + 提交（单入口）。
 *
 * 顺序说明：先校验工单号再登录。bridge 的会话键是 `caseID@nodeIP`，
 * 若带着非法工单号登录，会话会挂在一个服务端不认的键上，后续采日志必然
 * session_missing —— 所以宁可在点下去的第一时间把工单号拦掉。
 */
async function submit() {
  if (submitting.value) return
  errorMessage.value = ''
  const value = caseId.value.trim()
  if (!isValidCaseId(value)) {
    errorMessage.value = '工单号格式不正确，应为 Q + 8 位日期 + 5 位序号（如 Q2026101401684）'
    return
  }

  submitting.value = true
  try {
    if (!sshConnected.value) {
      // 结论未知就现探一次（用户可能刚启动 bridge）；已有 running 就不再打扰
      if (bridgeStatus.value !== 'running') await detectBridge()
      if (bridgeStatus.value !== 'running') {
        errorMessage.value = 'SSH Bridge 未运行：请先下载并启动 terminal_bridge.exe，再点击「确定」'
        return
      }
      // 登录失败 / 字段缺失由内嵌登录区就地提示（toast + 区内错误态），这里不重复报错
      const ok = await sshSectionRef.value?.connect()
      if (!ok) return
    }
    await chatStore.confirmLogCollectCase(value)
  } catch (e: any) {
    errorMessage.value = e?.message || '工单校验失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="chatStore.logCollectCaseDialogVisible"
    title="指定工单号"
    width="560px"
    :close-on-click-modal="false"
    @close="chatStore.closeLogCollectCaseDialog()"
  >
    <p class="lc-case-tip">
      输入一个平台工单号（如 Q2026101401684）。<b>已存在会自动复用，不存在会按该号建档</b>，
      日志收集只追加任务，不影响该工单的聊天内容。
    </p>
    <el-input
      v-model="caseId"
      placeholder="Q2026102300001"
      maxlength="20"
      clearable
      @keyup.enter="submit"
    />

    <div v-if="recentCases.length" class="lc-case-list">
      <p class="lc-case-list-title">或从我的工单中选择</p>
      <button
        v-for="item in recentCases"
        :key="item.case_id"
        type="button"
        class="lc-case-item"
        :class="{ 'is-selected': caseId === item.case_id }"
        @click="caseId = item.case_id"
      >
        <span class="lc-case-item-id">{{ item.case_id }}</span>
        <span class="lc-case-item-title">{{ item.title || '（无标题）' }}</span>
        <span class="lc-case-item-status">{{ statusText(item.status) }}</span>
      </button>
    </div>
    <p v-else class="lc-case-hint">当前客户端暂无历史工单，请先在对话中创建工单。</p>

    <!-- SSH 登录：与 SSH 终端共用同一模板（SshConnectSection），强依赖先登录 -->
    <div class="lc-ssh-block">
      <p class="lc-ssh-title">登录 HCI（日志收集依赖 SSH 会话）</p>
      <!--
        case-id 必须传：bridge 的会话键是 caseID@nodeIP。不传时 connectSSH 会退化成
        "terminal-only"，而日志收集面板按工单号取会话 → 必然 session_missing。

        show-connect-button=false：去掉独立的「连接」按钮，登录与提交合并到「确定」；
        open-terminal=false：登录成功不弹右侧 SSH 终端抽屉，直接进左侧日志收集面板。
      -->
      <SshConnectSection
        ref="sshSectionRef"
        :bridge-status="bridgeStatus"
        :case-id="caseId"
        mode="inline"
        :show-connect-button="false"
        :open-terminal="false"
      />
      <p v-if="!sshConnected" class="lc-ssh-hint">
        点击「确定」会先登录 SSH，登录成功后再提交并打开日志收集；发起节点取自已连接的 SSH 会话。
      </p>
    </div>

    <p v-if="errorMessage" class="lc-case-error">{{ errorMessage }}</p>
    <p class="lc-case-hint">
      换设备或清理过浏览器缓存时，旧工单可能不属于当前客户端，需要在原浏览器中操作。
      已结单工单可以指定，日志将归档但不改变工单状态。
    </p>
    <template #footer>
      <el-button @click="chatStore.closeLogCollectCaseDialog()">取消</el-button>
      <!-- 不再因"未登录"禁用：点击即先登录再提交（登录失败时区内就地报错，不提交工单） -->
      <el-button
        type="primary"
        :loading="submitting || chatStore.logCollectCaseLoading"
        @click="submit"
      >
        确定
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.lc-case-tip {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.6;
  color: #303133;
}

.lc-case-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: #f56c6c;
}

.lc-case-hint {
  margin: 12px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: #909399;
}

.lc-case-list {
  margin-top: 12px;
}

.lc-case-list-title {
  margin: 0 0 6px;
  font-size: 12px;
  color: #909399;
}

.lc-case-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 10px;
  margin-bottom: 4px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fff;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.lc-case-item:hover {
  border-color: #c6e2ff;
  background: #f5f9ff;
}

.lc-case-item.is-selected {
  border-color: #409eff;
  background: #ecf5ff;
}

.lc-case-item-id {
  flex-shrink: 0;
  font-weight: 600;
  color: #337ecc;
}

.lc-case-item-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: #606266;
}

.lc-case-item-status {
  flex-shrink: 0;
  color: #909399;
}

.lc-ssh-block {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px dashed #e4e7ed;
}

.lc-ssh-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.lc-ssh-hint {
  margin: 8px 0 0;
  font-size: 12px;
  color: #e6a23c;
}

/* 内嵌 SSH 表单后弹框变高：正文滚动，按钮固定在 footer */
:deep(.el-dialog__body) {
  max-height: 70vh;
  overflow-y: auto;
}
</style>
