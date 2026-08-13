/**
 * API 客户端 - Axios 封装
 * 自动注入 X-Client-ID 和 traceparent 头
 */

import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'
import { generateUUID } from './utils/crypto'
import type {
  CaseCreate,
  CloseReason,
  CaseResponse,
  CaseListResponse,
  CaseStatsResponse,
  ClientListResponse,
  ConversationResponse,
  MessageResponse,
  AssistantInfo,
  AssistantsResponse,
  EnvType,
  EnvironmentCreate,
  EnvironmentResponse,
  EnvironmentListResponse,
  EnvironmentContextResponse,
  CaseUpdate,
  DiagnosisSession,
  DiagnosisWorkspace,
  DiagnosisUploadSession,
  CollectionPlan,
  CollectorArtifact,
  EvidenceAssessment,
  SupplementPlan,
  EvidenceBundle,
  DiagnosisRun,
  SignalEvaluation,
  DiagnosisCandidate,
  DiagnosisReport,
  DiagnosisTimelineEvent,
  LegalHold,
  DeletionJob,
  OfflineScenario,
  OfflineScenarioOption,
  CollectorDefinition,
  CollectorDefinitionWrite,
  CollectorReviewStatus,
  OfflineSignalMapping,
  OfflineSignalMappingWrite,
  ManagedDiagnosisSessionList,
  DiagnosisManagementRecord,
  DiagnosisManagementAction,
  CollectorTrustStore,
  CollectorRevocationList,
  CollectionProfileSnapshot,
  KbdCollectionImpact,
  OfflineResourceSyncBatch,
  OfflineResourceSyncHistory,
} from './types'

/** 创建带通用拦截器的 Axios 实例 */
export function createApiClient(baseURL: string, clientId?: string): AxiosInstance {
  const client = axios.create({
    baseURL,
    timeout: 30000,
    headers: { 'Content-Type': 'application/json' },
  })

  // 请求拦截：注入 X-Client-ID
  client.interceptors.request.use((config) => {
    if (clientId) {
      config.headers['X-Client-ID'] = clientId
    }
    return config
  })

  // 响应拦截：统一错误处理
  client.interceptors.response.use(
    (res) => res,
    (error) => {
      console.error('[API Error]', error.response?.status, error.response?.data)
      return Promise.reject(error)
    },
  )

  return client
}

/** Case API 方法集合 */
export function createCaseApi(client: AxiosInstance) {
  return {
    /** 创建工单 */
    create(data: CaseCreate) {
      return client.post<CaseResponse>('/cases/', data)
    },

    /** 获取工单详情 */
    getById(caseId: string) {
      return client.get<CaseResponse>(`/cases/${caseId}`)
    },

    /** 查询客户端的工单列表 */
    listByClient(clientId: string) {
      return client.get<CaseResponse[]>('/cases/', { params: { client_id: clientId } })
    },

    /** 确认工单 */
    confirm(caseId: string) {
      return client.put<CaseResponse>(`/cases/${caseId}/confirm`)
    },

    /** 关闭工单 */
    close(caseId: string, data?: { close_reason?: CloseReason }) {
      return client.put<CaseResponse>(`/cases/${caseId}/close`, data)
    },

    // ---- Admin ----

    /** [Admin] 所有工单列表 */
    listAll(params?: {
      skip?: number
      limit?: number
      status?: string
      client_id?: string
      case_id?: string
      title?: string
      start_time?: string
      end_time?: string
    }) {
      return client.get<CaseListResponse>('/cases/all', { params })
    },

    /** [Admin] 编辑工单 */
    update(caseId: string, data: CaseUpdate) {
      return client.put<CaseResponse>(`/cases/${caseId}`, data)
    },

    /** [Admin] 工单统计 */
    stats() {
      return client.get<CaseStatsResponse>('/cases/stats')
    },

    /** [Admin] 客户端列表 */
    clients() {
      return client.get<ClientListResponse>('/cases/clients')
    },
  }
}

/** Assistant API 方法集合 */
export function createAssistantApi(client: AxiosInstance) {
  return {
    /** 获取可用 AI 助手列表（v2.1：结构化响应）*/
    list() {
      return client.get<AssistantsResponse>('/assistants/')
    },
  }
}

/** Conversation API 方法集合 */
export function createConversationApi(client: AxiosInstance) {
  return {
    /** 创建会话（case_id 通过 query parameter 传递） */
    create(caseId: string, assistantType?: string) {
      return client.post<ConversationResponse>('/conversations/', null, {
        params: {
          case_id: caseId,
          ...(assistantType ? { assistant_type: assistantType } : {}),
        },
      })
    },

    /** 获取会话消息历史 */
    getMessages(conversationId: string) {
      return client.get<MessageResponse[]>(`/conversations/${conversationId}/messages`)
    },

    /** 发送消息并接收 SSE 流 */
    sendMessageStream(conversationId: string, content: string): EventSource {
      const params = new URLSearchParams({ content })
      const url = `/api/conversations/${conversationId}/message?${params.toString()}`
      // 使用 POST 方式需要通过 fetch，因 EventSource 仅支持 GET
      // 所以后端 SSE 端点需用 POST，这里改用 fetch + ReadableStream
      // 为与现有后端一致，直接返回 URL，让调用方自行处理
      return new EventSource(url)
    },
  }
}

/** PromptAudit API 方法集合（Admin 专用） */
export function createPromptAuditApi(client: AxiosInstance) {
  return {
    /** [Admin] 获取工单的 PromptAudit 记录列表 */
    listByCaseId(caseId: string, params?: { limit?: number; offset?: number; include_messages?: boolean }) {
      return client.get<{
        case_id: string
        total: number
        offset: number
        limit: number
        records: Array<{
          audit_id: string
          conversation_id: string | null
          assistant_type: string | null
          model: string | null
          has_sop: boolean | null
          kb_chunks_count: number | null
          kb_top_score: number | null
          system_prompt_chars: number | null
          message_count: number | null
          user_rating: number | null
          captured_at: string | null
          messages?: any
        }>
      }>(`/cases/${caseId}/prompt-audit`, { params })
    },
  }
}

/** AuditLog API 方法集合（Admin 专用） */
export function createAuditLogApi(client: AxiosInstance) {
  return {
    /** [Admin] 查询工具调用审计日志 */
    list(params?: {
      session_id?: string
      tool_name?: string
      risk_level?: number
      limit?: number
      offset?: number
    }) {
      return client.get<{
        total: number
        limit: number
        offset: number
        items: Array<{
          id: string
          session_id: string
          tool_name: string
          tool_args: any
          risk_level: number
          policy: string | null
          authorized_by: string | null
          result: any
          error: string | null
          started_at: string | null
          completed_at: string | null
          duration_ms: number | null
          trace_id: string | null
        }>
      }>('/api/v1/audit-logs', { params })
    },
  }
}

/** Offline Diagnosis（离线诊断）控制面和直传数据面 API */
export function createOfflineDiagnosisApi(
  client: AxiosInstance,
  identity: { token?: string | (() => string | undefined); tenantId?: string; actorId?: string },
) {
  const accessToken = () => (typeof identity.token === 'function' ? identity.token() : identity.token)
  const headers = () => {
    const token = accessToken()
    return {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(identity.tenantId ? { 'X-Tenant-ID': identity.tenantId } : {}),
      ...(identity.actorId ? { 'X-Actor-ID': identity.actorId } : {}),
    }
  }
  return {
    createSession(data: {
      case_id: string
      selected_scenario: OfflineScenario
      selected_category?: string
      incident: { start_time: string; end_time: string; timezone: string }
      affected_objects: Array<{ type: string; id?: string; name?: string; source_node?: string }>
      impact_scope: string
      current_status: 'ongoing' | 'recovered' | 'intermittent'
      recent_change_description?: string
    }, idempotencyKey?: string) {
      return client.post<DiagnosisSession>('/diagnosis-sessions', data, {
        headers: { ...headers(), 'Idempotency-Key': idempotencyKey || generateUUID() },
      })
    },
    listScenarios() {
      return client.get<OfflineScenarioOption[]>('/diagnosis-scenarios', { headers: headers() })
    },
    getSession(sessionId: string) {
      return client.get<DiagnosisSession>(`/diagnosis-sessions/${sessionId}`, { headers: headers() })
    },
    resumeWorkspace(caseId: string) {
      return client.get<DiagnosisWorkspace>(`/diagnosis-sessions/by-case/${encodeURIComponent(caseId)}/workspace`, {
        headers: headers(),
      })
    },
    createPlan(sessionId: string, productVersion: string, idempotencyKey?: string) {
      return client.post<CollectionPlan>(
        `/diagnosis-sessions/${sessionId}/collection-plans`,
        { product_version: productVersion, context: {} },
        { headers: { ...headers(), 'Idempotency-Key': idempotencyKey || generateUUID() } },
      )
    },
    getPlan(sessionId: string, planId: string) {
      return client.get<CollectionPlan>(`/diagnosis-sessions/${sessionId}/collection-plans/${planId}`, {
        headers: headers(),
      })
    },
    createArtifact(sessionId: string, planId: string, targetNode?: string, idempotencyKey?: string) {
      return client.post<CollectorArtifact>(
        `/diagnosis-sessions/${sessionId}/collector-artifacts`,
        { collection_plan_id: planId, target_node: targetNode || undefined, parameters_by_item: {} },
        { headers: { ...headers(), 'Idempotency-Key': idempotencyKey || generateUUID() } },
      )
    },
    getArtifact(sessionId: string, artifactId: string) {
      return client.get<CollectorArtifact>(
        `/diagnosis-sessions/${sessionId}/collector-artifacts/${artifactId}`,
        { headers: headers() },
      )
    },
    createUpload(
      sessionId: string,
      data: {
        bundle_type: 'initial' | 'supplement'
        parent_bundle_id?: string
        collection_plan_id: string
        collector_artifact_id: string
        file_name: string
        media_type: 'application/vnd.hci.evidence'
        total_size_bytes: number
        sha256?: string
      },
    ) {
      return client.post<DiagnosisUploadSession>(`/diagnosis-sessions/${sessionId}/uploads`, data, {
        headers: { ...headers(), 'Idempotency-Key': generateUUID() },
      })
    },
    async uploadFile(
      upload: DiagnosisUploadSession,
      file: File,
      onProgress: (percent: number) => void,
      uploadedParts: number[] = [],
    ) {
      if (!upload.upload_token) throw new Error('上传会话未返回一次性直传令牌')
      const completed = [...uploadedParts]
      for (const target of upload.upload_targets) {
        if (completed.includes(target.part_number)) continue
        const start = (target.part_number - 1) * upload.chunk_size_bytes
        const end = Math.min(file.size, start + upload.chunk_size_bytes)
        await axios.put(target.upload_url, file.slice(start, end), {
          headers: {
            'Content-Type': 'application/octet-stream',
            'X-Upload-Token': upload.upload_token,
          },
          timeout: 10 * 60 * 1000,
        })
        completed.push(target.part_number)
        onProgress(Math.round((completed.length / upload.part_count) * 100))
      }
      return completed
    },
    getUpload(sessionId: string, uploadId: string) {
      return client.get<DiagnosisUploadSession>(`/diagnosis-sessions/${sessionId}/uploads/${uploadId}`, {
        headers: headers(),
      })
    },
    abortUpload(sessionId: string, uploadId: string) {
      return client.post<{ upload_id: string; status: string }>(
        `/diagnosis-sessions/${sessionId}/uploads/${uploadId}/abort`,
        undefined,
        { headers: headers() },
      )
    },
    completeUpload(sessionId: string, uploadId: string, parts: number[]) {
      return client.post<EvidenceBundle>(
        `/diagnosis-sessions/${sessionId}/uploads/${uploadId}/complete`,
        { parts },
        { headers: headers() },
      )
    },
    listBundles(sessionId: string) {
      return client.get<EvidenceBundle[]>(`/diagnosis-sessions/${sessionId}/bundles`, { headers: headers() })
    },
    getAssessment(sessionId: string) {
      return client.get<EvidenceAssessment>(`/diagnosis-sessions/${sessionId}/assessment`, { headers: headers() })
    },
    getSupplementPlan(sessionId: string) {
      return client.get<SupplementPlan>(`/diagnosis-sessions/${sessionId}/supplement-plan`, {
        headers: headers(),
      })
    },
    listRuns(sessionId: string) {
      return client.get<DiagnosisRun[]>(`/diagnosis-sessions/${sessionId}/runs`, { headers: headers() })
    },
    loadOriginalRuleSnapshot(sessionId: string) {
      return client.post<DiagnosisRun>(
        `/internal/diagnosis-sessions/${sessionId}/diagnose`,
        { use_latest_rules: false },
        { headers: headers() },
      )
    },
    listSignals(sessionId: string, runId: string) {
      return client.get<SignalEvaluation[]>(`/diagnosis-sessions/${sessionId}/runs/${runId}/signals`, {
        headers: headers(),
      })
    },
    listCandidates(sessionId: string, runId: string) {
      return client.get<DiagnosisCandidate[]>(`/diagnosis-sessions/${sessionId}/runs/${runId}/candidates`, {
        headers: headers(),
      })
    },
    listReports(sessionId: string) {
      return client.get<DiagnosisReport[]>(`/diagnosis-sessions/${sessionId}/reports`, { headers: headers() })
    },
    reviewReport(
      sessionId: string,
      report: DiagnosisReport,
      action: 'submit_review' | 'confirm' | 'publish' | 'reject' | 'return_to_draft',
      reason: string,
      edits?: { summary?: string; recommended_recovery?: Array<Record<string, unknown>> },
    ) {
      return client.post<DiagnosisReport>(
        `/diagnosis-sessions/${sessionId}/reports/${report.report_id}/review`,
        { action, reason, ...edits },
        { headers: { ...headers(), 'If-Match': `"${report.version}"` } },
      )
    },
    getTimeline(sessionId: string) {
      return client.get<DiagnosisTimelineEvent[]>(`/diagnosis-sessions/${sessionId}/timeline`, {
        headers: headers(),
      })
    },
    getLegalHold(sessionId: string) {
      return client.get<LegalHold>(`/diagnosis-sessions/${sessionId}/legal-hold`, { headers: headers() })
    },
    updateLegalHold(sessionId: string, action: 'apply' | 'release', reason: string) {
      return client.post<LegalHold>(
        `/diagnosis-sessions/${sessionId}/legal-hold`,
        { action, reason },
        { headers: headers() },
      )
    },
    requestDeletion(sessionId: string, reason: string) {
      return client.post<DeletionJob>(
        `/diagnosis-sessions/${sessionId}/deletion`,
        { reason },
        { headers: headers() },
      )
    },
    getDeletion(sessionId: string) {
      return client.get<DeletionJob>(`/diagnosis-sessions/${sessionId}/deletion`, { headers: headers() })
    },
    executeDeletion(sessionId: string) {
      return client.post<DeletionJob>(
        `/internal/diagnosis-sessions/${sessionId}/deletion/execute`,
        undefined,
        { headers: headers() },
      )
    },
    listManagedSessions(params?: { query?: string; status?: string; assigned_to?: string; offset?: number; limit?: number }) {
      return client.get<ManagedDiagnosisSessionList>('/internal/diagnosis-sessions', {
        headers: headers(),
        params,
      })
    },
    assignSession(sessionId: string, assignedTo: string) {
      return client.post<DiagnosisManagementAction>(
        `/internal/diagnosis-sessions/${sessionId}/assign`,
        { assigned_to: assignedTo },
        { headers: headers() },
      )
    },
    terminateSession(sessionId: string, reason: string) {
      return client.post<DiagnosisManagementAction>(
        `/internal/diagnosis-sessions/${sessionId}/terminate`,
        { reason },
        { headers: headers() },
      )
    },
    retrySessionProcessing(sessionId: string) {
      return client.post<DiagnosisManagementAction>(
        `/internal/diagnosis-sessions/${sessionId}/retry-processing`,
        undefined,
        { headers: headers() },
      )
    },
    listReportReviews() {
      return client.get<DiagnosisManagementRecord[]>('/internal/diagnosis-sessions/report-reviews', {
        headers: headers(),
      })
    },
    listSecurityEvents() {
      return client.get<DiagnosisManagementRecord[]>('/internal/diagnosis-security/events', {
        headers: headers(),
      })
    },
    reviewSecurityEvent(bundleId: string, action: 'acknowledge' | 'clear', note: string) {
      return client.post<DiagnosisManagementAction>(
        `/internal/diagnosis-security/events/${bundleId}/review`,
        { action, note },
        { headers: headers() },
      )
    },
    listDiagnosisGovernance() {
      return client.get<DiagnosisManagementRecord[]>('/internal/diagnosis-sessions/governance', {
        headers: headers(),
      })
    },
    listDiagnosisAudit(limit = 200) {
      return client.get<DiagnosisManagementRecord[]>('/internal/diagnosis-sessions/audit', {
        headers: headers(),
        params: { limit },
      })
    },
    getCollectorTrustStore() {
      return client.get<CollectorTrustStore>('/internal/collectors/security/trust-store', {
        headers: headers(),
      })
    },
    getCollectorRevocations() {
      return client.get<CollectorRevocationList>('/internal/collectors/security/revocations', {
        headers: headers(),
      })
    },
    listCollectionProfiles() {
      return client.get<CollectionProfileSnapshot[]>('/internal/collection-profiles', {
        headers: headers(),
      })
    },
    saveCollectionProfile(profile: CollectionProfileSnapshot['profile'], version: string, lockVersion?: number) {
      return client.put<CollectionProfileSnapshot>(
        `/internal/collection-profiles/${encodeURIComponent(profile.profile_id)}`,
        { version, profile },
        { headers: { ...headers(), ...(lockVersion ? { 'If-Match': `"${lockVersion}"` } : {}) } },
      )
    },
    reviewCollectionProfile(profile: CollectionProfileSnapshot, approved: boolean, reason?: string) {
      return client.post<CollectionProfileSnapshot>(
        `/internal/collection-profiles/${encodeURIComponent(profile.profile.profile_id)}/review`,
        { approved, reason: reason || undefined },
        { headers: { ...headers(), 'If-Match': `"${profile.lock_version}"` } },
      )
    },
    disableCollectionProfile(profile: CollectionProfileSnapshot) {
      return client.post<CollectionProfileSnapshot>(
        `/internal/collection-profiles/${encodeURIComponent(profile.profile.profile_id)}/disable`,
        undefined,
        { headers: { ...headers(), 'If-Match': `"${profile.lock_version}"` } },
      )
    },
    listCollectionPlans(params?: { status?: string; session_id?: string }) {
      return client.get<CollectionPlan[]>('/internal/collection-plans', { headers: headers(), params })
    },
    regenerateCollectionPlan(planId: string, reason: string) {
      return client.post<CollectionPlan>(
        `/internal/collection-plans/${planId}/regenerate`,
        { reason },
        { headers: { ...headers(), 'Idempotency-Key': generateUUID() } },
      )
    },
    listCollectorArtifacts(params?: { status?: string; session_id?: string }) {
      return client.get<CollectorArtifact[]>('/internal/collector-artifacts', { headers: headers(), params })
    },
    revokeManagedArtifact(artifactId: string, reason: string) {
      return client.post<CollectorArtifact>(
        `/internal/collector-artifacts/${artifactId}/revoke`,
        { reason },
        { headers: headers() },
      )
    },
    getKbdCollectionImpact(kbdId: number) {
      return client.get<KbdCollectionImpact>(`/internal/kbd-collection-impact/${kbdId}`, {
        headers: headers(),
      })
    },
    previewOfflineResourceSync(mode: 'incremental' | 'full' = 'incremental') {
      return client.post<OfflineResourceSyncBatch>(
        '/internal/offline-resource-sync/preview',
        { mode },
        { headers: headers() },
      )
    },
    listOfflineResourceSyncHistory(offset = 0, limit = 50) {
      return client.get<OfflineResourceSyncHistory>('/internal/offline-resource-sync/history', {
        headers: headers(),
        params: { offset, limit },
      })
    },
    getOfflineResourceSyncBatch(batchId: string) {
      return client.get<OfflineResourceSyncBatch>(`/internal/offline-resource-sync/${batchId}`, {
        headers: headers(),
      })
    },
    publishOfflineResourceSync(batchId: string, reason: string) {
      return client.post<OfflineResourceSyncBatch>(
        `/internal/offline-resource-sync/${batchId}/publish`,
        { reason },
        { headers: headers() },
      )
    },
    rejectOfflineResourceSync(batchId: string, reason: string) {
      return client.post<OfflineResourceSyncBatch>(
        `/internal/offline-resource-sync/${batchId}/reject`,
        { reason },
        { headers: headers() },
      )
    },
    rollbackOfflineResourceSync(batchId: string, reason: string) {
      return client.post<OfflineResourceSyncBatch>(
        `/internal/offline-resource-sync/${batchId}/rollback`,
        { reason },
        { headers: headers() },
      )
    },
    listCollectors(params?: { review_status?: CollectorReviewStatus; is_enabled?: boolean }) {
      return client.get<CollectorDefinition[]>('/internal/collectors', {
        headers: headers(),
        params,
      })
    },
    getCollector(collectorId: string) {
      return client.get<CollectorDefinition>(`/internal/collectors/${encodeURIComponent(collectorId)}`, {
        headers: headers(),
      })
    },
    saveCollector(data: CollectorDefinitionWrite, lockVersion?: number) {
      return client.put<CollectorDefinition>(
        `/internal/collectors/${encodeURIComponent(data.collector_id)}`,
        data,
        {
          headers: {
            ...headers(),
            ...(lockVersion ? { 'If-Match': `"${lockVersion}"` } : {}),
          },
        },
      )
    },
    reviewCollector(collector: CollectorDefinition, approved: boolean, reason?: string) {
      return client.post<CollectorDefinition>(
        `/internal/collectors/${encodeURIComponent(collector.collector_id)}/review`,
        { approved, reason: reason || undefined },
        { headers: { ...headers(), 'If-Match': `"${collector.lock_version}"` } },
      )
    },
    disableCollector(collector: CollectorDefinition) {
      return client.post<CollectorDefinition>(
        `/internal/collectors/${encodeURIComponent(collector.collector_id)}/disable`,
        undefined,
        { headers: { ...headers(), 'If-Match': `"${collector.lock_version}"` } },
      )
    },
    listSignalMappings() {
      return client.get<OfflineSignalMapping[]>('/internal/offline-signal-mappings', { headers: headers() })
    },
    saveSignalMapping(mappingId: string, data: OfflineSignalMappingWrite, lockVersion?: number) {
      return client.put<OfflineSignalMapping>(`/internal/offline-signal-mappings/${mappingId}`, data, {
        headers: {
          ...headers(),
          ...(lockVersion ? { 'If-Match': `"${lockVersion}"` } : {}),
        },
      })
    },
  }
}

/** Environment API 方法集合（Custom-UI 数据采集） */
export function createEnvironmentApi(client: AxiosInstance) {
  return {
    /** 创建环境数据（alert/task/environment 采集） */
    create(data: EnvironmentCreate) {
      return client.post<EnvironmentResponse>('/environments/', data)
    },

    /** 获取工单所有环境数据 */
    listByCase(caseId: string) {
      return client.get<EnvironmentListResponse>(`/environments/case/${caseId}`)
    },

    /** 获取指定类型环境数据 */
    getByType(caseId: string, envType: EnvType) {
      return client.get<EnvironmentResponse>(`/environments/case/${caseId}/type/${envType}`)
    },

    /**
     * upsert 环境数据（幂等：有则更新，无则创建）
     * 业界最佳实践：REST PUT 幂等语义，多次调用结果相同
     * URL path 已指定资源位置，body 仅含 env_data 和可选的 collected_at
     */
    upsert(caseId: string, envType: EnvType, envData: Record<string, unknown>, collectedAt?: string) {
      return client.put<EnvironmentResponse>(`/environments/case/${caseId}/type/${envType}`, {
        env_data: envData,
        ...(collectedAt ? { collected_at: collectedAt } : {}),
      })
    },

    /** 获取 S0 阶段 Prompt 构建所需的环境上下文 */
    getContext(caseId: string) {
      return client.get<EnvironmentContextResponse>(`/environments/case/${caseId}/context`)
    },
  }
}
