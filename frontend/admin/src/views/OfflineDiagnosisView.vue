<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createApiClient,
  createOfflineDiagnosisApi,
  type CollectionProfileSnapshot,
  type CollectionPlan,
  type CollectorArtifact,
  type CollectorArtifactItem,
  type CollectorDefinition,
  type CollectorDefinitionWrite,
  type CollectorExecutor,
  type CollectorPlatform,
  type CollectorReviewStatus,
  type CollectorRevocationList,
  type CollectorTrustStore,
  type DiagnosisCandidate,
  type DiagnosisManagementRecord,
  type DiagnosisReport,
  type DiagnosisRun,
  type DiagnosisSession,
  type EvidenceAssessment,
  type EvidenceBundle,
  type MissingEvidence,
  type ManagedDiagnosisSession,
  type OfflineSignalMapping,
  type OfflineSignalMappingWrite,
  type OfflineSignalQueryType,
  type OfflineResourceSyncBatch,
  type SignalEvaluation,
} from '@hci/shared'

const client = createApiClient('/api')
// 正式环境由同源身份层注入短期访问令牌，构建产物不得携带内部服务令牌。
const developmentToken = import.meta.env.DEV
  ? import.meta.env.VITE_DIAGNOSIS_TOKEN || import.meta.env.VITE_INTERNAL_API_TOKEN || ''
  : ''
const usesSameOriginIdentity = import.meta.env.PROD
const getIdentityToken = () => developmentToken || window.__HCI_AUTH__?.getAccessToken?.()
const tenantId = import.meta.env.VITE_DIAGNOSIS_TENANT_ID || undefined
const actorId = import.meta.env.VITE_DIAGNOSIS_ACTOR_ID || 'admin-ui'
const api = createOfflineDiagnosisApi(client, { token: getIdentityToken, tenantId, actorId })

const activeTab = ref('workbench')
const sessionId = ref('')
const session = ref<DiagnosisSession | null>(null)
const bundles = ref<EvidenceBundle[]>([])
const assessment = ref<EvidenceAssessment | null>(null)
const runs = ref<DiagnosisRun[]>([])
const activeRunId = ref('')
const signals = ref<SignalEvaluation[]>([])
const candidates = ref<DiagnosisCandidate[]>([])
const reports = ref<DiagnosisReport[]>([])
const managedSessions = ref<ManagedDiagnosisSession[]>([])
const managedSessionTotal = ref(0)
const managedQuery = ref('')
const managedStatus = ref('')
const reportReviews = ref<DiagnosisManagementRecord[]>([])
const securityEvents = ref<DiagnosisManagementRecord[]>([])
const trustStore = ref<CollectorTrustStore | null>(null)
const revocations = ref<CollectorRevocationList | null>(null)
const collectionProfiles = ref<CollectionProfileSnapshot[]>([])
const collectionPlans = ref<CollectionPlan[]>([])
const collectorArtifacts = ref<CollectorArtifact[]>([])
const artifactDetail = ref<CollectorArtifact | null>(null)
const artifactDetailVisible = ref(false)
const profileLoading = ref(false)
const planLoading = ref(false)
const artifactLoading = ref(false)
const profileDialogVisible = ref(false)
const profileSaving = ref(false)
const profileReadOnly = ref(false)
const profileForm = reactive({
  originalId: '',
  profile_id: '',
  version: '1.0.0',
  definition: '{}',
  lock_version: undefined as number | undefined,
})
const managementLoading = ref(false)
const loading = ref(false)
const reviewDialogVisible = ref(false)
const reviewSaving = ref(false)
const reviewReportTarget = ref<DiagnosisReport | null>(null)
const reviewForm = reactive({
  action: 'submit_review' as 'submit_review' | 'confirm' | 'publish' | 'reject' | 'return_to_draft',
  reason: '',
  summary: '',
  recommended_recovery: '[]',
})

const collectors = ref<CollectorDefinition[]>([])
const collectorLoading = ref(false)
const collectorStatus = ref<CollectorReviewStatus | ''>('')
const collectorEnabled = ref<'' | 'true' | 'false'>('')
const collectorDialogVisible = ref(false)
const collectorSaving = ref(false)
const collectorReadOnly = ref(false)
const collectorForm = reactive({
  originalId: '',
  collector_id: '',
  display_name: '',
  description: '',
  platform: 'linux' as CollectorPlatform,
  executor: 'shell' as CollectorExecutor,
  command_template: '',
  parameter_schema: '{\n  "type": "object",\n  "properties": {},\n  "additionalProperties": false\n}',
  timeout_seconds: 30,
  max_output_mb: 10,
  supported_product_versions: '*',
  schema_id: '',
  media_type: 'text/plain',
  output_path: 'commands/output.txt',
  version: '1.0.0',
  lock_version: undefined as number | undefined,
})

const signalMappings = ref<OfflineSignalMapping[]>([])
const mappingLoading = ref(false)
const mappingDialogVisible = ref(false)
const mappingSaving = ref(false)
const mappingForm = reactive({
  mapping_id: '',
  source_kbd_id: undefined as number | undefined,
  source_kbd_revision: undefined as number | undefined,
  source_signal_id: '',
  execution_contract_checksum: '',
  acquire_tool: '',
  category_scope: '*',
  command_scope: '*',
  collector_id: '',
  query_type: 'command_output' as OfflineSignalQueryType,
  field_mapping: '{}',
  priority: 100,
  is_enabled: true,
  lock_version: undefined as number | undefined,
})

const syncBatches = ref<OfflineResourceSyncBatch[]>([])
const syncTotal = ref(0)
const syncLoading = ref(false)
const syncDetail = ref<OfflineResourceSyncBatch | null>(null)
const syncDialogVisible = ref(false)

const missingEvidenceStatusLabels: Record<string, string> = {
  missing: '未采集',
  collection_failed: '采集失败',
  out_of_time_range: '不在故障时间窗',
  not_applicable: '当前环境不适用',
  unreadable: '证据不可读取',
  skipped_by_user: '用户未提供',
  assessment_link_mismatch: '历史评估关联异常',
}

function scenarioLabel(scenario: string): string {
  const snapshot = collectionProfiles.value.find(
    item => item.profile.profile_id === scenario || item.profile.scenario === scenario,
  )
  return snapshot?.profile.display_name || scenario || '—'
}

function missingEvidenceStatusLabel(status: string): string {
  return missingEvidenceStatusLabels[status] || status || '未知'
}

function syncSummaryNumber(key: string): number {
  const value = syncDetail.value?.summary_json?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function syncSummaryList(key: string): string[] {
  const value = syncDetail.value?.summary_json?.[key]
  return Array.isArray(value) ? value.map(item => String(item)) : []
}

function isLegacySyncBatch(): boolean {
  return syncDetail.value?.summary_json?.scenario_source !== 'kbd.category_id'
}

function syncResultTitle(): string {
  if (isLegacySyncBatch()) {
    return '该历史批次使用旧离线准入规则；请升级服务后重新执行全量检测'
  }
  if (syncSummaryNumber('unresolved_category_kbd_count') > 0) {
    return '存在已发布但缺少最终分类的 KBD；修复分类后重新检测'
  }
  if (syncSummaryNumber('candidate_change_count') === 0) {
    return '检测完成，当前生效离线资源无需变更'
  }
  return '已从发布 KBD 的共用问题场景生成离线资源候选；审核发布后生效'
}

function syncResultType(): 'success' | 'warning' | 'info' {
  if (isLegacySyncBatch()) return 'warning'
  if (syncSummaryNumber('unresolved_category_kbd_count') > 0) return 'warning'
  return syncSummaryNumber('candidate_change_count') > 0 ? 'success' : 'info'
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
  const match = reason.match(/^collector_exit_(\d+)$/)
  if (!match) return reason
  const exitCode = Number(match[1])
  if (exitCode === 127) return '采集命令不存在（退出码 127）。'
  if (exitCode === 126) return '采集命令缺少执行条件或访问凭据（退出码 126）。'
  return `采集命令执行失败（退出码 ${exitCode}）。`
}

async function loadWorkbench() {
  managementLoading.value = true
  try {
    const response = await api.listManagedSessions({
      ...(managedQuery.value.trim() ? { query: managedQuery.value.trim() } : {}),
      ...(managedStatus.value ? { status: managedStatus.value } : {}),
      limit: 100,
    })
    managedSessions.value = response.data.items
    managedSessionTotal.value = response.data.total
    // 场景名称来自 Collection Profile（采集画像），不在前端维护业务枚举。
    try {
      collectionProfiles.value = (await api.listCollectionProfiles()).data
    } catch {
      // 画像名称加载失败不应阻断任务列表，页面回退展示稳定场景编码。
    }
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    managementLoading.value = false
  }
}

async function openManagedSession(item: ManagedDiagnosisSession | DiagnosisManagementRecord) {
  if (!item.session_id) return
  sessionId.value = item.session_id
  activeTab.value = 'detail'
  await search()
}

async function assignManagedSession(item: ManagedDiagnosisSession) {
  try {
    const { value } = await ElMessageBox.prompt('请输入工程师账号', '转派诊断任务', {
      inputPattern: /^[A-Za-z0-9._:@-]+$/,
      inputErrorMessage: '账号格式不合法',
    })
    await api.assignSession(item.session_id, value)
    await loadWorkbench()
    ElMessage.success('诊断任务已转派')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function retryManagedSession(item: ManagedDiagnosisSession) {
  try {
    await ElMessageBox.confirm('确认重试最近一次失败的后台处理任务？', '重试后台任务')
    await api.retrySessionProcessing(item.session_id)
    await loadWorkbench()
    ElMessage.success('后台任务已重新进入队列')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function terminateManagedSession(item: ManagedDiagnosisSession) {
  try {
    const { value } = await ElMessageBox.prompt('请输入终止原因', '终止诊断任务', {
      inputPattern: /\S{2,}/,
      inputErrorMessage: '终止原因至少 2 个字符',
      type: 'warning',
    })
    await api.terminateSession(item.session_id, value)
    await loadWorkbench()
    ElMessage.success('诊断任务已终止')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function search() {
  if (!sessionId.value.trim()) return
  loading.value = true
  try {
    const id = sessionId.value.trim()
    const [sessionResponse, bundleResponse] = await Promise.all([api.getSession(id), api.listBundles(id)])
    session.value = sessionResponse.data
    bundles.value = bundleResponse.data
    assessment.value = null
    runs.value = []
    signals.value = []
    candidates.value = []
    reports.value = []
    if (bundles.value.some((item) => item.processing_status === 'ready')) {
      const [assessmentResponse, runResponse, reportResponse] = await Promise.all([
        api.getAssessment(id),
        api.listRuns(id),
        api.listReports(id),
      ])
      assessment.value = assessmentResponse.data
      runs.value = runResponse.data
      reports.value = reportResponse.data
      const latestRun = runs.value.at(-1)
      if (latestRun) await loadRunDetails(latestRun.run_id)
    }
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

async function loadRunDetails(runId: string) {
  activeRunId.value = runId
  try {
    const [signalResponse, candidateResponse] = await Promise.all([
      api.listSignals(sessionId.value.trim(), runId),
      api.listCandidates(sessionId.value.trim(), runId),
    ])
    signals.value = signalResponse.data
    candidates.value = candidateResponse.data
  } catch (error) {
    signals.value = []
    candidates.value = []
    if ((error as { response?: { status?: number } }).response?.status !== 404) {
      ElMessage.error(errorMessage(error))
    }
  }
}

async function loadReportReviews() {
  managementLoading.value = true
  try {
    reportReviews.value = (await api.listReportReviews()).data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    managementLoading.value = false
  }
}

async function loadSecurityEvents() {
  managementLoading.value = true
  try {
    securityEvents.value = (await api.listSecurityEvents()).data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    managementLoading.value = false
  }
}

async function reviewSecurityEvent(item: DiagnosisManagementRecord, action: 'acknowledge' | 'clear') {
  try {
    const { value } = await ElMessageBox.prompt(
      action === 'acknowledge' ? '请输入确认备注' : '请输入清除结论',
      action === 'acknowledge' ? '确认安全事件' : '清除安全事件',
      { inputPattern: /\S{2,}/, inputErrorMessage: '备注至少 2 个字符' },
    )
    await api.reviewSecurityEvent(item.resource_id, action, value)
    await loadSecurityEvents()
    ElMessage.success('安全事件处置状态已更新；被拒绝的证据包仍保持隔离')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function loadCollectors() {
  collectorLoading.value = true
  try {
    const [collectorResponse, profileResponse] = await Promise.all([
      api.listCollectors({
        ...(collectorStatus.value ? { review_status: collectorStatus.value } : {}),
        ...(collectorEnabled.value ? { is_enabled: collectorEnabled.value === 'true' } : {}),
      }),
      api.listCollectionProfiles(),
    ])
    collectors.value = collectorResponse.data
    collectionProfiles.value = profileResponse.data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    collectorLoading.value = false
  }
}

async function loadProfiles() {
  profileLoading.value = true
  try {
    collectionProfiles.value = (await api.listCollectionProfiles()).data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    profileLoading.value = false
  }
}

function openNewProfile() {
  profileReadOnly.value = false
  Object.assign(profileForm, {
    originalId: '',
    profile_id: '',
    version: '1.0.0',
    definition: JSON.stringify(
      {
        profile_id: '',
        display_name: '',
        product_line: 'HCI',
        scenario: '',
        supported_product_versions: ['6.*', '7.*', '8.*'],
        items: [],
      },
      null,
      2,
    ),
    lock_version: undefined,
  })
  profileDialogVisible.value = true
}

function openProfile(item: CollectionProfileSnapshot) {
  profileReadOnly.value = item.managed_by === 'kbd_sync'
  Object.assign(profileForm, {
    originalId: item.profile.profile_id,
    profile_id: item.profile.profile_id,
    version: item.version,
    definition: JSON.stringify(item.profile, null, 2),
    lock_version: item.lock_version,
  })
  profileDialogVisible.value = true
}

async function saveProfile() {
  profileSaving.value = true
  try {
    const definition = parseJsonObject(profileForm.definition, 'Collection Profile（采集画像）定义') as unknown as CollectionProfileSnapshot['profile']
    if (!profileForm.profile_id.trim()) throw new Error('Profile ID（画像标识）不能为空')
    if (profileForm.originalId && profileForm.originalId !== profileForm.profile_id.trim()) {
      throw new Error('已有 Collection Profile（采集画像）的标识不可修改')
    }
    if (definition.profile_id !== profileForm.profile_id.trim() || definition.scenario !== profileForm.profile_id.trim()) {
      throw new Error('profile_id、scenario 和路径标识必须一致')
    }
    await api.saveCollectionProfile(definition, profileForm.version.trim(), profileForm.lock_version)
    profileDialogVisible.value = false
    await loadProfiles()
    ElMessage.success('Collection Profile（采集画像）草稿已保存')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    profileSaving.value = false
  }
}

async function reviewProfile(item: CollectionProfileSnapshot, approved: boolean) {
  try {
    let reason: string | undefined
    if (approved) {
      await ElMessageBox.confirm('批准后将发布新的不可变画像修订，确认继续？', '批准采集画像')
    } else {
      const result = await ElMessageBox.prompt('请输入驳回原因', '驳回采集画像', {
        inputPattern: /\S{2,}/,
        inputErrorMessage: '驳回原因至少 2 个字符',
      })
      reason = result.value
    }
    await api.reviewCollectionProfile(item, approved, reason)
    await loadProfiles()
    ElMessage.success(approved ? '采集画像已批准并发布' : '采集画像已驳回')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function disableProfile(item: CollectionProfileSnapshot) {
  try {
    await ElMessageBox.confirm('禁用后新计划不再使用该画像，历史快照仍保留。确认继续？', '禁用采集画像', {
      type: 'warning',
    })
    await api.disableCollectionProfile(item)
    await loadProfiles()
    ElMessage.success('采集画像已禁用')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function loadCollectionPlans() {
  planLoading.value = true
  try {
    collectionPlans.value = (await api.listCollectionPlans()).data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    planLoading.value = false
  }
}

async function regeneratePlan(item: CollectionPlan) {
  try {
    const { value } = await ElMessageBox.prompt(
      '请输入重生成原因。系统将采用最新画像和 KBD 规则集，旧计划作废且旧制品自动撤销。',
      '重生成采集计划',
      { inputPattern: /\S{2,}/, inputErrorMessage: '原因至少 2 个字符', type: 'warning' },
    )
    await api.regenerateCollectionPlan(item.collection_plan_id, value)
    await Promise.all([loadCollectionPlans(), loadCollectorArtifacts()])
    ElMessage.success('新计划修订已生成，旧计划和旧制品已安全失效')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function loadCollectorArtifacts() {
  artifactLoading.value = true
  try {
    collectorArtifacts.value = (await api.listCollectorArtifacts()).data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    artifactLoading.value = false
  }
}

function openArtifactDetail(item: CollectorArtifact) {
  artifactDetail.value = item
  artifactDetailVisible.value = true
}

function formatArtifactExecution(item: CollectorArtifactItem): string {
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

async function revokeArtifact(item: CollectorArtifact) {
  try {
    await ElMessageBox.confirm('撤销后客户将不能继续下载或使用该制品，确认继续？', '撤销采集器制品', {
      type: 'warning',
    })
    await api.revokeManagedArtifact(item.artifact_id, 'admin_revoked')
    await loadCollectorArtifacts()
    ElMessage.success('Collector Artifact（采集器制品）已撤销')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function loadSignalMappings() {
  mappingLoading.value = true
  try {
    const [mappingResponse, collectorResponse] = await Promise.all([
      api.listSignalMappings(),
      api.listCollectors({ review_status: 'approved', is_enabled: true }),
    ])
    signalMappings.value = mappingResponse.data
    collectors.value = collectorResponse.data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    mappingLoading.value = false
  }
}

async function loadSyncHistory() {
  syncLoading.value = true
  try {
    const response = await api.listOfflineResourceSyncHistory(0, 100)
    syncBatches.value = response.data.items
    syncTotal.value = response.data.total
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    syncLoading.value = false
  }
}

async function previewSync(mode: 'incremental' | 'full') {
  try {
    if (mode === 'full') {
      await ElMessageBox.confirm(
        '全量检测会按 KBD 最终分类重新核对全部已发布 KBD 和 Tool Registry；在线、离线诊断共用问题场景，已有候选批次将自动失效。确认继续？',
        '全量检测 KBD 与 Tool Registry',
        { type: 'warning' },
      )
    }
    syncLoading.value = true
    syncDetail.value = (await api.previewOfflineResourceSync(mode)).data
    syncDialogVisible.value = true
    await loadSyncHistory()
    ElMessage.success('KBD 与 Tool Registry 差异检测完成；候选版本尚未影响当前生效资源')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  } finally {
    syncLoading.value = false
  }
}

async function openSyncBatch(item: OfflineResourceSyncBatch) {
  try {
    syncDetail.value = (await api.getOfflineResourceSyncBatch(item.batch_id)).data
    syncDialogVisible.value = true
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

async function publishSyncBatch(item: OfflineResourceSyncBatch) {
  try {
    const { value } = await ElMessageBox.prompt(
      '发布会原子切换本批次全部 Collector（采集器）、映射和 Collection Profile（采集画像），请输入审批原因。',
      '确认发布同步批次',
      { inputPattern: /\S{2,}/, inputErrorMessage: '审批原因至少 2 个字符', type: 'warning' },
    )
    syncDetail.value = (await api.publishOfflineResourceSync(item.batch_id, value)).data
    await Promise.all([loadSyncHistory(), loadCollectors(), loadProfiles(), loadSignalMappings()])
    ElMessage.success(syncDetail.value.status === 'published' ? '同步批次已原子发布' : '发布失败，失败结果已写入历史')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function rejectSyncBatch(item: OfflineResourceSyncBatch) {
  try {
    const { value } = await ElMessageBox.prompt('请输入拒绝原因；候选差异仍会永久保留。', '拒绝同步批次', {
      inputPattern: /\S{2,}/,
      inputErrorMessage: '拒绝原因至少 2 个字符',
    })
    syncDetail.value = (await api.rejectOfflineResourceSync(item.batch_id, value)).data
    await loadSyncHistory()
    ElMessage.success('同步候选已拒绝，动作和结果已留痕')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function rollbackSyncBatch(item: OfflineResourceSyncBatch) {
  try {
    const { value } = await ElMessageBox.prompt(
      '仅最后一次发布批次可回滚。系统会整批恢复同步前的生效版本和 KBD 游标，请输入回滚原因。',
      '回滚同步批次',
      { inputPattern: /\S{2,}/, inputErrorMessage: '回滚原因至少 2 个字符', type: 'warning' },
    )
    syncDetail.value = (await api.rollbackOfflineResourceSync(item.batch_id, value)).data
    await Promise.all([loadSyncHistory(), loadCollectors(), loadProfiles(), loadSignalMappings()])
    ElMessage.success(syncDetail.value.status === 'rolled_back' ? '同步批次已整批回滚' : '回滚失败，结果已写入历史')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function loadCollectorTrust() {
  managementLoading.value = true
  try {
    const [trustResponse, revocationResponse] = await Promise.all([
      api.getCollectorTrustStore(),
      api.getCollectorRevocations(),
    ])
    trustStore.value = trustResponse.data
    revocations.value = revocationResponse.data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    managementLoading.value = false
  }
}

function openNewCollector() {
  collectorReadOnly.value = false
  Object.assign(collectorForm, {
    originalId: '',
    collector_id: '',
    display_name: '',
    description: '',
    platform: 'linux',
    executor: 'shell',
    command_template: '',
    parameter_schema: '{\n  "type": "object",\n  "properties": {},\n  "additionalProperties": false\n}',
    timeout_seconds: 30,
    max_output_mb: 10,
    supported_product_versions: '*',
    schema_id: '',
    media_type: 'text/plain',
    output_path: 'commands/output.txt',
    version: '1.0.0',
    lock_version: undefined,
  })
  collectorDialogVisible.value = true
}

function openCollector(item: CollectorDefinition) {
  collectorReadOnly.value = item.managed_by === 'kbd_sync'
  Object.assign(collectorForm, {
    originalId: item.collector_id,
    collector_id: item.collector_id,
    display_name: item.display_name,
    description: item.description,
    platform: item.platform,
    executor: item.executor,
    command_template: item.command_template,
    parameter_schema: JSON.stringify(item.parameter_schema, null, 2),
    timeout_seconds: item.timeout_seconds,
    max_output_mb: item.max_output_mb,
    supported_product_versions: item.supported_product_versions.join(', '),
    schema_id: item.output_contract.schema_id,
    media_type: item.output_contract.media_type,
    output_path: item.output_contract.output_path,
    version: item.version,
    lock_version: item.lock_version,
  })
  collectorDialogVisible.value = true
}

function changeCollectorPlatform(platform: CollectorPlatform) {
  const values: Record<CollectorPlatform, { executor: CollectorExecutor; mediaType: string; path: string }> = {
    linux: { executor: 'shell', mediaType: 'text/plain', path: 'commands/output.txt' },
    hci_api: { executor: 'http', mediaType: 'application/json', path: 'exports/api-output.json' },
    manual: { executor: 'manual', mediaType: 'application/octet-stream', path: 'attachments/manual-output.bin' },
  }
  collectorForm.executor = values[platform].executor
  collectorForm.media_type = values[platform].mediaType
  collectorForm.output_path = values[platform].path
}

async function saveCollector() {
  collectorSaving.value = true
  try {
    if (collectorForm.originalId && collectorForm.originalId !== collectorForm.collector_id.trim()) {
      throw new Error('已有 Collector（采集器）的标识不可修改')
    }
    const payload: CollectorDefinitionWrite = {
      collector_id: collectorForm.collector_id.trim(),
      display_name: collectorForm.display_name.trim(),
      description: collectorForm.description.trim(),
      platform: collectorForm.platform,
      executor: collectorForm.executor,
      command_template: collectorForm.command_template.trim(),
      parameter_schema: parseJsonObject(collectorForm.parameter_schema, 'Parameter Schema（参数模式）'),
      risk_level: 'read_only',
      timeout_seconds: collectorForm.timeout_seconds,
      max_output_mb: collectorForm.max_output_mb,
      supported_product_versions: collectorForm.supported_product_versions
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean),
      output_contract: {
        schema_id: collectorForm.schema_id.trim(),
        media_type: collectorForm.media_type.trim(),
        output_path: collectorForm.output_path.trim(),
      },
      version: collectorForm.version.trim(),
    }
    await api.saveCollector(payload, collectorForm.lock_version)
    collectorDialogVisible.value = false
    await loadCollectors()
    ElMessage.success('Collector（采集器）草稿已保存，需审批后才会用于新制品')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    collectorSaving.value = false
  }
}

async function approveCollector(item: CollectorDefinition) {
  try {
    await ElMessageBox.confirm(`确认批准并发布 ${item.collector_id} 的不可变运行时修订版本？`, '批准 Collector（采集器）')
    await api.reviewCollector(item, true)
    await loadCollectors()
    ElMessage.success('Collector（采集器）已批准并发布')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function rejectCollector(item: CollectorDefinition) {
  try {
    const { value } = await ElMessageBox.prompt('请输入驳回原因', '驳回 Collector（采集器）', {
      inputPattern: /\S{2,}/,
      inputErrorMessage: '驳回原因至少 2 个字符',
    })
    await api.reviewCollector(item, false, value)
    await loadCollectors()
    ElMessage.success('Collector（采集器）已驳回')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

async function disableCollector(item: CollectorDefinition) {
  try {
    await ElMessageBox.confirm(
      `禁用 ${item.collector_id} 后，引用它的生效计划会作废，仍可下载的历史制品会被撤销，但审计记录不会删除。是否继续？`,
      '禁用 Collector（采集器）',
      { type: 'warning' },
    )
    await api.disableCollector(item)
    await loadCollectors()
    ElMessage.success('Collector（采集器）已禁用，关联计划和制品已安全失效')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error))
  }
}

function openNewMapping() {
  Object.assign(mappingForm, {
    mapping_id: crypto.randomUUID(),
    source_kbd_id: undefined,
    source_kbd_revision: undefined,
    source_signal_id: '',
    execution_contract_checksum: '',
    acquire_tool: '',
    category_scope: '*',
    command_scope: '*',
    collector_id: '',
    query_type: 'command_output',
    field_mapping: '{}',
    priority: 100,
    is_enabled: true,
    lock_version: undefined,
  })
  mappingDialogVisible.value = true
}

function openMapping(item: OfflineSignalMapping) {
  Object.assign(mappingForm, {
    mapping_id: item.mapping_id,
    source_kbd_id: item.source_kbd_id,
    source_kbd_revision: item.source_kbd_revision,
    source_signal_id: item.source_signal_id,
    execution_contract_checksum: item.execution_contract_checksum,
    acquire_tool: item.acquire_tool,
    category_scope: item.category_scope,
    command_scope: item.command_scope,
    collector_id: item.collector_id,
    query_type: item.query_type,
    field_mapping: JSON.stringify(item.field_mapping, null, 2),
    priority: item.priority,
    is_enabled: item.is_enabled,
    lock_version: item.lock_version,
  })
  mappingDialogVisible.value = true
}

async function saveMapping() {
  mappingSaving.value = true
  try {
    const fields = parseJsonObject(mappingForm.field_mapping, 'Field Mapping（字段映射）')
    if (Object.values(fields).some((value) => typeof value !== 'string')) {
      throw new Error('Field Mapping（字段映射）的所有值必须是字符串')
    }
    const payload: OfflineSignalMappingWrite = {
      source_kbd_id: Number(mappingForm.source_kbd_id),
      source_kbd_revision: Number(mappingForm.source_kbd_revision),
      source_signal_id: mappingForm.source_signal_id.trim(),
      execution_contract_checksum: mappingForm.execution_contract_checksum.trim(),
      acquire_tool: mappingForm.acquire_tool.trim(),
      category_scope: mappingForm.category_scope.trim(),
      command_scope: mappingForm.command_scope.trim(),
      collector_id: mappingForm.collector_id.trim(),
      query_type: mappingForm.query_type,
      field_mapping: fields as Record<string, string>,
      priority: mappingForm.priority,
      is_enabled: mappingForm.is_enabled,
    }
    await api.saveSignalMapping(mappingForm.mapping_id, payload, mappingForm.lock_version)
    mappingDialogVisible.value = false
    await loadSignalMappings()
    ElMessage.success('Offline Signal Mapping（离线信号映射）已保存')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    mappingSaving.value = false
  }
}

function onTabChange(name: string | number) {
  if (name === 'workbench') void loadWorkbench()
  if (name === 'reviews') void loadReportReviews()
  if (name === 'security') void loadSecurityEvents()
  if (name === 'collectors') void loadCollectors()
  if (name === 'profiles') void loadProfiles()
  if (name === 'plans') void loadCollectionPlans()
  if (name === 'artifacts') void loadCollectorArtifacts()
  if (name === 'mappings') void loadSignalMappings()
  if (name === 'sync') void loadSyncHistory()
  if (name === 'collector-trust') void loadCollectorTrust()
}

function openReview(
  report: DiagnosisReport,
  action: 'submit_review' | 'confirm' | 'publish' | 'reject' | 'return_to_draft',
) {
  reviewReportTarget.value = report
  Object.assign(reviewForm, {
    action,
    reason: '',
    summary: report.summary,
    recommended_recovery: JSON.stringify(report.recommended_recovery, null, 2),
  })
  reviewDialogVisible.value = true
}

async function submitReview() {
  if (!reviewReportTarget.value) return
  reviewSaving.value = true
  try {
    if (reviewForm.reason.trim().length < 2) throw new Error('审核原因至少 2 个字符')
    const recovery = JSON.parse(reviewForm.recommended_recovery)
    if (!Array.isArray(recovery)) throw new Error('恢复建议必须是 JSON（结构化数据）数组')
    const report = reviewReportTarget.value
    const response = await api.reviewReport(
      sessionId.value.trim(),
      report,
      reviewForm.action,
      reviewForm.reason.trim(),
      { summary: reviewForm.summary.trim(), recommended_recovery: recovery },
    )
    const index = reports.value.findIndex((item) => item.report_id === report.report_id)
    if (index >= 0) reports.value.splice(index, 1, response.data)
    reviewDialogVisible.value = false
    ElMessage.success('报告状态已更新并写入审计记录')
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    reviewSaving.value = false
  }
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(value)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error()
    return parsed
  } catch {
    throw new Error(`${label} 必须是合法 JSON 对象`)
  }
}

function pretty(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

function errorMessage(error: unknown): string {
  const candidate = error as { response?: { data?: { error?: { message?: string } } }; message?: string }
  return candidate.response?.data?.error?.message || candidate.message || '操作失败'
}

onMounted(loadWorkbench)
</script>

<template>
  <div>
    <el-alert
      v-if="usesSameOriginIdentity"
      title="正式环境使用 OIDC（开放身份连接）短期身份；本地容器由同源反向代理注入开发身份。浏览器资源不包含内部服务令牌。"
      type="info"
      :closable="false"
      show-icon
      class="identity-alert"
    />
    <el-alert
      title="管理端保留诊断任务、报告审核、安全隔离和采集器核心运营。自动补采、法务保全、留存中心和质量运营仍在后续阶段开放。"
      type="info"
      :closable="false"
      class="identity-alert"
    />

    <el-tabs v-model="activeTab" type="border-card" @tab-change="onTabChange">
      <el-tab-pane label="诊断任务" name="workbench">
        <div class="toolbar">
          <el-input
            v-model="managedQuery"
            clearable
            placeholder="会话标识、工单号或客户标识"
            style="width: 360px"
            @keyup.enter="loadWorkbench"
          />
          <el-select v-model="managedStatus" clearable placeholder="任务状态" style="width: 180px">
            <el-option
              v-for="item in ['created', 'plan_ready', 'collecting', 'uploading', 'assessing', 'review_pending', 'published', 'failed', 'cancelled']"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
          <el-button :loading="managementLoading" @click="loadWorkbench">查询</el-button>
          <span>共 {{ managedSessionTotal }} 条</span>
        </div>
        <el-table v-loading="managementLoading" :data="managedSessions" border>
          <el-table-column prop="case_id" label="工单号" width="165" />
          <el-table-column label="故障场景" min-width="190">
            <template #default="{ row }">{{ scenarioLabel(row.selected_scenario) }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="150" />
          <el-table-column prop="assigned_to" label="负责人" width="150" />
          <el-table-column label="报告" width="150">
            <template #default="{ row }">
              {{ row.latest_report_sequence ? `V${row.latest_report_sequence}` : '—' }} {{ row.latest_report_status || '' }}
            </template>
          </el-table-column>
          <el-table-column prop="bundle_count" label="证据包" width="85" />
          <el-table-column prop="failed_task_count" label="失败任务" width="90" />
          <el-table-column prop="updated_at" label="更新时间" min-width="180" />
          <el-table-column label="操作" fixed="right" width="300">
            <template #default="{ row }">
              <el-button link type="primary" @click="openManagedSession(row)">详情</el-button>
              <el-button link @click="assignManagedSession(row)">转派</el-button>
              <el-button v-if="row.failed_task_count" link type="warning" @click="retryManagedSession(row)">重试任务</el-button>
              <el-button
                v-if="!['closed', 'cancelled', 'deleted'].includes(row.status)"
                link
                type="danger"
                @click="terminateManagedSession(row)"
              >终止</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="会话详情" name="detail">
        <el-input v-model="sessionId" clearable placeholder="输入诊断会话 UUID" @keyup.enter="search">
          <template #append><el-button :loading="loading" @click="search">查询</el-button></template>
        </el-input>

        <el-descriptions v-if="session" class="section" :column="3" border>
          <el-descriptions-item label="工单号">{{ session.case_id }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ session.status }}</el-descriptions-item>
          <el-descriptions-item label="场景">{{ scenarioLabel(session.selected_scenario) }}</el-descriptions-item>
          <el-descriptions-item label="当前故障状态">{{ session.current_status }}</el-descriptions-item>
          <el-descriptions-item label="负责人">{{ session.assigned_to || '未分配' }}</el-descriptions-item>
          <el-descriptions-item label="解析分类">{{ session.resolved_category || '待解析' }}</el-descriptions-item>
          <el-descriptions-item label="Trace ID（链路标识）" :span="3">{{ session.trace_id }}</el-descriptions-item>
        </el-descriptions>

        <el-card v-if="bundles.length" class="section">
          <template #header>证据包与安全结果</template>
          <el-table :data="bundles">
            <el-table-column prop="bundle_id" label="证据包标识" min-width="280" />
            <el-table-column prop="processing_status" label="处理状态" width="140" />
            <el-table-column prop="size_bytes" label="大小（字节）" width="140" />
            <el-table-column label="安全结果" min-width="300"><template #default="{ row }"><pre>{{ pretty(row.security_results) }}</pre></template></el-table-column>
            <el-table-column prop="failure_message" label="失败原因" min-width="220" />
          </el-table>
        </el-card>

        <el-card v-if="assessment" class="section">
          <template #header>证据完整性</template>
          <el-progress :percentage="assessment.completeness_score" />
          <p>必需证据：{{ assessment.mandatory.available }}/{{ assessment.mandatory.total }}</p>
          <p>可诊断范围：{{ assessment.diagnosable_scope.join('、') || '无' }}</p>
          <p>不可诊断范围：{{ assessment.non_diagnosable_scope.join('、') || '无' }}</p>
          <el-table v-if="assessment.missing_evidence.length" :data="assessment.missing_evidence">
            <el-table-column label="采集项" min-width="220">
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
            <el-table-column prop="impact" label="影响" />
          </el-table>
        </el-card>

        <el-card v-if="runs.length" class="section">
          <template #header>诊断依据（仅工程师可见）</template>
          <el-tabs v-model="activeRunId" @tab-change="(name) => loadRunDetails(String(name))">
            <el-tab-pane v-for="item in runs" :key="item.run_id" :name="item.run_id" :label="`分析 #${item.run_sequence}`">
              <el-descriptions :column="3" border>
                <el-descriptions-item label="状态">{{ item.status }}</el-descriptions-item>
                <el-descriptions-item label="解析分类">{{ item.resolved_category || '未解析' }}</el-descriptions-item>
                <el-descriptions-item label="结论策略">{{ item.conclusion_policy_version }}</el-descriptions-item>
              </el-descriptions>
            </el-tab-pane>
          </el-tabs>
          <el-collapse>
            <el-collapse-item title="查看信号判断和知识库候选">
              <el-table :data="signals" border>
                <el-table-column prop="signal_id" label="信号标识" min-width="230" />
                <el-table-column prop="state" label="结果" width="140" />
                <el-table-column prop="evidence_status" label="证据状态" width="150" />
                <el-table-column prop="reason" label="判定原因" min-width="300" />
              </el-table>
              <el-table :data="candidates" border class="section">
                <el-table-column prop="support_id" label="知识库标识" width="165" />
                <el-table-column prop="title" label="候选标题" min-width="260" />
                <el-table-column label="评分" width="100"><template #default="{ row }">{{ (row.score * 100).toFixed(1) }}%</template></el-table-column>
                <el-table-column label="信号覆盖" width="110"><template #default="{ row }">{{ (row.signal_coverage * 100).toFixed(1) }}%</template></el-table-column>
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </el-card>

        <el-card v-for="report in reports" :key="report.report_id" class="section">
          <template #header>
            <div class="header-row">
              <span>诊断报告 V{{ report.report_sequence }}</span>
              <span><el-tag>{{ report.diagnosis_level }}</el-tag><el-tag type="info">{{ report.publish_status }}</el-tag></span>
            </div>
          </template>
          <p class="summary">{{ report.summary }}</p>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="置信度">{{ (report.confidence * 100).toFixed(1) }}%</el-descriptions-item>
            <el-descriptions-item label="主要判断">{{ report.primary_hypothesis || '无确定性根因' }}</el-descriptions-item>
          </el-descriptions>
          <div class="report-grid">
            <section><h4>支持证据</h4><pre>{{ pretty(report.supporting_evidence) }}</pre></section>
            <section><h4>缺失证据</h4><pre>{{ pretty(report.missing_evidence) }}</pre></section>
            <section><h4>恢复建议</h4><pre>{{ pretty(report.recommended_recovery) }}</pre></section>
            <section><h4>风险与回退</h4><pre>{{ pretty(report.risk_and_rollback) }}</pre></section>
          </div>
          <el-alert
            v-if="report.supplement_plan_id"
            class="section"
            type="warning"
            :closable="false"
            title="当前证据不足。自动补采已从精简版 P0 延后，请根据缺失证据人工联系客户补充。"
          />
          <div class="actions">
            <el-button v-if="report.publish_status === 'draft'" @click="openReview(report, 'submit_review')">提交审核</el-button>
            <template v-if="report.publish_status === 'review_pending'">
              <el-button type="success" @click="openReview(report, 'confirm')">编辑并确认</el-button>
              <el-button @click="openReview(report, 'return_to_draft')">退回草稿</el-button>
              <el-button type="danger" @click="openReview(report, 'reject')">驳回</el-button>
            </template>
            <el-button v-if="report.publish_status === 'engineer_confirmed'" type="primary" @click="openReview(report, 'publish')">发布给客户</el-button>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="报告审核" name="reviews">
        <div class="toolbar"><el-button :loading="managementLoading" @click="loadReportReviews">刷新审核队列</el-button></div>
        <el-table v-loading="managementLoading" :data="reportReviews" border>
          <el-table-column label="工单号" width="165"><template #default="{ row }">{{ row.details.case_id || '—' }}</template></el-table-column>
          <el-table-column label="报告版本" width="100"><template #default="{ row }">V{{ row.details.report_sequence }}</template></el-table-column>
          <el-table-column prop="status" label="审核状态" width="165" />
          <el-table-column label="诊断级别" width="120"><template #default="{ row }">{{ row.details.diagnosis_level }}</template></el-table-column>
          <el-table-column label="摘要" min-width="300"><template #default="{ row }">{{ row.details.summary }}</template></el-table-column>
          <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click="openManagedSession(row)">审核</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="安全隔离" name="security">
        <div class="toolbar"><el-button :loading="managementLoading" @click="loadSecurityEvents">刷新安全事件</el-button></div>
        <el-alert title="这里只展示安全元数据。被拒绝的证据包不会进入解析和诊断链路。" type="warning" :closable="false" class="section" />
        <el-table v-loading="managementLoading" :data="securityEvents" border class="section">
          <el-table-column prop="resource_id" label="证据包标识" min-width="285" />
          <el-table-column prop="session_id" label="诊断会话标识" min-width="285" />
          <el-table-column prop="status" label="处置状态" width="140" />
          <el-table-column label="失败代码" width="220"><template #default="{ row }">{{ row.details.failure_code }}</template></el-table-column>
          <el-table-column label="安全结果" min-width="320"><template #default="{ row }"><pre>{{ pretty(row.details.security_results) }}</pre></template></el-table-column>
          <el-table-column label="操作" fixed="right" width="170">
            <template #default="{ row }">
              <el-button v-if="row.status === 'open'" link type="warning" @click="reviewSecurityEvent(row, 'acknowledge')">确认</el-button>
              <el-button v-if="row.status !== 'cleared'" link type="success" @click="reviewSecurityEvent(row, 'clear')">清除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="KBD 同步与版本" name="sync">
        <div class="toolbar">
          <el-button type="primary" :loading="syncLoading" @click="previewSync('incremental')">同步 KBD + Tool（增量检测）</el-button>
          <el-button :loading="syncLoading" @click="previewSync('full')">全量重新检测</el-button>
          <el-button :loading="syncLoading" @click="loadSyncHistory">刷新历史</el-button>
          <span>共 {{ syncTotal }} 个同步批次</span>
        </div>
        <el-alert
          title="系统联合已发布 KBD 结构化信号与已发布 Tool Registry（工具注册表）命令/指引模板生成候选资源。发布前不影响当前资源；发布后可对最后一批整体回滚。"
          type="info"
          :closable="false"
        />
        <el-table v-loading="syncLoading" :data="syncBatches" border class="section">
          <el-table-column prop="batch_id" label="同步批次" min-width="285" />
          <el-table-column prop="sync_mode" label="模式" width="105" />
          <el-table-column prop="status" label="状态" width="130" />
          <el-table-column prop="kbd_change_count" label="KBD 修订" width="105" />
          <el-table-column prop="tool_change_count" label="Tool 修订" width="105" />
          <el-table-column label="资源差异" width="155">
            <template #default="{ row }">C {{ row.collector_change_count }} / P {{ row.profile_change_count }} / M {{ row.mapping_change_count }}</template>
          </el-table-column>
          <el-table-column label="KBD / Tool 游标" width="220"><template #default="{ row }">K {{ row.base_cursor }} → {{ row.target_cursor }} / T {{ row.base_tool_cursor }} → {{ row.target_tool_cursor }}</template></el-table-column>
          <el-table-column prop="requested_by" label="发起人" width="140" />
          <el-table-column prop="created_at" label="发起时间" min-width="180" />
          <el-table-column label="操作" fixed="right" width="250">
            <template #default="{ row }">
              <el-button link type="primary" @click="openSyncBatch(row)">查看</el-button>
              <el-button v-if="row.status === 'candidate'" link type="success" @click="publishSyncBatch(row)">审核发布</el-button>
              <el-button v-if="row.status === 'candidate'" link type="danger" @click="rejectSyncBatch(row)">拒绝</el-button>
              <el-button
                v-if="row.status === 'published' || row.status === 'rollback_failed'"
                link
                type="warning"
                @click="rollbackSyncBatch(row)"
              >{{ row.status === 'rollback_failed' ? '重试回滚' : '整批回滚' }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="Collector（采集器）治理" name="collectors">
        <div class="toolbar">
          <el-select v-model="collectorStatus" clearable placeholder="审批状态" style="width: 150px">
            <el-option label="草稿" value="draft" />
            <el-option label="已批准" value="approved" />
            <el-option label="已驳回" value="rejected" />
          </el-select>
          <el-select v-model="collectorEnabled" clearable placeholder="启用状态" style="width: 150px">
            <el-option label="已启用" value="true" />
            <el-option label="已禁用" value="false" />
          </el-select>
          <el-button :loading="collectorLoading" @click="loadCollectors">刷新</el-button>
          <el-button type="primary" @click="openNewCollector">新建 Collector（采集器）</el-button>
        </div>

        <el-alert
          title="KBD 同步生成的 Collector（采集器）只能通过同步批次变更和回滚；仅人工管理资源可在本页编辑。客户制品按工单即时组装。"
          type="info"
          :closable="false"
        />

        <el-table v-loading="collectorLoading" :data="collectors" border class="section">
          <el-table-column prop="collector_id" label="Collector ID（采集器标识）" min-width="220" />
          <el-table-column prop="display_name" label="名称" min-width="180" />
          <el-table-column label="平台/执行器" width="150">
            <template #default="{ row }">{{ row.platform }} / {{ row.executor }}</template>
          </el-table-column>
          <el-table-column prop="version" label="语义版本" width="110" />
          <el-table-column label="治理来源" width="125"><template #default="{ row }"><el-tag :type="row.managed_by === 'kbd_sync' ? 'primary' : 'info'">{{ row.managed_by === 'kbd_sync' ? 'KBD 同步' : '人工' }}</el-tag></template></el-table-column>
          <el-table-column prop="active_revision" label="生效修订" width="100" />
          <el-table-column label="审批状态" width="110">
            <template #default="{ row }">
              <el-tag :type="row.review_status === 'approved' ? 'success' : row.review_status === 'rejected' ? 'danger' : 'warning'">
                {{ row.review_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="80">
            <template #default="{ row }">{{ row.is_enabled ? '是' : '否' }}</template>
          </el-table-column>
          <el-table-column label="操作" fixed="right" width="285">
            <template #default="{ row }">
              <el-button link type="primary" @click="openCollector(row)">{{ row.managed_by === 'kbd_sync' ? '查看' : '编辑' }}</el-button>
              <el-button v-if="row.managed_by !== 'kbd_sync' && row.review_status !== 'approved'" link type="success" @click="approveCollector(row)">
                批准
              </el-button>
              <el-button v-if="row.managed_by !== 'kbd_sync' && row.review_status !== 'rejected'" link type="danger" @click="rejectCollector(row)">
                驳回
              </el-button>
              <el-button v-if="row.managed_by !== 'kbd_sync' && row.is_enabled" link type="warning" @click="disableCollector(row)">禁用</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="Collection Profile（采集画像）" name="profiles">
        <div class="toolbar">
          <el-button :loading="profileLoading" @click="loadProfiles">刷新</el-button>
          <el-button type="primary" @click="openNewProfile">新建采集画像</el-button>
        </div>
        <el-alert
          title="KBD 同步生成的 Collection Profile（采集画像）只能通过同步批次变更和回滚；仅人工管理画像可在本页编辑。"
          type="info"
          :closable="false"
        />
        <el-table v-loading="profileLoading" :data="collectionProfiles" border class="section">
          <el-table-column label="Profile ID（画像标识）" min-width="210"><template #default="{ row }">{{ row.profile.profile_id }}</template></el-table-column>
          <el-table-column label="名称" min-width="170"><template #default="{ row }">{{ row.profile.display_name }}</template></el-table-column>
          <el-table-column label="场景" min-width="180"><template #default="{ row }">{{ scenarioLabel(row.profile.scenario) }}</template></el-table-column>
          <el-table-column prop="version" label="语义版本" width="100" />
          <el-table-column label="治理来源" width="125"><template #default="{ row }"><el-tag :type="row.managed_by === 'kbd_sync' ? 'primary' : 'info'">{{ row.managed_by === 'kbd_sync' ? 'KBD 同步' : '人工' }}</el-tag></template></el-table-column>
          <el-table-column prop="revision" label="生效修订" width="100" />
          <el-table-column label="采集项" width="80"><template #default="{ row }">{{ row.profile.items.length }}</template></el-table-column>
          <el-table-column label="审批状态" width="110"><template #default="{ row }"><el-tag :type="row.review_status === 'approved' ? 'success' : row.review_status === 'rejected' ? 'danger' : 'warning'">{{ row.review_status }}</el-tag></template></el-table-column>
          <el-table-column label="启用" width="70"><template #default="{ row }">{{ row.is_enabled ? '是' : '否' }}</template></el-table-column>
          <el-table-column label="操作" fixed="right" width="260">
            <template #default="{ row }">
              <el-button link type="primary" @click="openProfile(row)">{{ row.managed_by === 'kbd_sync' ? '查看' : '编辑' }}</el-button>
              <el-button v-if="row.managed_by !== 'kbd_sync' && row.review_status !== 'approved'" link type="success" @click="reviewProfile(row, true)">批准</el-button>
              <el-button v-if="row.managed_by !== 'kbd_sync' && row.review_status !== 'rejected'" link type="danger" @click="reviewProfile(row, false)">驳回</el-button>
              <el-button v-if="row.managed_by !== 'kbd_sync' && row.is_enabled" link type="warning" @click="disableProfile(row)">禁用</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="Collection Plan（采集计划）" name="plans">
        <div class="toolbar"><el-button :loading="planLoading" @click="loadCollectionPlans">刷新</el-button></div>
        <el-alert title="计划保存生成时使用的画像修订和精确 KBD 规则集。重生成会创建新修订、作废旧计划并自动撤销旧制品。" type="info" :closable="false" />
        <el-table v-loading="planLoading" :data="collectionPlans" border class="section">
          <el-table-column prop="collection_plan_id" label="Plan ID（计划标识）" min-width="285" />
          <el-table-column prop="session_id" label="会话标识" min-width="285" />
          <el-table-column label="轮次/修订" width="110"><template #default="{ row }">{{ row.plan_sequence }} / R{{ row.plan_revision }}</template></el-table-column>
          <el-table-column prop="profile_name" label="采集画像" min-width="180" />
          <el-table-column label="KBD 规则" width="110"><template #default="{ row }">{{ row.kbd_ruleset_snapshot.length }} 条</template></el-table-column>
          <el-table-column prop="status" label="状态" width="110" />
          <el-table-column label="创建时间" min-width="180"><template #default="{ row }">{{ row.created_at }}</template></el-table-column>
          <el-table-column label="操作" fixed="right" width="100"><template #default="{ row }"><el-button v-if="row.status === 'ready'" link type="warning" @click="regeneratePlan(row)">重生成</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="Collector Artifact（采集器制品）" name="artifacts">
        <div class="toolbar"><el-button :loading="artifactLoading" @click="loadCollectorArtifacts">刷新</el-button></div>
        <el-alert title="制品不可编辑、不可删除，只能撤销；历史签名、内容哈希和计划关联永久保留用于审计。" type="info" :closable="false" />
        <el-table v-loading="artifactLoading" :data="collectorArtifacts" border class="section">
          <el-table-column prop="artifact_id" label="Artifact ID（制品标识）" min-width="285" />
          <el-table-column prop="file_name" label="文件名" min-width="220" />
          <el-table-column prop="target_key" label="目标节点" min-width="150" />
          <el-table-column label="采集器数" width="100"><template #default="{ row }">{{ row.items.length }}</template></el-table-column>
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column prop="expires_at" label="过期时间" min-width="190" />
          <el-table-column label="操作" fixed="right" width="150">
            <template #default="{ row }">
              <el-button link type="primary" @click="openArtifactDetail(row)">执行清单</el-button>
              <el-button v-if="row.status === 'ready'" link type="danger" @click="revokeArtifact(row)">撤销</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="Signal Mapping（信号映射）" name="mappings">
        <div class="toolbar">
          <el-button :loading="mappingLoading" @click="loadSignalMappings">刷新</el-button>
          <el-button type="primary" @click="openNewMapping">新建映射</el-button>
        </div>
        <el-alert
          title="新计划只使用精确绑定 KBD 修订、Signal ID（信号标识）与执行契约的映射；历史宽泛映射仅保留审计，需通过 KBD 同步重建。只有已审批且已启用的 Collector（采集器）可以进入客户制品。"
          type="info"
          :closable="false"
        />
        <el-table v-loading="mappingLoading" :data="signalMappings" border class="section">
          <el-table-column prop="source_kbd_id" label="KBD ID" width="100" />
          <el-table-column prop="source_kbd_revision" label="KBD 修订" width="100" />
          <el-table-column prop="source_signal_id" label="Signal ID（信号标识）" min-width="150" />
          <el-table-column prop="acquire_tool" label="Acquire Tool（采集工具）" min-width="160" />
          <el-table-column prop="category_scope" label="分类范围" min-width="150" />
          <el-table-column prop="command_scope" label="命令范围" min-width="150" />
          <el-table-column prop="collector_id" label="Collector（采集器）" min-width="200" />
          <el-table-column prop="query_type" label="查询类型" width="150" />
          <el-table-column prop="priority" label="优先级" width="90" />
          <el-table-column label="启用" width="80">
            <template #default="{ row }">{{ row.is_enabled ? '是' : '否' }}</template>
          </el-table-column>
          <el-table-column prop="lock_version" label="锁版本" width="90" />
          <el-table-column label="操作" fixed="right" width="90">
            <template #default="{ row }">
              <el-button link type="primary" @click="openMapping(row)">编辑</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="采集器信任" name="collector-trust">
        <div class="toolbar">
          <el-button :loading="managementLoading" @click="loadCollectorTrust">刷新</el-button>
        </div>
        <el-alert
          title="当前页面只读展示制品验证所需的 Trust Store（信任库）和 Revocation List（吊销清单）；密钥轮换和批量撤销编排留到后续阶段。"
          type="info"
          :closable="false"
        />
        <el-descriptions class="section" :column="2" border>
          <el-descriptions-item label="信任库版本">{{ trustStore?.schema_version || '—' }}</el-descriptions-item>
          <el-descriptions-item label="受信密钥数">{{ trustStore?.keys.length || 0 }}</el-descriptions-item>
          <el-descriptions-item label="吊销清单版本">{{ revocations?.schema_version || '—' }}</el-descriptions-item>
          <el-descriptions-item label="已吊销制品数">{{ revocations?.revoked_artifacts.length || 0 }}</el-descriptions-item>
          <el-descriptions-item label="吊销清单下次更新时间" :span="2">
            {{ revocations?.next_update_at || '—' }}
          </el-descriptions-item>
        </el-descriptions>
        <el-collapse class="section">
          <el-collapse-item title="Trust Store（信任库）详情"><pre>{{ pretty(trustStore) }}</pre></el-collapse-item>
          <el-collapse-item title="Revocation List（吊销清单）详情"><pre>{{ pretty(revocations) }}</pre></el-collapse-item>
        </el-collapse>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="syncDialogVisible" title="KBD 与 Tool Registry 同步批次详情" width="1100px">
      <template v-if="syncDetail">
        <el-descriptions :column="3" border>
          <el-descriptions-item label="批次标识" :span="2">{{ syncDetail.batch_id }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ syncDetail.status }}</el-descriptions-item>
          <el-descriptions-item label="模式">{{ syncDetail.sync_mode }}</el-descriptions-item>
          <el-descriptions-item label="KBD 游标">{{ syncDetail.base_cursor }} → {{ syncDetail.target_cursor }}</el-descriptions-item>
          <el-descriptions-item label="KBD 修订数">{{ syncDetail.kbd_change_count }}</el-descriptions-item>
          <el-descriptions-item label="Tool 游标">{{ syncDetail.base_tool_cursor }} → {{ syncDetail.target_tool_cursor }}</el-descriptions-item>
          <el-descriptions-item label="Tool 修订数">{{ syncDetail.tool_change_count }}</el-descriptions-item>
          <el-descriptions-item label="发起人">{{ syncDetail.requested_by }}</el-descriptions-item>
          <el-descriptions-item label="批准人">{{ syncDetail.approved_by || '—' }}</el-descriptions-item>
          <el-descriptions-item label="回滚人">{{ syncDetail.rollback_by || '—' }}</el-descriptions-item>
          <el-descriptions-item label="Trace ID（链路标识）" :span="3">{{ syncDetail.trace_id }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          class="section"
          :type="syncResultType()"
          :title="syncResultTitle()"
          :closable="false"
        />
        <el-descriptions :column="4" border class="section">
          <el-descriptions-item label="已发布 KBD">{{ syncSummaryNumber('published_kbd_count') }}</el-descriptions-item>
          <el-descriptions-item label="共用场景 KBD">{{ syncSummaryNumber('shared_scenario_kbd_count') }}</el-descriptions-item>
          <el-descriptions-item label="分类异常 KBD">{{ syncSummaryNumber('unresolved_category_kbd_count') }}</el-descriptions-item>
          <el-descriptions-item label="候选资源">{{ syncSummaryNumber('candidate_change_count') }}</el-descriptions-item>
          <el-descriptions-item label="影响问题场景" :span="4">
            {{ syncSummaryList('impacted_scenarios').join('、') || '无' }}
          </el-descriptions-item>
          <el-descriptions-item
            v-if="syncSummaryList('unresolved_category_kbd_ids').length"
            label="分类异常 KBD ID"
            :span="4"
          >{{ syncSummaryList('unresolved_category_kbd_ids').join('、') }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-for="(item, index) in syncDetail.validation_json"
          :key="index"
          class="section"
          :type="item.severity === 'error' ? 'error' : item.severity === 'warning' ? 'warning' : 'info'"
          :title="String(item.message || item.code || '校验结果')"
          :closable="false"
        />
        <el-table :data="syncDetail.changes" border class="section" max-height="420">
          <el-table-column prop="resource_type" label="资源类型" width="170" />
          <el-table-column prop="resource_name" label="资源标识" min-width="220" />
          <el-table-column prop="change_type" label="变更" width="100" />
          <el-table-column label="版本" width="130"><template #default="{ row }">R{{ row.before_revision || '—' }} → R{{ row.after_revision || '候选' }}</template></el-table-column>
          <el-table-column label="来源 KBD" min-width="180"><template #default="{ row }">{{ row.source_kbd_ids.join(', ') || '—' }}</template></el-table-column>
          <el-table-column label="不可变来源修订" min-width="280"><template #default="{ row }"><pre>{{ pretty([...row.source_kbd_revisions, ...row.source_tool_revisions]) }}</pre></template></el-table-column>
          <el-table-column label="候选内容" min-width="260"><template #default="{ row }"><pre>{{ pretty(row.candidate_json) }}</pre></template></el-table-column>
        </el-table>
        <h4 class="section">动作与结果（追加式审计）</h4>
        <el-table :data="syncDetail.events" border>
          <el-table-column prop="event_sequence" label="#" width="60" />
          <el-table-column prop="created_at" label="时间" min-width="180" />
          <el-table-column prop="action" label="动作" width="110" />
          <el-table-column prop="result" label="结果" width="110" />
          <el-table-column prop="actor_id" label="操作者" width="150" />
          <el-table-column label="详情" min-width="300"><template #default="{ row }"><pre>{{ pretty(row.details_json) }}</pre></template></el-table-column>
        </el-table>
      </template>
      <template #footer>
        <el-button @click="syncDialogVisible = false">关闭</el-button>
        <el-button v-if="syncDetail?.status === 'candidate'" type="danger" @click="rejectSyncBatch(syncDetail)">拒绝</el-button>
        <el-button
          v-if="syncDetail?.status === 'candidate'"
          type="success"
          :disabled="isLegacySyncBatch()"
          @click="publishSyncBatch(syncDetail)"
        >{{ isLegacySyncBatch() ? '请重新全量检测' : '审核并发布' }}</el-button>
        <el-button
          v-if="syncDetail?.status === 'published' || syncDetail?.status === 'rollback_failed'"
          type="warning"
          @click="rollbackSyncBatch(syncDetail)"
        >{{ syncDetail.status === 'rollback_failed' ? '重试回滚' : '整批回滚' }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="artifactDetailVisible" title="Collector Artifact（采集器制品）执行清单" width="1100px">
      <el-alert
        title="以下内容来自该制品签名时固化的不可变执行清单，不受当前 Collector（采集器）模板后续修改影响。"
        type="info"
        :closable="false"
      />
      <el-descriptions v-if="artifactDetail" :column="2" border class="section">
        <el-descriptions-item label="Artifact ID（制品标识）">{{ artifactDetail.artifact_id }}</el-descriptions-item>
        <el-descriptions-item label="目标节点">{{ artifactDetail.target_key }}</el-descriptions-item>
        <el-descriptions-item label="Schema Version（模式版本）">{{ artifactDetail.schema_version }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ artifactDetail.status }}</el-descriptions-item>
        <el-descriptions-item label="SHA-256（摘要）" :span="2"><code>{{ artifactDetail.artifact_sha256 }}</code></el-descriptions-item>
      </el-descriptions>
      <el-table v-if="artifactDetail" :data="artifactDetail.items" border class="section">
        <el-table-column prop="sequence" label="#" width="60" />
        <el-table-column prop="collector_id" label="Collector ID（采集器标识）" min-width="200" />
        <el-table-column label="实际执行内容" min-width="470">
          <template #default="{ row }"><pre class="execution-spec">{{ formatArtifactExecution(row) }}</pre></template>
        </el-table-column>
        <el-table-column label="输出位置" min-width="220">
          <template #default="{ row }"><code>{{ row.output_contract?.output_path || '—' }}</code></template>
        </el-table-column>
        <el-table-column prop="timeout_seconds" label="超时（秒）" width="100" />
      </el-table>
      <template #footer><el-button @click="artifactDetailVisible = false">关闭</el-button></template>
    </el-dialog>

    <el-dialog v-model="reviewDialogVisible" title="诊断报告审核" width="820px">
      <el-form label-width="150px">
        <el-form-item label="审核动作"><el-tag>{{ reviewForm.action }}</el-tag></el-form-item>
        <el-form-item label="审核原因" required><el-input v-model="reviewForm.reason" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="结论摘要" required><el-input v-model="reviewForm.summary" type="textarea" :rows="5" /></el-form-item>
        <el-form-item label="恢复建议" required><el-input v-model="reviewForm.recommended_recovery" type="textarea" :rows="12" class="code-input" /></el-form-item>
      </el-form>
      <el-alert type="info" :closable="false" title="提交后保存变更前后快照、操作者、原因、版本和 Trace ID（链路标识）。" />
      <template #footer>
        <el-button @click="reviewDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="reviewSaving" @click="submitReview">保存并执行审核动作</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="collectorDialogVisible" :title="collectorReadOnly ? 'Collector（采集器）同步快照' : 'Collector（采集器）草稿'" width="760px">
      <el-form label-width="170px" :disabled="collectorReadOnly">
        <el-form-item label="Collector ID（采集器标识）" required>
          <el-input v-model="collectorForm.collector_id" :disabled="Boolean(collectorForm.originalId)" />
        </el-form-item>
        <el-form-item label="显示名称" required><el-input v-model="collectorForm.display_name" /></el-form-item>
        <el-form-item label="描述" required>
          <el-input v-model="collectorForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="平台/执行器" required>
          <el-select v-model="collectorForm.platform" @change="changeCollectorPlatform">
            <el-option label="Direct Command（直接命令）" value="linux" />
            <el-option label="HCI API（HCI 接口）" value="hci_api" />
            <el-option label="Manual（人工附件）" value="manual" />
          </el-select>
          <el-tag class="inline-tag" type="info">{{ collectorForm.executor }}</el-tag>
        </el-form-item>
        <el-form-item label="命令/指引模板" required>
          <el-input v-model="collectorForm.command_template" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item label="Parameter Schema（参数模式）" required>
          <el-input v-model="collectorForm.parameter_schema" type="textarea" :rows="7" class="code-input" />
        </el-form-item>
        <el-form-item label="超时/输出上限" required>
          <el-input-number v-model="collectorForm.timeout_seconds" :min="1" :max="3600" />
          <span class="unit">秒</span>
          <el-input-number v-model="collectorForm.max_output_mb" :min="0.1" :max="1024" />
          <span class="unit">MiB</span>
        </el-form-item>
        <el-form-item label="支持产品版本" required>
          <el-input v-model="collectorForm.supported_product_versions" placeholder="逗号分隔，例如 6.*, 7.*" />
        </el-form-item>
        <el-form-item label="输出 Schema ID（模式标识）" required>
          <el-input v-model="collectorForm.schema_id" />
        </el-form-item>
        <el-form-item label="输出 Media Type（媒体类型）" required>
          <el-input v-model="collectorForm.media_type" />
        </el-form-item>
        <el-form-item label="输出路径" required><el-input v-model="collectorForm.output_path" /></el-form-item>
        <el-form-item label="语义版本" required><el-input v-model="collectorForm.version" /></el-form-item>
      </el-form>
      <el-alert
        :title="collectorReadOnly ? '该资源由 KBD 与 Tool Registry 同步批次管理，请在“KBD 同步与版本”中查看来源、发布或回滚。' : '保存只创建草稿；批准后才发布新的不可变运行时修订版本。'"
        :type="collectorReadOnly ? 'info' : 'warning'"
        :closable="false"
      />
      <template #footer>
        <el-button @click="collectorDialogVisible = false">{{ collectorReadOnly ? '关闭' : '取消' }}</el-button>
        <el-button v-if="!collectorReadOnly" type="primary" :loading="collectorSaving" @click="saveCollector">保存草稿</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="profileDialogVisible" :title="profileReadOnly ? 'Collection Profile（采集画像）同步快照' : 'Collection Profile（采集画像）草稿'" width="860px">
      <el-form label-width="170px" :disabled="profileReadOnly">
        <el-form-item label="Profile ID（画像标识）" required>
          <el-input v-model="profileForm.profile_id" :disabled="Boolean(profileForm.originalId)" />
        </el-form-item>
        <el-form-item label="语义版本" required><el-input v-model="profileForm.version" /></el-form-item>
        <el-form-item label="Profile JSON（画像定义）" required>
          <el-input v-model="profileForm.definition" type="textarea" :rows="24" class="code-input" />
        </el-form-item>
      </el-form>
      <el-alert :title="profileReadOnly ? '该画像由 KBD 与 Tool Registry 同步批次管理，不可单独编辑。' : '保存后进入草稿态；批准时会校验所有 Collector（采集器）均已批准且启用。'" :type="profileReadOnly ? 'info' : 'warning'" :closable="false" />
      <template #footer>
        <el-button @click="profileDialogVisible = false">{{ profileReadOnly ? '关闭' : '取消' }}</el-button>
        <el-button v-if="!profileReadOnly" type="primary" :loading="profileSaving" @click="saveProfile">保存草稿</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mappingDialogVisible" title="Offline Signal Mapping（离线信号映射）" width="680px">
      <el-form label-width="170px">
        <el-form-item label="Mapping ID（映射标识）">
          <el-input v-model="mappingForm.mapping_id" disabled />
        </el-form-item>
        <el-form-item label="KBD ID（知识库标识）" required>
          <el-input-number v-model="mappingForm.source_kbd_id" :min="1" style="width: 100%" />
        </el-form-item>
        <el-form-item label="KBD Revision（知识库修订）" required>
          <el-input-number v-model="mappingForm.source_kbd_revision" :min="1" style="width: 100%" />
        </el-form-item>
        <el-form-item label="Signal ID（信号标识）" required>
          <el-input v-model="mappingForm.source_signal_id" />
        </el-form-item>
        <el-form-item label="Execution Contract Checksum（执行契约校验和）" required>
          <el-input v-model="mappingForm.execution_contract_checksum" class="code-input" />
        </el-form-item>
        <el-form-item label="Acquire Tool（采集工具）" required>
          <el-input v-model="mappingForm.acquire_tool" />
        </el-form-item>
        <el-form-item label="分类范围" required><el-input v-model="mappingForm.category_scope" /></el-form-item>
        <el-form-item label="命令范围" required><el-input v-model="mappingForm.command_scope" /></el-form-item>
        <el-form-item label="Collector ID（采集器标识）" required>
          <el-select v-model="mappingForm.collector_id" filterable style="width: 100%">
            <el-option
              v-for="item in collectors.filter((candidate) => candidate.review_status === 'approved' && candidate.is_enabled)"
              :key="item.collector_id"
              :label="`${item.display_name} (${item.collector_id})`"
              :value="item.collector_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="查询类型" required>
          <el-select v-model="mappingForm.query_type">
            <el-option label="Log（日志）" value="log" />
            <el-option label="JSON（结构化数据）" value="json" />
            <el-option label="Command Output（命令输出）" value="command_output" />
            <el-option label="Metric（指标）" value="metric" />
            <el-option label="Evidence Status（证据状态）" value="evidence_status" />
          </el-select>
        </el-form-item>
        <el-form-item label="Field Mapping（字段映射）">
          <el-input v-model="mappingForm.field_mapping" type="textarea" :rows="6" class="code-input" />
        </el-form-item>
        <el-form-item label="优先级"><el-input-number v-model="mappingForm.priority" :min="0" :max="10000" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="mappingForm.is_enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mappingDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="mappingSaving" @click="saveMapping">保存映射</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.identity-alert { margin-bottom: 16px; }
.section { margin-top: 20px; }
.header-row, .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.header-row > span:last-child { display: flex; gap: 8px; }
.toolbar { justify-content: flex-start; margin-bottom: 16px; }
.summary { margin-bottom: 16px; line-height: 1.7; }
.actions { margin-top: 16px; }
.report-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
.report-grid section { padding: 14px; border: 1px solid var(--el-border-color-light); border-radius: 6px; }
.report-grid h4 { margin: 0 0 10px; }
pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.inline-tag { margin-left: 12px; }
.unit { margin: 0 12px 0 6px; color: var(--el-text-color-secondary); }
.code-input :deep(textarea) { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
.execution-spec { line-height: 1.6; }
.collector-id { display: block; margin-top: 4px; color: var(--el-text-color-secondary); font-size: 11px; }
.failure-reason { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; }
@media (max-width: 900px) { .report-grid { grid-template-columns: 1fr; } }
</style>
