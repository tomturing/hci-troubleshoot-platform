<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createApiClient,
  createOfflineDiagnosisApi,
  type CollectionPlan,
  type CollectorArtifact,
  type CollectorArtifactItem,
  type DiagnosisReport,
  type DiagnosisSession,
  type DiagnosisUploadSession,
  type EvidenceAssessment,
  type EvidenceBundle,
  type MissingEvidence,
  type OfflineScenario,
  type OfflineScenarioOption,
} from '@hci/shared'
import { formatPlanTarget, getPlanTargetNodes, resolveArtifactTarget } from '@/utils/offlineDiagnosis'

const props = defineProps<{ caseId: string; standalone?: boolean }>()
const visible = defineModel<boolean>({ required: true })

// 正式环境由同源身份层注入短期访问令牌，构建产物不得携带内部服务令牌。
const developmentToken = import.meta.env.DEV
  ? import.meta.env.VITE_DIAGNOSIS_TOKEN || import.meta.env.VITE_INTERNAL_API_TOKEN || ''
  : ''
const usesSameOriginIdentity = import.meta.env.PROD
const getIdentityToken = () => developmentToken || window.__HCI_AUTH__?.getAccessToken?.()
const tenantId = import.meta.env.VITE_DIAGNOSIS_TENANT_ID || undefined
const actorId = import.meta.env.VITE_DIAGNOSIS_ACTOR_ID || undefined
const client = createApiClient('/api')
const api = createOfflineDiagnosisApi(client, { token: getIdentityToken, tenantId, actorId })

const scenarios = ref<OfflineScenarioOption[]>([])
const scenariosLoading = ref(false)
const scenariosError = ref('')
const reportLevelLabels: Record<DiagnosisReport['diagnosis_level'], string> = {
  Confirmed: '已确认',
  Probable: '高概率',
  Suspected: '疑似',
  Insufficient: '证据不足',
  Conflicted: '证据冲突',
}
const missingEvidenceStatusLabels: Record<string, string> = {
  missing: '未采集',
  collection_failed: '采集失败',
  out_of_time_range: '不在故障时间窗',
  not_applicable: '当前环境不适用',
  unreadable: '证据不可读取',
  skipped_by_user: '用户未提供',
  assessment_link_mismatch: '历史评估关联异常',
}

const form = ref({
  scenario: '' as OfflineScenario,
  productVersion: '',
  objectType: 'vm',
  objectId: '',
  objectName: '',
  sourceNode: '',
  startTime: '',
  endTime: '',
  impactScope: 'single_object',
  currentStatus: 'ongoing' as 'ongoing' | 'recovered' | 'intermittent',
  recentChangeDescription: '',
})
const session = ref<DiagnosisSession | null>(null)
const plan = ref<CollectionPlan | null>(null)
const artifact = ref<CollectorArtifact | null>(null)
const artifactTargetNode = ref('')
const file = ref<File | null>(null)
const currentUpload = ref<DiagnosisUploadSession | null>(null)
const uploadProgress = ref(0)
const bundles = ref<EvidenceBundle[]>([])
const assessment = ref<EvidenceAssessment | null>(null)
const reports = ref<DiagnosisReport[]>([])
const busy = ref(false)
const refreshing = ref(false)
let pollTimer: number | undefined

const activeReport = computed(() => reports.value[0] || null)
const publishedReport = computed(
  () => reports.value.find((item) => item.publish_status === 'customer_published') || null,
)
const canCreate = computed(() =>
  Boolean(
    form.value.scenario &&
      form.value.productVersion.trim() &&
      (!selectedScenario.value?.requires_affected_object || form.value.objectId.trim()) &&
      form.value.sourceNode.trim() &&
      form.value.startTime &&
      form.value.endTime,
  ),
)
const selectedScenario = computed(() => scenarios.value.find((item) => item.scenario === form.value.scenario) || null)
const planTargetNodes = computed(() => getPlanTargetNodes(plan.value))
const artifactPlanItems = computed(() => {
  const artifactItemIds = new Set((artifact.value?.items || []).map((item) => item.plan_item_id))
  return (plan.value?.items || []).filter(
    (item) => item.activation_state === 'active' && (!artifact.value || artifactItemIds.has(item.item_id)),
  )
})
const canGenerateArtifact = computed(() => Boolean(session.value && plan.value && artifactTargetNode.value.trim()))
const latestBundle = computed(() => bundles.value[0] || null)
const processingFailed = computed(() =>
  bundles.value.some((item) => ['rejected', 'failed'].includes(item.processing_status)),
)
const currentStep = computed(() => {
  if (publishedReport.value) return 6
  if (activeReport.value) return 5
  if (assessment.value || bundles.value.length) return 4
  if (currentUpload.value || file.value) return 3
  if (artifact.value) return 2
  if (session.value) return 1
  return 0
})

async function loadScenarios() {
  scenariosLoading.value = true
  scenariosError.value = ''
  try {
    scenarios.value = (await api.listScenarios()).data
    if (!scenarios.value.some((item) => item.scenario === form.value.scenario)) {
      form.value.scenario = scenarios.value[0]?.scenario || ''
    }
  } catch (error) {
    scenarios.value = []
    form.value.scenario = ''
    scenariosError.value = errorMessage(error)
  } finally {
    scenariosLoading.value = false
  }
}

watch(visible, (value) => {
  if (value && !session.value) void loadScenarios()
})

onMounted(async () => {
  await loadScenarios()
  await restoreWorkspace()
})

function stableRequestKey(kind: string, payload: unknown): string {
  const storageKey = `hci-offline:${props.caseId}:${kind}`
  const serialized = JSON.stringify(payload)
  const existing = sessionStorage.getItem(storageKey)
  if (existing) {
    try {
      const parsed = JSON.parse(existing) as { payload: string; key: string }
      if (parsed.payload === serialized && parsed.key) return parsed.key
    } catch {
      // 损坏的本地恢复信息直接覆盖，不影响服务端事实源。
    }
  }
  const key = crypto.randomUUID()
  sessionStorage.setItem(storageKey, JSON.stringify({ payload: serialized, key }))
  return key
}

function rememberWorkspace() {
  sessionStorage.setItem(
    `hci-offline:${props.caseId}:workspace`,
    JSON.stringify({
      session_id: session.value?.session_id,
      plan_id: plan.value?.collection_plan_id,
      artifact_id: artifact.value?.artifact_id,
    }),
  )
}

async function restoreWorkspace() {
  if (!props.caseId || session.value) return
  try {
    const workspace = (await api.resumeWorkspace(props.caseId)).data
    session.value = workspace.session
    form.value.scenario = workspace.session.selected_scenario
    if (workspace.plan_id) {
      plan.value = (await api.getPlan(workspace.session.session_id, workspace.plan_id)).data
      form.value.productVersion = plan.value.product_version
    }
    if (workspace.artifact_id) {
      artifact.value = (await api.getArtifact(workspace.session.session_id, workspace.artifact_id)).data
    }
    artifactTargetNode.value = resolveArtifactTarget(plan.value, form.value.sourceNode)
    rememberWorkspace()
    if (workspace.active_upload_id) {
      ElMessage.warning('检测到刷新前未完成的上传；为保护一次性上传令牌，请重新选择证据包开始新上传')
    }
    await refresh()
    if (!reports.value.length && !processingFailed.value) startPolling()
  } catch (error) {
    const status = (error as { response?: { status?: number } }).response?.status
    if (status && status !== 404) ElMessage.error(errorMessage(error))
  }
}

async function createCollector() {
  if (!canCreate.value) return
  busy.value = true
  try {
    const sessionPayload = {
      case_id: props.caseId,
      selected_scenario: form.value.scenario,
      incident: {
        start_time: new Date(form.value.startTime).toISOString(),
        end_time: new Date(form.value.endTime).toISOString(),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      },
      affected_objects:
        form.value.objectId.trim() || form.value.sourceNode.trim()
          ? [
              {
                type: form.value.objectType,
                id: form.value.objectId.trim() || undefined,
                name: form.value.objectName.trim() || undefined,
                source_node: form.value.sourceNode.trim() || undefined,
              },
            ]
          : [],
      impact_scope: form.value.impactScope,
      current_status: form.value.currentStatus,
      recent_change_description: form.value.recentChangeDescription.trim() || undefined,
    }
    const sessionResponse = await api.createSession(
      sessionPayload,
      stableRequestKey('create-session', sessionPayload),
    )
    session.value = sessionResponse.data
    plan.value = (
      await api.createPlan(
        session.value.session_id,
        form.value.productVersion,
        stableRequestKey('create-plan', {
          session_id: session.value.session_id,
          product_version: form.value.productVersion,
        }),
      )
    ).data
    artifactTargetNode.value = resolveArtifactTarget(plan.value, form.value.sourceNode)
    artifact.value = await requestArtifact(plan.value.collection_plan_id, artifactTargetNode.value)
    rememberWorkspace()
    ElMessage.success('离线采集工具已准备好，请下载后在故障环境执行')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    busy.value = false
  }
}

async function retryCreatePlan() {
  if (!session.value || !form.value.productVersion.trim()) return
  busy.value = true
  try {
    plan.value = (
      await api.createPlan(
        session.value.session_id,
        form.value.productVersion.trim(),
        stableRequestKey('create-plan', {
          session_id: session.value.session_id,
          product_version: form.value.productVersion.trim(),
        }),
      )
    ).data
    artifactTargetNode.value = resolveArtifactTarget(plan.value, form.value.sourceNode)
    artifact.value = await requestArtifact(plan.value.collection_plan_id, artifactTargetNode.value)
    rememberWorkspace()
    ElMessage.success('离线采集工具已准备好，请下载后在故障环境执行')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    busy.value = false
  }
}

async function requestArtifact(planId: string, targetNode: string): Promise<CollectorArtifact> {
  if (!session.value) throw new Error('离线诊断任务尚未创建')
  const normalizedTarget = targetNode.trim()
  if (!normalizedTarget) throw new Error('请先指定采集工具执行节点')
  return (
    await api.createArtifact(
      session.value.session_id,
      planId,
      normalizedTarget,
      stableRequestKey('create-artifact', { plan_id: planId, target_node: normalizedTarget }),
    )
  ).data
}

async function generateArtifact() {
  if (!session.value || !plan.value || !canGenerateArtifact.value) {
    ElMessage.warning('请先指定采集工具执行节点')
    return
  }
  busy.value = true
  try {
    artifact.value = await requestArtifact(plan.value.collection_plan_id, artifactTargetNode.value)
    ElMessage.success('离线采集工具已生成，可立即下载')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    busy.value = false
  }
}

async function download(path: string, fileName: string) {
  try {
    // 服务端返回站点绝对路径，客户端已配置 /api 前缀，下载前需去除重复前缀。
    const relativePath = path.startsWith('/api/') ? path.slice('/api'.length) : path
    const response = await client.get(relativePath, { headers: identityHeaders(), responseType: 'blob' })
    const url = URL.createObjectURL(response.data)
    const link = document.createElement('a')
    link.href = url
    link.download = fileName
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

function onFileChange(uploadFile: { raw?: File }) {
  const selected = uploadFile.raw || null
  if (selected && selected.size > 512 * 1024 * 1024) {
    file.value = null
    ElMessage.error('证据包不能超过 512 MiB，请缩小采集范围后重新生成')
    return
  }
  file.value = selected
}

async function uploadEvidence() {
  if (!session.value || !plan.value || !artifact.value || !file.value) return
  busy.value = true
  uploadProgress.value = 0
  try {
    currentUpload.value = (
      await api.createUpload(session.value.session_id, {
        bundle_type: 'initial',
        collection_plan_id: plan.value.collection_plan_id,
        collector_artifact_id: artifact.value.artifact_id,
        file_name: file.value.name,
        media_type: 'application/vnd.hci.evidence',
        total_size_bytes: file.value.size,
      })
    ).data
    await resumeUpload()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    busy.value = false
  }
}

async function resumeUpload() {
  if (!session.value || !currentUpload.value || !file.value) return
  busy.value = true
  try {
    const status = await api.getUpload(session.value.session_id, currentUpload.value.upload_id)
    const uploadedParts = Object.keys(status.data.uploaded_parts).map(Number)
    const parts = await api.uploadFile(
      currentUpload.value,
      file.value,
      (value) => (uploadProgress.value = value),
      uploadedParts,
    )
    await api.completeUpload(session.value.session_id, currentUpload.value.upload_id, parts)
    currentUpload.value = null
    ElMessage.success('证据包上传完成，平台正在进行安全检查和诊断')
    startPolling()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    busy.value = false
  }
}

async function abortUpload() {
  if (!session.value || !currentUpload.value) return
  try {
    await ElMessageBox.confirm('终止后需要重新上传证据包，是否继续？', '终止上传', { type: 'warning' })
    await api.abortUpload(session.value.session_id, currentUpload.value.upload_id)
    currentUpload.value = null
    uploadProgress.value = 0
    ElMessage.success('上传已终止')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

function startPolling() {
  stopPolling()
  void refresh()
  pollTimer = window.setInterval(() => void refresh(), 3000)
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
}

async function refresh() {
  if (!session.value || refreshing.value) return
  refreshing.value = true
  try {
    const sessionId = session.value.session_id
    const [sessionResponse, bundleResponse] = await Promise.all([
      api.getSession(sessionId),
      api.listBundles(sessionId),
    ])
    session.value = sessionResponse.data
    bundles.value = bundleResponse.data

    if (bundles.value.some((item) => item.processing_status === 'ready')) {
      const [assessmentResponse, reportResponse] = await Promise.all([
        api.getAssessment(sessionId),
        api.listReports(sessionId),
      ])
      assessment.value = assessmentResponse.data
      reports.value = reportResponse.data
      if (reports.value.length) stopPolling()
    }
    if (processingFailed.value) stopPolling()
  } catch (error) {
    const status = (error as { response?: { status?: number } }).response?.status
    if (status && status !== 404) ElMessage.error(errorMessage(error))
  } finally {
    refreshing.value = false
  }
}

function identityHeaders() {
  const token = getIdentityToken()
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(tenantId ? { 'X-Tenant-ID': tenantId } : {}),
    ...(actorId ? { 'X-Actor-ID': actorId } : {}),
  }
}

function pretty(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / 1024 ** 2).toFixed(1)} MiB`
}

function missingEvidenceStatusLabel(status: string): string {
  return missingEvidenceStatusLabels[status] || status || '未知'
}

function missingEvidenceReason(item: MissingEvidence): string {
  const reasons = item.failure_reasons?.map(failureReasonLabel) || []
  const details = item.failure_details || []
  if (reasons.length || details.length) return [...reasons, ...details].join('\n')
  if (item.status === 'skipped_by_user') return '该采集项需要人工提供，本次采集未包含对应附件。'
  if (item.status === 'missing') return '证据包中未发现该采集项的输出。'
  if (item.reason === 'mandatory_collection_item_unavailable') return '必需采集项没有可用证据。'
  return item.reason || '采集端未提供更详细的失败原因。'
}

function failureReasonLabel(reason: string): string {
  if (reason === 'historical_assessment_link_mismatch') return '旧版完整性算法未正确关联已成功采集的证据。'
  if (reason === 'collector_product_version_unsupported') return '现场产品版本不支持该采集命令。'
  if (reason === 'collector_argument_unsupported') return '采集命令参数与现场 aCLI 不兼容。'
  const match = reason.match(/^collector_exit_(\d+)$/)
  if (!match) return reason
  const exitCode = Number(match[1])
  if (exitCode === 127) return '采集命令不存在（退出码 127）。'
  if (exitCode === 126) return '采集命令缺少执行条件或访问凭据（退出码 126）。'
  return `采集命令执行失败（退出码 ${exitCode}）。`
}

function formatArtifactExecution(item: CollectorArtifactItem | undefined): string {
  if (!item) return '该签名制品未包含此采集项。'
  const spec = item.execution_spec || {}
  if (spec.executor === 'command' && spec.argv?.length) {
    return `命令：${item.rendered_command}\nargv（参数数组）：${JSON.stringify(spec.argv)}`
  }
  if (spec.executor === 'http') {
    return `HCI API（HCI 接口）：${spec.method || 'GET'} ${spec.path || item.rendered_command}`
  }
  if (spec.executor === 'manual') return `Manual（人工采集）：${spec.guide || item.rendered_command}`
  return item.rendered_command || '历史制品未保存结构化执行规范。'
}

function planItemExecution(planItemId: string): string {
  return formatArtifactExecution(artifact.value?.items.find((item) => item.plan_item_id === planItemId))
}

function errorMessage(error: unknown): string {
  const candidate = error as { response?: { data?: { error?: { message?: string } } }; message?: string }
  return candidate.response?.data?.error?.message || candidate.message || '离线诊断操作失败'
}

onBeforeUnmount(stopPolling)
</script>

<template>
  <component
    :is="standalone ? 'main' : 'el-drawer'"
    v-model="visible"
    :class="standalone ? 'standalone-workspace' : undefined"
    :title="standalone ? undefined : '离线诊断'"
    :size="standalone ? undefined : '92%'"
  >
    <div v-if="standalone" class="standalone-header">
      <div><strong>HCI Offline Diagnosis（离线诊断）</strong><el-tag>工单 {{ caseId }}</el-tag></div>
      <a href="/">返回客户界面</a>
    </div>

    <el-alert
      v-if="!getIdentityToken() && !usesSameOriginIdentity"
      type="warning"
      :closable="false"
      title="当前开发环境尚未配置身份令牌，离线诊断接口将拒绝请求。"
    />

    <el-card class="section workflow-card" shadow="never">
      <el-steps :active="currentStep" finish-status="success" align-center>
        <el-step title="填写故障" />
        <el-step title="准备工具" />
        <el-step title="本地采集" />
        <el-step title="上传证据" />
        <el-step title="平台诊断" />
        <el-step title="工程师审核" />
        <el-step title="查看报告" />
      </el-steps>
    </el-card>

    <el-card v-if="!session" class="section">
      <template #header>1. 填写故障信息</template>
      <el-alert
        v-if="scenariosError"
        :title="`可用场景加载失败：${scenariosError}`"
        type="error"
        :closable="false"
        class="field-alert"
      />
      <el-alert
        v-else-if="!scenariosLoading && !scenarios.length"
        title="当前没有已批准、已启用的 Collection Profile（采集画像），请联系管理员先完成 KBD 同步并发布画像。"
        type="warning"
        :closable="false"
        class="field-alert"
      />
      <el-alert
        v-else
        title="可选故障场景与当前生效的 Collection Profile（采集画像）实时联动；画像停用后将不能新建该场景的诊断任务。"
        type="info"
        :closable="false"
        class="field-alert"
      />
      <el-form label-width="150px" class="context-form">
        <el-form-item label="故障场景" required>
          <el-select
            v-model="form.scenario"
            :loading="scenariosLoading"
            :disabled="scenariosLoading || !scenarios.length"
            placeholder="请选择已发布的采集画像场景"
          >
            <el-option
              v-for="item in scenarios"
              :key="item.scenario"
              :label="item.display_name"
              :value="item.scenario"
            />
          </el-select>
          <div v-if="selectedScenario" class="field-help">
            Profile Revision（画像修订）{{ selectedScenario.profile_revision }}；支持产品版本：
            {{ selectedScenario.supported_product_versions.join('、') }}
          </div>
        </el-form-item>
        <el-form-item label="产品版本" required>
          <el-input v-model="form.productVersion" placeholder="请填写现场实际版本，例如 6.12.0；不要按默认值猜测" />
          <div class="field-help">版本用于在生成采集工具前排除现场不支持的命令。</div>
        </el-form-item>
        <el-form-item label="故障对象标识" :required="selectedScenario?.requires_affected_object">
          <el-input v-model="form.objectId" placeholder="对象已存在时填写，例如虚拟机 ID、磁盘序列号" />
          <div class="field-help">
            {{ selectedScenario?.requires_affected_object ? '当前采集画像要求填写。' : '可选；创建失败等对象尚未生成的场景可以留空。' }}
          </div>
        </el-form-item>
        <el-form-item label="故障对象名称"><el-input v-model="form.objectName" /></el-form-item>
        <el-form-item label="执行节点" required>
          <el-input v-model="form.sourceNode" placeholder="运行采集工具的 HCI 节点名称或 IP" />
        </el-form-item>
        <el-form-item label="故障时间范围" required>
          <el-date-picker v-model="form.startTime" type="datetime" placeholder="开始时间" />
          <span class="time-separator">至</span>
          <el-date-picker v-model="form.endTime" type="datetime" placeholder="结束时间" />
        </el-form-item>
        <el-form-item label="当前状态">
          <el-select v-model="form.currentStatus">
            <el-option label="仍在发生" value="ongoing" />
            <el-option label="已经恢复" value="recovered" />
            <el-option label="间歇发生" value="intermittent" />
          </el-select>
        </el-form-item>
        <el-form-item label="近期变更">
          <el-input v-model="form.recentChangeDescription" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :disabled="!canCreate" :loading="busy" @click="createCollector">
            生成离线采集工具
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-else>
      <el-card class="section">
        <template #header><div class="header-row"><span>2. 下载并运行离线采集工具</span><el-tag>{{ session.status }}</el-tag></div></template>
        <div v-if="!plan" class="artifact-recovery">
          <el-alert
            title="诊断会话已创建，但采集计划尚未生成。请核对现场真实产品版本后重试。"
            type="warning"
            :closable="false"
          />
          <div class="actions">
            <el-input v-model="form.productVersion" placeholder="现场实际版本，例如 6.12.0" />
            <el-button type="primary" :disabled="!form.productVersion.trim()" :loading="busy" @click="retryCreatePlan">
              重新生成采集计划与工具
            </el-button>
          </div>
        </div>
        <el-result
          v-if="artifact"
          icon="success"
          title="离线采集工具已准备好"
          sub-title="请下载完整工具包，在执行节点解压后按照 README 操作；工具只运行平台审批的只读采集命令。"
        >
          <template #extra>
            <el-button
              type="primary"
              @click="download(artifact.verification_bundle_path, `hci-offline-collector-${artifact.artifact_id}.zip`)"
            >
              下载完整采集工具包
            </el-button>
          </template>
        </el-result>

        <div v-if="plan && !artifact" class="artifact-recovery">
          <el-alert title="采集范围已生成，但采集工具生成失败。请确认执行节点后重试。" type="warning" :closable="false" />
          <div class="actions">
            <el-select v-if="planTargetNodes.length" v-model="artifactTargetNode" placeholder="选择执行节点">
              <el-option v-for="node in planTargetNodes" :key="node" :label="node" :value="node" />
            </el-select>
            <el-input v-else v-model="artifactTargetNode" placeholder="输入执行节点" />
            <el-button type="primary" :disabled="!canGenerateArtifact" :loading="busy" @click="generateArtifact">重试生成</el-button>
          </div>
        </div>

        <el-collapse v-if="plan && artifact" class="technical-details">
          <el-collapse-item title="查看采集范围与安全校验信息">
            <el-descriptions :column="2" border>
              <el-descriptions-item label="预计大小">{{ plan.estimated_size_mb }} MiB</el-descriptions-item>
              <el-descriptions-item label="预计耗时">{{ Math.ceil(plan.estimated_duration_seconds / 60) }} 分钟</el-descriptions-item>
              <el-descriptions-item label="文件摘要" :span="2"><code>{{ artifact.artifact_sha256 }}</code></el-descriptions-item>
              <el-descriptions-item label="签名公钥指纹" :span="2"><code>{{ artifact.public_key_fingerprint }}</code></el-descriptions-item>
            </el-descriptions>
            <el-table :data="artifactPlanItems" border class="inner-table">
              <el-table-column prop="display_name" label="采集内容" min-width="200" />
              <el-table-column label="执行位置" min-width="180"><template #default="{ row }">{{ formatPlanTarget(row.target) }}</template></el-table-column>
              <el-table-column prop="reason" label="用途" min-width="280" />
              <el-table-column label="实际执行内容" min-width="430">
                <template #default="{ row }"><pre class="execution-spec">{{ planItemExecution(row.item_id) }}</pre></template>
              </el-table-column>
            </el-table>
          </el-collapse-item>
        </el-collapse>
      </el-card>

      <el-card class="section">
        <template #header>3. 上传采集结果</template>
        <p class="section-description">采集结束后会生成一个 <code>.hci-eb</code> 文件。平台会先完成安全检查，再进入诊断流程。</p>
        <el-upload :auto-upload="false" :limit="1" :on-change="onFileChange">
          <el-button>选择证据包</el-button>
        </el-upload>
        <el-progress v-if="uploadProgress" :percentage="uploadProgress" />
        <div class="actions">
          <el-button type="success" :disabled="!file || Boolean(currentUpload)" :loading="busy" @click="uploadEvidence">上传并开始诊断</el-button>
          <el-button v-if="currentUpload" type="primary" :loading="busy" @click="resumeUpload">继续上传</el-button>
          <el-button v-if="currentUpload" type="danger" plain @click="abortUpload">终止上传</el-button>
          <el-button :loading="refreshing" @click="refresh">刷新状态</el-button>
        </div>
        <el-alert
          v-if="latestBundle"
          class="inner-table"
          :type="processingFailed ? 'error' : latestBundle.processing_status === 'ready' ? 'success' : 'info'"
          :closable="false"
          :title="processingFailed ? `证据包未通过检查：${latestBundle.failure_message || '请联系工程师'}` : latestBundle.processing_status === 'ready' ? '证据包已通过安全检查' : '证据包正在安全检查和分析中'"
          :description="`文件大小：${formatBytes(latestBundle.size_bytes)}`"
        />
        <el-table v-if="assessment?.missing_evidence.length" :data="assessment ? assessment.missing_evidence : []" class="missing-evidence-table">
          <el-table-column label="采集项" min-width="210">
            <template #default="{ row }">
              <strong>{{ row.display_name || row.collector_id }}</strong>
              <code class="collector-id">{{ row.collector_id }}</code>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="170">
            <template #default="{ row }"><el-tag type="warning">{{ missingEvidenceStatusLabel(row.status) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="缺失原因" min-width="360">
            <template #default="{ row }"><div class="failure-reason">{{ missingEvidenceReason(row) }}</div></template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card v-if="assessment" class="section">
        <template #header>4. 证据完整性</template>
        <div class="assessment-summary">
          <el-progress type="dashboard" :percentage="assessment.completeness_score" />
          <div>
            <h3>{{ assessment.ready_for_diagnosis ? '证据可以支持诊断' : '证据不足，需要工程师确认' }}</h3>
            <p>必需证据：{{ assessment.mandatory.available }}/{{ assessment.mandatory.total }}</p>
            <p v-if="assessment.non_diagnosable_scope.length">暂不能判断：{{ assessment.non_diagnosable_scope.join('、') }}</p>
          </div>
        </div>
        <el-alert
          v-if="assessment.missing_evidence.length"
          type="warning"
          :closable="false"
          title="存在缺失证据。精简版暂不开放自动补采，工程师会根据当前证据给出结论或联系你补充采集。"
        />
      </el-card>

      <el-card v-if="activeReport" class="section">
        <template #header>
          <div class="header-row">
            <span>5. 诊断结果</span>
            <span><el-tag>{{ reportLevelLabels[activeReport.diagnosis_level] }}</el-tag><el-tag type="info">{{ activeReport.publish_status === 'customer_published' ? '已发布' : '工程师审核中' }}</el-tag></span>
          </div>
        </template>
        <el-alert
          v-if="activeReport.publish_status !== 'customer_published'"
          title="以下是平台生成的诊断草稿，工程师审核发布后才作为正式客户结论。"
          type="info"
          :closable="false"
        />
        <h3>{{ activeReport.summary }}</h3>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="诊断可信度">{{ (activeReport.confidence * 100).toFixed(1) }}%</el-descriptions-item>
          <el-descriptions-item label="主要判断">{{ activeReport.primary_hypothesis || '当前证据不足，尚不能确认根因' }}</el-descriptions-item>
        </el-descriptions>
        <div class="report-grid">
          <section><h4>支持证据</h4><pre>{{ pretty(activeReport.supporting_evidence) }}</pre></section>
          <section><h4>缺失证据</h4><pre>{{ pretty(activeReport.missing_evidence) }}</pre></section>
          <section><h4>恢复建议</h4><pre>{{ pretty(activeReport.recommended_recovery) }}</pre></section>
          <section><h4>风险与回退</h4><pre>{{ pretty(activeReport.risk_and_rollback) }}</pre></section>
        </div>
      </el-card>
    </template>
  </component>
</template>

<style scoped>
.standalone-workspace { min-height: 100vh; padding: 20px 28px 40px; background: var(--el-bg-color-page); }
.section { margin-top: 20px; }
.standalone-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 16px; border: 1px solid var(--el-border-color-light); border-radius: 8px; background: var(--el-bg-color); }
.standalone-header > div { display: flex; align-items: center; gap: 16px; }
.standalone-header a { color: var(--el-color-primary); text-decoration: none; }
.header-row, .actions, .assessment-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.header-row > span:last-child { display: flex; gap: 8px; }
.actions { justify-content: flex-start; margin-top: 16px; flex-wrap: wrap; }
.actions :deep(.el-select), .actions :deep(.el-input) { width: 360px; }
.context-form { max-width: 860px; }
.context-form :deep(.el-select), .context-form :deep(.el-date-editor) { width: 100%; }
.field-alert { margin-bottom: 18px; }
.field-help { margin-top: 6px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
.time-separator { margin: 0 10px; color: var(--el-text-color-secondary); }
.technical-details, .inner-table { margin-top: 16px; }
.artifact-recovery { margin-top: 16px; }
.section-description { color: var(--el-text-color-secondary); }
.assessment-summary { justify-content: flex-start; margin-bottom: 16px; }
.assessment-summary p { margin: 6px 0; color: var(--el-text-color-secondary); }
.missing-evidence-table { margin-top: 12px; }
.collector-id { display: block; margin-top: 4px; color: var(--el-text-color-secondary); font-size: 11px; }
.failure-reason { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; }
.report-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
.report-grid section { padding: 14px; border: 1px solid var(--el-border-color-light); border-radius: 6px; }
.report-grid h4 { margin: 0 0 10px; }
pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.execution-spec { line-height: 1.6; }
code { overflow-wrap: anywhere; }
@media (max-width: 900px) {
  .standalone-workspace { padding: 12px; }
  .workflow-card { overflow-x: auto; }
  .report-grid { grid-template-columns: 1fr; }
  .assessment-summary { align-items: flex-start; flex-direction: column; }
}
</style>
