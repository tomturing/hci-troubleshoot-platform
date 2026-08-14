/**
 * 共享类型定义 - 与后端 schemas.py 对应
 */

declare global {
  interface Window {
    /** 企业身份宿主在登录完成后提供的短期 OIDC Access Token（访问令牌）读取器。 */
    __HCI_AUTH__?: {
      getAccessToken?: () => string | undefined
    }
  }
}

/** 工单状态 */
export type CaseStatus = 'created' | 'confirmed' | 'in_progress' | 'resolved' | 'closed' | 'cancelled'

/** 工单关闭原因 */
export type CloseReason = 'user_command' | 'timeout' | 'abandon' | 'admin_close'

/** 消息角色 */
export type MessageRole = 'user' | 'assistant' | 'system' | 'command'

/** 工单响应 */
export interface CaseResponse {
  case_id: string
  client_id: string
  status: CaseStatus
  title: string
  description: string | null
  assistant_type?: string
  created_at: string
  updated_at: string
  closed_at: string | null
  trace_id: string | null
  priority?: string
  category?: string | null
}

/** 创建工单请求 */
export interface CaseCreate {
  client_id: string
  title: string
  description?: string
  assistant_type?: string  // v2.0: 可选，不传则系统自动分配
}

/** 编辑工单请求 */
export interface CaseUpdate {
  title?: string
  description?: string | null
  status?: CaseStatus
  priority?: string
  category?: string | null
  assistant_type?: string
}

/** AI 助手信息（v2.1 扩展）*/
export interface AssistantInfo {
  type: string
  display_name: string
  description: string
  capabilities: string[]  // 能力标签数组
  available: boolean
  is_default?: boolean    // 是否为默认助手
}

/** AI 助手列表响应（v2.1 结构化）*/
export interface AssistantsResponse {
  assistants: AssistantInfo[]
  show_selector: boolean       // 是否显示助手选择器
  default_assistant: string | null  // 默认助手类型
  selector_mode: string        // 选择器显示模式: auto/true/false
}

/** 工单分页列表响应 */
export interface CaseListResponse {
  items: CaseResponse[]
  total: number
  skip: number
  limit: number
}

/** 工单统计响应 */
export interface CaseStatsResponse {
  total: number
  by_status: Record<string, number>
}

/** 客户端信息 */
export interface ClientInfo {
  client_id: string
  case_count: number
  last_case_at: string | null
}

/** 客户端列表响应 */
export interface ClientListResponse {
  items: ClientInfo[]
  total: number
}

/** 消息响应 */
export interface MessageResponse {
  message_id: string
  conversation_id: string
  role: MessageRole
  content: string
  metadata: Record<string, unknown> | null
  created_at: string
  trace_id: string | null
}

/** 创建消息请求 */
export interface MessageCreate {
  case_id: string
  role: MessageRole
  content: string
  metadata?: Record<string, unknown>
  assistant_type?: string  // v2.2: 动态切换助手
}

/** 会话响应 */
export interface ConversationResponse {
  conversation_id: string
  case_id: string
  created_at: string
  updated_at: string
  trace_id: string | null
}

/** 状态标签配色映射 */
export const STATUS_LABELS: Record<CaseStatus, string> = {
  created: '待确认',
  confirmed: '已确认',
  in_progress: '处理中',
  resolved: '已解决',
  closed: '已关闭',
  cancelled: '已取消',
}

export const STATUS_COLORS: Record<CaseStatus, string> = {
  created: 'warning',
  confirmed: 'primary',
  in_progress: 'primary',
  resolved: 'success',
  closed: 'info',
  cancelled: 'danger',
}

// ──────────────────────────────────────────────
// Environment 服务契约模型（Custom-UI 数据采集）
// ──────────────────────────────────────────────

/** 环境数据类型 */
export type EnvType = 'cluster' | 'host' | 'vm' | 'network' | 'alert' | 'task'

/** 环境数据响应 */
export interface EnvironmentResponse {
  environment_id: string
  case_id: string
  env_type: EnvType
  env_data: Record<string, unknown>
  collected_at: string | null  // ISO 8601 datetime
  created_at: string  // ISO 8601 datetime
  updated_at: string  // ISO 8601 datetime
  trace_id: string | null
}

/** 创建环境数据请求 */
export interface EnvironmentCreate {
  case_id: string
  env_type: EnvType
  env_data: Record<string, unknown>
  collected_at?: string  // ISO 8601 datetime，可选
}

/** 工单环境数据列表响应 */
export interface EnvironmentListResponse {
  items: EnvironmentResponse[]
  total: number
}

/** S0 阶段 Prompt 构建所需的环境上下文响应 */
export interface EnvironmentContextResponse {
  env_info: Record<string, unknown>
  alert_logs: Array<{
    level: string       // "CRITICAL" | "WARNING"，来自 urgent_type 1/0
    time: string        // 可读时间字符串，来自 end 时间戳转换
    target: string      // 告警对象
    type: string        // 事件类型
    description: string // 告警描述
    host: string        // 主机名
    vm?: string         // 虚拟机名（可选）
  }>
  task_logs: Array<{
    status: string            // "失败" | "完成"，来自 status 整数 3/2
    process?: string          // process 原始值字符串：'100'=完成, '-1'=失败, '-2'=排队中, '-3'=取消中, '0~99'=进行中百分比
    type: string              // 任务行为/类型
    time: string              // 可读时间字符串，来自 end 时间戳转换
    host: string              // 主机名
    target: string            // 操作对象
    description: string       // 错误描述
    errcode_tracing: string   // 错误码
    trace_id: string          // 来自 request_id
    vm?: string               // 虚拟机名（可选）
  }>
}

// ──────────────────────────────────────────────
// Offline Diagnosis（离线诊断）契约
// ──────────────────────────────────────────────

export type OfflineScenario = string

export interface OfflineScenarioOption {
  scenario: OfflineScenario
  display_name: string
  profile_revision: number
  profile_version: string
  supported_product_versions: string[]
  requires_affected_object: boolean
}

export interface DiagnosisSession {
  session_id: string
  case_id: string
  tenant_id: string
  assigned_to: string | null
  product_line: string
  selected_scenario: OfflineScenario
  selected_category: string | null
  resolved_category: string | null
  incident: { start_time: string; end_time: string; timezone: string }
  affected_objects: Array<{ type: string; id?: string; name?: string; source_node?: string }>
  impact_scope: string
  current_status: 'ongoing' | 'recovered' | 'intermittent'
  experimental: boolean
  status: string
  supplement_count: number
  version: number
  trace_id: string
  created_at: string
  updated_at: string
}

export interface DiagnosisWorkspace {
  session: DiagnosisSession
  plan_id: string | null
  artifact_id: string | null
  active_upload_id: string | null
}

export interface CollectionPlanItem {
  item_id: string
  sequence: number
  collector_id: string
  collector_revision: number | null
  collector_version: string | null
  collector_checksum: string | null
  display_name: string
  required_level: 'mandatory' | 'recommended' | 'conditional' | 'optional'
  activation_state: string
  target: Record<string, unknown>
  time_window: Record<string, unknown>
  condition_snapshot: Record<string, unknown> | null
  reason: string
  expected_size_mb: number
  timeout_seconds: number
  required_permissions: string[]
  sensitive_data_types: string[]
}

export interface CollectionPlan {
  collection_plan_id: string
  session_id: string
  plan_sequence: number
  plan_revision: number
  profile_name: string
  profile_revision: number
  profile_version: string
  profile_checksum: string
  product_version: string
  kbd_ruleset_snapshot: Array<Record<string, unknown>>
  kbd_ruleset_checksum: string
  required_permissions: string[]
  sensitive_data_types: string[]
  unresolved_variables: string[]
  estimated_size_mb: number
  estimated_duration_seconds: number
  status: string
  trace_id: string
  created_at: string
  updated_at: string
  items: CollectionPlanItem[]
}

export interface CollectorArtifactItem {
  plan_item_id: string
  sequence: number
  collector_id: string
  collector_revision: number
  collector_checksum: string
  rendered_command: string
  execution_spec: {
    executor?: 'command' | 'http' | 'manual' | string
    argv?: string[]
    method?: string
    path?: string
    guide?: string
  }
  output_contract: Record<string, unknown>
  timeout_seconds: number
  max_output_bytes: number
}

export interface CollectorArtifact {
  artifact_id: string
  session_id: string
  collection_plan_id: string
  target_key: string
  artifact_type: string
  schema_version: string
  file_name: string
  download_path: string
  verification_bundle_path: string
  artifact_sha256: string
  signature_algorithm: string
  signature_base64: string
  signing_key_id: string
  public_key_base64: string
  public_key_fingerprint: string
  signed_at: string
  expires_at: string
  status: string
  revoked_at: string | null
  revoked_by: string | null
  revocation_reason: string | null
  manifest: Record<string, unknown>
  trace_id: string
  created_at: string
  items: CollectorArtifactItem[]
}

export interface UploadTarget {
  part_number: number
  upload_url: string
  expires_at: string
}

export interface DiagnosisUploadSession {
  upload_id: string
  session_id: string
  status: string
  bundle_type: 'initial' | 'supplement' | 'verification'
  total_size_bytes: number
  chunk_size_bytes: number
  part_count: number
  uploaded_parts: Record<string, { size_bytes: number; sha256: string }>
  upload_token: string | null
  upload_targets: UploadTarget[]
  expires_at: string
  trace_id: string
}

export interface EvidenceBundle {
  bundle_id: string
  session_id: string
  bundle_type: 'initial' | 'supplement' | 'verification'
  parent_bundle_id: string | null
  collection_plan_id: string
  collector_artifact_id: string
  size_bytes: number
  sha256: string
  schema_version: string | null
  processing_status: string
  security_results: Record<string, unknown>
  failure_code: string | null
  failure_message: string | null
  retention_until: string
  legal_hold: boolean
  version: number
  trace_id: string
  created_at: string
  updated_at: string
}

export interface MissingEvidence {
  plan_item_id?: string
  collector_id: string
  display_name?: string
  target?: Record<string, unknown>
  status: string
  reason?: string
  impact?: string
  failure_reasons?: string[]
  failure_details?: string[]
}

export interface EvidenceAssessment {
  assessment_id: string
  session_id: string
  completeness_score: number
  mandatory: { total: number; available: number }
  missing_evidence: MissingEvidence[]
  diagnosable_scope: string[]
  non_diagnosable_scope: string[]
  ready_for_diagnosis: boolean
  algorithm_version: string
  bundle_ids: string[]
  calculation_details: Record<string, unknown>
  trace_id: string
  created_at: string
}

export interface SupplementPlan {
  supplement_plan_id: string
  collection_plan_id: string
  parent_bundle_id: string
  session_id: string
  run_id: string
  reason: string
  confirmed_findings: string[]
  unconfirmed_findings: string[]
  collection_items: Array<Record<string, unknown>>
  expected_size_mb: number
  expected_duration_minutes: number
  status: 'ready' | 'collecting' | 'completed' | 'cancelled'
  trace_id: string
  created_at: string
  updated_at: string
}

export interface ManagedDiagnosisSession {
  session_id: string
  case_id: string
  customer_id: string | null
  selected_scenario: string
  status: string
  assigned_to: string | null
  supplement_count: number
  latest_report_status: string | null
  latest_report_sequence: number | null
  bundle_count: number
  failed_task_count: number
  trace_id: string
  created_at: string
  updated_at: string
}

export interface ManagedDiagnosisSessionList {
  items: ManagedDiagnosisSession[]
  total: number
  offset: number
  limit: number
}

export interface DiagnosisManagementRecord {
  record_type: string
  resource_id: string
  session_id: string | null
  status: string | null
  occurred_at: string
  trace_id: string
  details: Record<string, unknown>
}

export interface DiagnosisManagementAction {
  resource_id: string
  status: string
  trace_id: string
  details: Record<string, unknown>
}

export interface CollectorTrustStore {
  schema_version: string
  generated_at: string
  keys: Array<Record<string, unknown>>
}

export interface CollectorRevocationList {
  schema_version: string
  generated_at: string
  next_update_at: string
  revoked_artifacts: Array<Record<string, unknown>>
  document_signature: Record<string, unknown>
}

export interface CollectionProfileSnapshot {
  profile: {
    profile_id: string
    display_name: string
    product_line: 'HCI'
    scenario: string
    supported_product_versions: string[]
    items: Array<Record<string, unknown>>
  }
  revision: number | null
  version: string
  checksum: string | null
  managed_by: 'manual' | 'kbd_sync'
  generation_metadata: Record<string, unknown>
  review_status: CollectorReviewStatus
  is_enabled: boolean
  approved_by: string | null
  approved_at: string | null
  rejection_reason: string | null
  lock_version: number
  trace_id: string | null
  published_at: string | null
}

export interface KbdCollectionImpact {
  kbd: Record<string, unknown>
  change_policy: Record<string, string>
  offline_ready: boolean
  requirements: Array<Record<string, unknown>>
  matched_mappings: Array<Record<string, unknown>>
  missing_mappings: Array<Record<string, unknown>>
  affected_profiles: Array<Record<string, unknown>>
  affected_plans: Array<Record<string, unknown>>
  affected_artifacts: Array<Record<string, unknown>>
  blockers: Array<Record<string, unknown>>
}

export interface OfflineResourceSyncChange {
  change_id: string
  batch_id: string
  resource_type: 'collector' | 'collection_profile' | 'signal_mapping'
  resource_name: string
  change_type: 'create' | 'update' | 'disable' | 'noop'
  status: 'candidate' | 'published' | 'failed' | 'rolled_back' | 'skipped'
  source_kbd_ids: number[]
  source_kbd_revisions: Array<Record<string, unknown>>
  source_tool_revisions: Array<Record<string, unknown>>
  before_revision: number | null
  after_revision: number | null
  before_governance_json: Record<string, unknown>
  candidate_governance_json: Record<string, unknown>
  before_json: Record<string, unknown> | null
  candidate_json: Record<string, unknown> | null
  after_json: Record<string, unknown> | null
  validation_json: Array<Record<string, unknown>>
  trace_id: string
  created_at: string
  updated_at: string
}

export interface OfflineResourceSyncEvent {
  event_id: string
  batch_id: string
  event_sequence: number
  action: 'preview' | 'publish' | 'reject' | 'rollback'
  result: 'started' | 'succeeded' | 'failed'
  actor_id: string
  details_json: Record<string, unknown>
  trace_id: string
  created_at: string
}

export interface OfflineResourceSyncBatch {
  batch_id: string
  base_cursor: number
  target_cursor: number
  base_tool_cursor: number
  target_tool_cursor: number
  sync_mode: 'incremental' | 'full'
  status: 'candidate' | 'published' | 'rejected' | 'failed' | 'rolled_back' | 'rollback_failed' | 'superseded'
  requested_by: string
  approved_by: string | null
  rollback_by: string | null
  approval_reason: string | null
  rollback_reason: string | null
  kbd_change_count: number
  tool_change_count: number
  collector_change_count: number
  profile_change_count: number
  mapping_change_count: number
  summary_json: Record<string, unknown>
  validation_json: Array<Record<string, unknown>>
  error_json: Record<string, unknown>
  trace_id: string
  created_at: string
  published_at: string | null
  rolled_back_at: string | null
  updated_at: string
  changes: OfflineResourceSyncChange[]
  events: OfflineResourceSyncEvent[]
}

export interface OfflineResourceSyncHistory {
  items: OfflineResourceSyncBatch[]
  total: number
  offset: number
  limit: number
}

export interface DiagnosisRun {
  run_id: string
  session_id: string
  assessment_id: string
  run_sequence: number
  status: string
  selected_category: string | null
  resolved_category: string | null
  run_manifest: Record<string, unknown>
  conclusion_policy_version: string
  matcher_version: string
  agent_version: string
  trace_id: string
  created_at: string
  completed_at: string | null
}

export interface SignalEvaluation {
  evaluation_id: string
  run_id: string
  signal_id: string
  state: 'MATCHED' | 'NOT_MATCHED' | 'UNKNOWN'
  reason: string
  required_for_conclusion: boolean
  evidence_status: string
  evidence_refs: unknown[]
  matcher_snapshot: Record<string, unknown> | null
  trace_id: string
  created_at: string
}

export interface DiagnosisCandidate {
  candidate_id: string
  run_id: string
  kbd_id: number | null
  support_id: string | null
  title: string
  category_id: string | null
  score: number
  matched_count: number
  not_matched_count: number
  unknown_count: number
  signal_coverage: number
  kbd_snapshot: Record<string, unknown>
  trace_id: string
  created_at: string
}

export interface DiagnosisReport {
  report_id: string
  session_id: string
  run_id: string
  report_sequence: number
  diagnosis_level: 'Confirmed' | 'Probable' | 'Suspected' | 'Insufficient' | 'Conflicted'
  summary: string
  resolved_domain: string | null
  primary_hypothesis: string | null
  confidence: number
  supporting_evidence: Array<Record<string, unknown>>
  counter_evidence: Array<Record<string, unknown>>
  excluded_causes: Array<Record<string, unknown>>
  missing_evidence: Array<Record<string, unknown>>
  recommended_recovery: Array<Record<string, unknown>>
  risk_and_rollback: Array<Record<string, unknown>>
  root_cause_validation: Array<Record<string, unknown>>
  supplement_plan_id: string | null
  matched_kbds: Array<Record<string, unknown>>
  publish_status: string
  conclusion_policy_version: string
  report_schema_version: string
  version: number
  trace_id: string
  created_at: string
  updated_at: string
}

export interface DiagnosisTimelineEvent {
  event_type: string
  event_id: string
  status: string | null
  occurred_at: string
  trace_id: string | null
  details: Record<string, unknown>
}

export interface LegalHold {
  session_id: string
  legal_hold: boolean
  affected_bundle_ids: string[]
  latest_action: string | null
  latest_actor_id: string | null
  latest_reason: string | null
  updated_at: string | null
}

export interface DeletionJob {
  deletion_id: string
  session_id: string
  status: string
  deletion_results: Record<string, unknown>
  failure_message: string | null
  trace_id: string
  created_at: string
  updated_at: string
}

export type CollectorPlatform = 'linux' | 'hci_api' | 'manual'
export type CollectorExecutor = 'shell' | 'http' | 'manual'
export type CollectorReviewStatus = 'draft' | 'approved' | 'rejected'

export interface CollectorOutputContract {
  schema_id: string
  media_type: string
  output_path: string
}

export interface CollectorDefinitionWrite {
  collector_id: string
  display_name: string
  description: string
  platform: CollectorPlatform
  executor: CollectorExecutor
  command_template: string
  parameter_schema: Record<string, unknown>
  risk_level: 'read_only'
  timeout_seconds: number
  max_output_mb: number
  supported_product_versions: string[]
  output_contract: CollectorOutputContract
  version: string
  managed_by?: 'manual' | 'kbd_sync'
  generation_metadata?: Record<string, unknown>
}

export interface CollectorDefinition extends CollectorDefinitionWrite {
  review_status: CollectorReviewStatus
  is_enabled: boolean
  approved_by: string | null
  approved_at: string | null
  rejection_reason: string | null
  lock_version: number
  active_revision: number | null
  active_checksum: string | null
}

export type OfflineSignalQueryType = 'log' | 'json' | 'command_output' | 'metric' | 'evidence_status'

export interface OfflineSignalMappingWrite {
  source_kbd_id: number
  source_kbd_revision: number
  source_signal_id: string
  execution_contract_checksum: string
  acquire_tool: string
  category_scope: string
  command_scope: string
  collector_id: string
  query_type: OfflineSignalQueryType
  field_mapping: Record<string, string>
  priority: number
  is_enabled: boolean
}

export interface OfflineSignalMapping extends OfflineSignalMappingWrite {
  mapping_id: string
  lock_version: number
  trace_id: string
  created_at: string
  updated_at: string
}
