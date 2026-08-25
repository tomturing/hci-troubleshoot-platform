<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Delete, EditPen, Plus, Refresh, Upload, Switch, CopyDocument } from '@element-plus/icons-vue'

type BundleStatus = 'draft' | 'validated' | 'approved' | 'published' | 'stale' | 'retired'

interface Approval {
  actor_id: string
  role: 'compiler' | 'expert' | 'security' | 'publisher'
  at: string
}

interface RouteResult {
  exit_code: number
  stdout: string
  stderr: string
}

interface ManifestRoute {
  id: string
  signal_id: string
  variant: string
  route_key: { tool: string; argv: string[]; node: string; container: string }
  result: RouteResult
  fault: { type: string; delay_ms?: number; max_bytes?: number }
}

interface BundleManifest {
  schema_version: string
  bundle: { digest: string; status: string }
  kbd: { support_id: string; revision: number; checksum: string }
  contracts: { tool_revision: string; policy_revision: string }
  variables: Record<string, string>
  routes: ManifestRoute[]
  [key: string]: unknown
}

interface BundleRecord {
  digest: string
  status: BundleStatus
  input_fingerprint: string
  support_id: string
  kbd_revision: number
  kbd_checksum: string
  signals_digest: string
  tool_contract_revision: string
  policy_revision: string
  compiler_revision: string
  parent_bundle_digest?: string
  draft_revision: number
  edit_reason?: string
  creator: string
  created_at: string
  updated_at: string
  stale_reason?: string
  approvals: Approval[]
  manifest: BundleManifest
}

const endpoint = (import.meta.env.VITE_HCI_SIM_CONTROL_PLANE_URL || '/api/hci-sim').replace(/\/$/, '')
const loading = ref(false)
const actionLoading = ref(false)
const supportFilter = ref('')
const createSupportId = ref('')
const bundles = ref<BundleRecord[]>([])
const selected = ref<BundleRecord | null>(null)
const activeTab = ref('routes')
const editVisible = ref(false)
const editManifest = ref<BundleManifest | null>(null)
const editReason = ref('')
const variablesJSON = ref('{}')
const activationStatus = ref('not_requested')
const activationDigest = ref('')

const lifecycleStep: Partial<Record<BundleStatus, number>> = { draft: 0, validated: 1, approved: 2, published: 4 }
const currentStep = computed(() => selected.value ? lifecycleStep[selected.value.status] ?? 0 : 0)
const expertApproved = computed(() => selected.value?.approvals?.some((item) => item.role === 'expert') ?? false)
const securityApproved = computed(() => selected.value?.approvals?.some((item) => item.role === 'security') ?? false)
const canRevise = computed(() => selected.value?.status === 'draft' || selected.value?.status === 'validated' || selected.value?.status === 'published')
const canRetire = computed(() => selected.value?.status === 'draft' || selected.value?.status === 'stale')
const shortDigest = (digest: string) => digest ? (digest.length > 24 ? `${digest.slice(0, 15)}…${digest.slice(-8)}` : digest) : '-'

function statusType(status: BundleStatus) {
  return ({ draft: 'info', validated: 'warning', approved: 'success', published: 'success', stale: 'danger', retired: 'info' } as const)[status]
}

async function copyToClipboard(text: string, label = '内容') {
  if (!text) return
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      document.body.removeChild(textarea)
    }
    ElMessage.success({ message: `已复制 ${label}`, duration: 1500 })
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

function responseDetail(body: unknown, fallback: string) {
  if (body && typeof body === 'object' && typeof (body as Record<string, unknown>).detail === 'string') {
    return String((body as Record<string, unknown>).detail)
  }
  return fallback
}

async function request(path: string, init?: RequestInit): Promise<Record<string, unknown>> {
  const response = await fetch(`${endpoint}${path}`, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(responseDetail(body, `控制面 HTTP ${response.status}`))
  return body as Record<string, unknown>
}

async function loadBundles(selectDigest?: string) {
  loading.value = true
  try {
    const query = supportFilter.value.trim() ? `?support_id=${encodeURIComponent(supportFilter.value.trim())}` : ''
    const body = await request(`/v1/control-plane/bundles${query}`)
    bundles.value = (body.bundles || []) as BundleRecord[]
    const digest = selectDigest || selected.value?.digest
    if (digest) {
      const next = bundles.value.find((item) => item.digest === digest)
      if (next) await selectBundle(next)
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    loading.value = false
  }
}

async function selectBundle(bundle: BundleRecord) {
  loading.value = true
  try {
    const body = await request(`/v1/control-plane/bundles/${encodeURIComponent(bundle.digest)}`)
    selected.value = body.bundle as BundleRecord
    activationStatus.value = selected.value.status === 'published' ? 'loading' : 'not_requested'
    activationDigest.value = ''
    if (selected.value.status === 'published') {
      try {
        const activation = await request(`/v1/control-plane/activations/${encodeURIComponent(selected.value.support_id)}`)
        const runtime = activation.runtime_activation as Record<string, unknown> | undefined
        activationStatus.value = String(runtime?.status || runtime?.Status || 'unknown')
        activationDigest.value = String(runtime?.active_digest || runtime?.ActiveDigest || '')
      } catch {
        activationStatus.value = 'unknown'
      }
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    loading.value = false
  }
}

async function createDraft() {
  const supportId = createSupportId.value.trim()
  if (!/^\d{1,20}$/.test(supportId)) return ElMessage.warning('请输入有效 KBD support_id')
  actionLoading.value = true
  try {
    const body = await request('/v1/control-plane/bundles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': `bundle-factory-${supportId}` },
      body: JSON.stringify({ support_id: supportId }),
    })
    const bundle = body.bundle as BundleRecord
    supportFilter.value = supportId
    createSupportId.value = ''
    await loadBundles(bundle.digest)
    ElMessage.success('Draft 已生成')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    actionLoading.value = false
  }
}

function openEditor() {
  if (!selected.value?.manifest || !canRevise.value) return
  editManifest.value = JSON.parse(JSON.stringify(selected.value.manifest)) as BundleManifest
  variablesJSON.value = JSON.stringify(editManifest.value.variables || {}, null, 2)
  editReason.value = ''
  editVisible.value = true
}

async function saveRevision() {
  if (!selected.value || !editManifest.value || !editReason.value.trim()) return ElMessage.warning('请填写修改原因')
  let variables: Record<string, string>
  try {
    variables = JSON.parse(variablesJSON.value) as Record<string, string>
    if (!variables || Array.isArray(variables) || typeof variables !== 'object') throw new Error()
    if (Object.values(variables).some((value) => typeof value !== 'string')) throw new Error()
  } catch {
    return ElMessage.error('Variables 必须是 string→string 的 JSON 对象')
  }
  editManifest.value.variables = variables
  actionLoading.value = true
  try {
    const body = await request(`/v1/control-plane/bundles/${encodeURIComponent(selected.value.digest)}/revise`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ manifest: editManifest.value, reason: editReason.value.trim() }),
    })
    const revised = body.bundle as BundleRecord
    editVisible.value = false
    await loadBundles(revised.digest)
    ElMessage.success('已生成新的不可变 Draft revision')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    actionLoading.value = false
  }
}

async function transition(action: 'validate' | 'approve-expert' | 'approve-security' | 'publish') {
  if (!selected.value) return
  const labels = { validate: '执行编译门禁', 'approve-expert': '提交专家审批', 'approve-security': '提交安全审批', publish: '发布 Bundle' }
  try {
    await ElMessageBox.confirm(`${labels[action]}：${shortDigest(selected.value.digest)}`, '确认操作', { type: action === 'publish' ? 'warning' : 'info' })
  } catch {
    return
  }
  actionLoading.value = true
  try {
    const body = await request(`/v1/control-plane/bundles/${encodeURIComponent(selected.value.digest)}/${action}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    const updated = body.bundle as BundleRecord
    if (typeof body.runtime_activation === 'string') activationStatus.value = body.runtime_activation
    await loadBundles(updated.digest)
    ElMessage.success(`${labels[action]}完成`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    actionLoading.value = false
  }
}

async function fastPublish() {
  if (!selected.value || !['draft', 'validated'].includes(selected.value.status)) return
  try {
    await ElMessageBox.confirm(`自动校验并发布：${shortDigest(selected.value.digest)}`, 'Internal Fast Path', { type: 'warning' })
  } catch {
    return
  }
  actionLoading.value = true
  try {
    const body = await request(`/v1/control-plane/bundles/${encodeURIComponent(selected.value.digest)}/fast-publish`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    const updated = body.bundle as BundleRecord
    const activation = body.runtime_activation as Record<string, unknown> | undefined
    activationStatus.value = String(activation?.status || 'pending')
    activationDigest.value = String(activation?.digest || activation?.bundle_digest || '')
    await loadBundles(updated.digest)
    ElMessage.success(activationStatus.value === 'active' ? '已发布并热激活' : '已发布，等待 Runtime 激活')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    actionLoading.value = false
  }
}

async function activateSelected() {
  if (!selected.value?.digest || selected.value.status !== 'published') return
  actionLoading.value = true
  try {
    const body = await request(`/v1/control-plane/activations/${encodeURIComponent(selected.value.support_id)}/activate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bundle_digest: selected.value.digest }),
    })
    const activation = body.runtime_activation as Record<string, unknown> | undefined
    activationStatus.value = String(activation?.status || 'unknown')
    activationDigest.value = String(activation?.digest || activation?.bundle_digest || '')
    ElMessage.success('Runtime 已切换到所选 Bundle')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    actionLoading.value = false
  }
}

async function retireSelected() {
  if (!selected.value || !canRetire.value) return
  try {
    await ElMessageBox.confirm(
      `确认删除（归档）Bundle：${shortDigest(selected.value.digest)}？\n\n此操作只会从默认列表隐藏该 Draft 或 Stale Bundle；不可变对象、审批和既有测试记录将被保留。`,
      '删除 Bundle（归档）',
      { confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  actionLoading.value = true
  try {
    await request(`/v1/control-plane/bundles/${encodeURIComponent(selected.value.digest)}/retire`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    selected.value = null
    await loadBundles()
    ElMessage.success('Bundle 已删除（归档）')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : String(error))
  } finally {
    actionLoading.value = false
  }
}

onMounted(() => loadBundles())
</script>

<template>
  <main class="factory-page" v-loading="loading">
    <header class="page-header">
      <div class="header-left">
        <h2>Bundle 工厂</h2>
        <div class="header-meta">
          <el-tag type="warning" effect="plain" size="small">四扫：契约门禁</el-tag>
          <el-tag type="info" effect="plain" size="small">身份：配置化服务主体</el-tag>
        </div>
      </div>
      <div class="create-bar">
        <el-input
          v-model="createSupportId"
          placeholder="输入 KBD support_id (如 27123)"
          maxlength="20"
          clearable
          @keyup.enter="createDraft"
        />
        <el-button type="primary" :icon="Plus" :loading="actionLoading" @click="createDraft">生成 Draft</el-button>
      </div>
    </header>

    <div class="factory-layout">
      <!-- 左侧精简紧凑列表 -->
      <aside class="bundle-list">
        <div class="toolbar">
          <el-input
            v-model="supportFilter"
            clearable
            placeholder="筛选 support_id"
            @keyup.enter="loadBundles()"
            @clear="loadBundles()"
          />
          <el-tooltip content="刷新 Bundle 列表" placement="top">
            <el-button :icon="Refresh" circle aria-label="刷新" @click="loadBundles()" />
          </el-tooltip>
        </div>
        <el-table
          :data="bundles"
          height="calc(100vh - 212px)"
          highlight-current-row
          @row-click="selectBundle"
          class="bundle-table"
        >
          <el-table-column label="KBD" width="76">
            <template #default="{ row }">
              <div class="kbd-cell">
                <span class="kbd-id">{{ row.support_id }}</span>
                <span class="kbd-rev">rev {{ row.kbd_revision }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="78" align="center">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" size="small" effect="light" class="status-tag">
                {{ row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="Digest" min-width="110">
            <template #default="{ row }">
              <div class="digest-cell" :title="row.digest">
                <code class="digest-code">{{ shortDigest(row.digest) }}</code>
                <div class="draft-sub">Draft r{{ row.draft_revision || 0 }}</div>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </aside>

      <!-- 右侧宽屏重点展示区 -->
      <section v-if="selected" class="bundle-detail">
        <div class="detail-header">
          <div class="header-main-info">
            <div class="title-line">
              <h3 class="kbd-title">KBD {{ selected.support_id }}</h3>
              <el-tag :type="statusType(selected.status)" size="small" effect="dark" class="header-status-tag">
                {{ selected.status }}
              </el-tag>
              <span class="header-draft-pill">Draft r{{ selected.draft_revision || 0 }}</span>
            </div>
            
            <!-- 精致的 Digest 芯片卡片，支持悬浮提示与一键复制 -->
            <div
              class="digest-chip"
              @click="copyToClipboard(selected.digest, 'Bundle Digest')"
              title="点击复制完整 Digest"
            >
              <span class="digest-chip-label">DIGEST</span>
              <el-tooltip :content="selected.digest" placement="bottom" :show-after="300">
                <code class="digest-chip-value">{{ selected.digest }}</code>
              </el-tooltip>
              <el-icon class="copy-icon"><CopyDocument /></el-icon>
            </div>
          </div>

          <div class="actions">
            <el-button v-if="canRevise" :icon="EditPen" @click="openEditor">
              {{ selected.status === 'published' ? '基于此版本创建 Draft' : '编辑 Draft' }}
            </el-button>
            <el-button v-if="canRetire" type="danger" :icon="Delete" :loading="actionLoading" @click="retireSelected">
              删除（归档）
            </el-button>
            <el-button
              v-if="selected.status === 'draft' || selected.status === 'validated'"
              type="success"
              :icon="Upload"
              :loading="actionLoading"
              @click="fastPublish"
            >
              自动校验并发布
            </el-button>
            <el-button
              v-if="selected.status === 'draft'"
              type="primary"
              :icon="Check"
              :loading="actionLoading"
              @click="transition('validate')"
            >
              校验
            </el-button>
            <template v-if="selected.status === 'validated'">
              <el-button type="primary" :disabled="expertApproved" @click="transition('approve-expert')">专家审批</el-button>
              <el-button type="warning" :disabled="securityApproved" @click="transition('approve-security')">安全审批</el-button>
            </template>
            <el-button v-if="selected.status === 'approved'" type="success" :icon="Upload" @click="transition('publish')">发布</el-button>
          </div>
        </div>

        <div class="lifecycle-wrapper">
          <el-steps :active="currentStep" finish-status="success" align-center class="lifecycle">
            <el-step title="Draft" />
            <el-step title="已校验" />
            <el-step title="双审完成" />
            <el-step title="已发布" />
          </el-steps>
        </div>

        <el-alert
          v-if="selected.status === 'published'"
          :closable="false"
          :type="activationStatus === 'active' ? 'success' : 'warning'"
          show-icon
          class="activation-alert"
        >
          <template #title>
            <div class="activation-title">
              <span>Runtime 激活：<strong>{{ activationStatus }}</strong></span>
              <span v-if="activationDigest" class="activation-digest">
                · <code>{{ shortDigest(activationDigest) }}</code>
              </span>
            </div>
          </template>
          <template #default>
            <div class="activation-action">
              <el-button
                v-if="activationStatus !== 'active' || activationDigest !== selected.digest"
                size="small"
                type="primary"
                plain
                :icon="Switch"
                @click="activateSelected"
              >
                切换到此 Bundle
              </el-button>
            </div>
          </template>
        </el-alert>
        <el-alert v-else-if="selected.stale_reason" :closable="false" type="error" show-icon :title="selected.stale_reason" class="activation-alert" />

        <el-descriptions :column="3" border size="small" class="facts">
          <el-descriptions-item label="KBD revision">
            <span class="fact-num">{{ selected.kbd_revision }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="Input fingerprint">
            <div class="fact-hash" @click="copyToClipboard(selected.input_fingerprint, 'Input fingerprint')" title="点击复制">
              <code>{{ shortDigest(selected.input_fingerprint) }}</code>
              <el-icon class="fact-copy"><CopyDocument /></el-icon>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="创建者">
            <span class="fact-creator">{{ selected.creator }}</span>
          </el-descriptions-item>

          <el-descriptions-item label="Draft revision">
            <span class="fact-num">{{ selected.draft_revision || 0 }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="KBD checksum">
            <div class="fact-hash" @click="copyToClipboard(selected.kbd_checksum, 'KBD checksum')" title="点击复制">
              <code>{{ shortDigest(selected.kbd_checksum) }}</code>
              <el-icon class="fact-copy"><CopyDocument /></el-icon>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="Tool contract">
            <div class="fact-hash" @click="copyToClipboard(selected.tool_contract_revision, 'Tool contract')" title="点击复制">
              <code>{{ shortDigest(selected.tool_contract_revision) }}</code>
              <el-icon class="fact-copy"><CopyDocument /></el-icon>
            </div>
          </el-descriptions-item>

          <el-descriptions-item label="Routes">
            <span class="routes-count">{{ selected.manifest?.routes?.length || 0 }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="Signals digest">
            <div class="fact-hash" @click="copyToClipboard(selected.signals_digest, 'Signals digest')" title="点击复制">
              <code>{{ shortDigest(selected.signals_digest) }}</code>
              <el-icon class="fact-copy"><CopyDocument /></el-icon>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="Policy">
            <div class="fact-hash" @click="copyToClipboard(selected.policy_revision, 'Policy')" title="点击复制">
              <code>{{ shortDigest(selected.policy_revision) }}</code>
              <el-icon class="fact-copy"><CopyDocument /></el-icon>
            </div>
          </el-descriptions-item>
        </el-descriptions>

        <el-tabs v-model="activeTab" class="detail-tabs">
          <el-tab-pane label="仿真路由" name="routes">
            <el-table :data="selected.manifest?.routes || []" row-key="id" border stripe class="routes-table">
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="output-grid">
                    <div class="console-box stdout-box">
                      <div class="console-header">
                        <span class="console-label stdout-label">STDOUT</span>
                        <el-button
                          v-if="row.result.stdout"
                          size="small"
                          link
                          :icon="CopyDocument"
                          @click.stop="copyToClipboard(row.result.stdout, 'stdout')"
                        >
                          复制
                        </el-button>
                      </div>
                      <pre class="console-body">{{ row.result.stdout || '∅' }}</pre>
                    </div>
                    <div class="console-box stderr-box">
                      <div class="console-header">
                        <span class="console-label stderr-label">STDERR</span>
                        <el-button
                          v-if="row.result.stderr"
                          size="small"
                          link
                          :icon="CopyDocument"
                          @click.stop="copyToClipboard(row.result.stderr, 'stderr')"
                        >
                          复制
                        </el-button>
                      </div>
                      <pre class="console-body" :class="{ 'has-error': !!row.result.stderr }">{{ row.result.stderr || '∅' }}</pre>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="signal_id" label="Signal" min-width="120" />
              <el-table-column prop="variant" label="Variant" min-width="130" />
              <el-table-column label="命令" min-width="260">
                <template #default="{ row }">
                  <code class="cmd-text">{{ row.route_key.argv.join(' ') }}</code>
                </template>
              </el-table-column>
              <el-table-column label="目标" min-width="140">
                <template #default="{ row }">
                  <span class="target-text">{{ row.route_key.node }} / {{ row.route_key.container }}</span>
                </template>
              </el-table-column>
              <el-table-column label="结果" width="100" align="center">
                <template #default="{ row }">
                  <el-tag :type="row.result.exit_code === 0 ? 'success' : 'danger'" size="small">
                    exit {{ row.result.exit_code }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="Fault" width="110" align="center">
                <template #default="{ row }">
                  <el-tag v-if="row.fault?.type && row.fault.type !== 'none'" type="warning" size="small">
                    {{ row.fault.type }}
                  </el-tag>
                  <span v-else class="text-muted">none</span>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="审批与修订" name="audit">
            <div class="audit-wrapper">
              <el-timeline>
                <el-timeline-item :timestamp="selected.created_at" type="primary">
                  <strong>{{ selected.creator }}</strong> 创建 Draft r{{ selected.draft_revision || 0 }}
                </el-timeline-item>
                <el-timeline-item v-if="selected.parent_bundle_digest" type="warning">
                  父版本 <code class="digest-code">{{ selected.parent_bundle_digest }}</code>
                  <div class="edit-reason-box">{{ selected.edit_reason }}</div>
                </el-timeline-item>
                <el-timeline-item
                  v-for="approval in selected.approvals"
                  :key="`${approval.role}-${approval.actor_id}`"
                  :timestamp="approval.at"
                  type="success"
                >
                  <el-tag size="small" type="success" effect="plain">{{ approval.role }}</el-tag>
                  <span class="approval-actor">{{ approval.actor_id }}</span>
                </el-timeline-item>
              </el-timeline>
            </div>
          </el-tab-pane>
          <el-tab-pane label="Variables" name="variables">
            <pre class="json-view">{{ JSON.stringify(selected.manifest?.variables || {}, null, 2) }}</pre>
          </el-tab-pane>
          <el-tab-pane label="Manifest" name="manifest">
            <pre class="json-view">{{ JSON.stringify(selected.manifest, null, 2) }}</pre>
          </el-tab-pane>
        </el-tabs>
      </section>

      <section v-else class="empty-detail">
        <el-empty description="选择左侧 Bundle 或生成新的 Draft" />
      </section>
    </div>

    <!-- 修订对话框 -->
    <el-dialog v-model="editVisible" title="修订 Draft" class="bundle-editor-dialog" top="3vh" :close-on-click-modal="false">
      <div v-if="editManifest" class="editor-body">
        <el-form label-width="110px">
          <el-form-item label="修改原因" required>
            <el-input
              v-model="editReason"
              maxlength="500"
              show-word-limit
              placeholder="说明证据或仿真设定的修正依据"
            />
          </el-form-item>
          <el-form-item label="Variables">
            <el-input v-model="variablesJSON" type="textarea" :rows="5" class="mono-input" />
          </el-form-item>
        </el-form>
        <section class="route-editor-list" aria-label="Route 返回结果编辑">
          <article v-for="row in editManifest.routes" :key="row.id" class="route-editor">
            <div class="route-editor-heading">
              <div><span class="field-label">Signal</span><strong>{{ row.signal_id }}</strong></div>
              <div><span class="field-label">Variant</span><span>{{ row.variant }}</span></div>
              <div><span class="field-label">目标</span><span>{{ row.route_key.node }} / {{ row.route_key.container }}</span></div>
            </div>
            <div class="route-command">
              <span class="field-label">命令参数（可编辑，每行一个）</span>
              <el-input
                :model-value="row.route_key.argv.join('\n')"
                type="textarea"
                :rows="Math.max(2, row.route_key.argv.length)"
                class="mono-input"
                resize="vertical"
                @update:model-value="(val: string) => (row.route_key.argv = val.split('\n').map((t: string) => t.trim()).filter((t: string) => t.length > 0))"
              />
              <code class="route-command-preview">{{ row.route_key.argv.join(' ') }}</code>
              <el-button
                size="small"
                text
                type="primary"
                class="route-command-reset"
                @click="row.route_key.argv = (selected.manifest?.routes.find((r: ManifestRoute) => r.id === row.id)?.route_key.argv || [])"
              >还原</el-button>
            </div>
            <div class="route-response-grid">
              <label><span class="field-label">stdout</span><el-input v-model="row.result.stdout" type="textarea" :rows="4" resize="vertical" class="mono-input" /></label>
              <label><span class="field-label">stderr</span><el-input v-model="row.result.stderr" type="textarea" :rows="4" resize="vertical" class="mono-input" /></label>
            </div>
            <div class="route-controls">
              <label><span class="field-label">Exit</span><el-input-number v-model="row.result.exit_code" :min="0" :max="255" controls-position="right" /></label>
              <label><span class="field-label">Fault</span><el-select v-model="row.fault.type"><el-option v-for="fault in ['none','timeout','permission','nonzero_exit','truncate','disconnect']" :key="fault" :label="fault" :value="fault" /></el-select></label>
            </div>
          </article>
        </section>
      </div>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="actionLoading" @click="saveRevision">生成新 Draft</el-button>
      </template>
    </el-dialog>
  </main>
</template>

<style scoped>
.factory-page {
  min-width: 0;
  color: #303133;
}

.page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.header-left h2 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 600;
  color: #1f2937;
  letter-spacing: -0.02em;
}

.header-meta,
.toolbar,
.title-line,
.actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.create-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  width: min(440px, 45vw);
}

/* 布局：左侧固定紧凑 280px，右侧自适应撑满 */
.factory-layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  min-height: calc(100vh - 148px);
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #ffffff;
  overflow: hidden;
  box-shadow: 0 1px 4px 0 rgba(0, 0, 0, 0.04);
}

/* 左侧列表 */
.bundle-list {
  min-width: 0;
  background-color: #fafbfc;
  border-right: 1px solid #e4e7ed;
}

.toolbar {
  padding: 10px;
  background: #ffffff;
  border-bottom: 1px solid #ebeef5;
}

.bundle-table {
  background: transparent;
}

.bundle-table :deep(.el-table__row) {
  cursor: pointer;
}

.bundle-table :deep(.el-table__body-wrapper .el-table__cell) {
  padding: 6px 0;
}

.kbd-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding-left: 6px;
}

.kbd-id {
  font-size: 13px;
  font-weight: 700;
  color: #1e293b;
  line-height: 1.2;
}

.kbd-rev {
  font-size: 11px;
  color: #94a3b8;
  line-height: 1.2;
  margin-top: 2px;
}

.status-tag {
  font-size: 11px;
  padding: 0 6px;
  height: 22px;
  line-height: 20px;
}

.digest-cell {
  min-width: 0;
  overflow: hidden;
}

.digest-code {
  font-size: 11.5px;
  color: #475569;
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.draft-sub {
  font-size: 11px;
  color: #94a3b8;
  margin-top: 2px;
}

/* 右侧详情区 */
.bundle-detail {
  min-width: 0;
  padding: 16px 20px;
  overflow-y: auto;
  background: #ffffff;
}

.detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px 16px;
}

.header-main-info {
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1 1 300px;
  min-width: 0;
}

.kbd-title {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  color: #0f172a;
}

.header-status-tag {
  font-weight: 600;
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.04em;
}

.header-draft-pill {
  font-size: 12px;
  color: #64748b;
  background: #f1f5f9;
  padding: 2px 8px;
  border-radius: 12px;
  font-weight: 500;
}

/* 优化后的 Digest 芯片 */
.digest-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
  max-width: 100%;
}

.digest-chip:hover {
  background: #eff6ff;
  border-color: #bfdbfe;
}

.digest-chip:hover .copy-icon {
  color: #2563eb;
}

.digest-chip-label {
  font-size: 10px;
  font-weight: 700;
  color: #64748b;
  background: #e2e8f0;
  padding: 1px 5px;
  border-radius: 3px;
  letter-spacing: 0.05em;
}

.digest-chip-value {
  font-size: 12px;
  color: #334155;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.copy-icon {
  font-size: 13px;
  color: #94a3b8;
  flex-shrink: 0;
  transition: color 0.15s ease;
}

.actions {
  flex: 0 1 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.actions :deep(.el-button) {
  margin-left: 0;
}

/* 流程图包裹层 */
.lifecycle-wrapper {
  margin: 16px 0;
  padding: 12px 16px;
  background: #f8fafc;
  border: 1px solid #f1f5f9;
  border-radius: 8px;
}

.lifecycle {
  margin: 0;
}

.activation-alert {
  margin-bottom: 14px;
}

.activation-title {
  display: flex;
  align-items: center;
  gap: 6px;
}

.activation-digest code {
  color: #475569;
}

/* 事实卡片 */
.facts {
  margin-top: 12px;
}

.facts :deep(table) {
  table-layout: fixed;
  width: 100%;
}

.facts :deep(.el-descriptions__cell) {
  min-width: 0;
  word-break: break-all;
  padding: 8px 12px;
}

.fact-num {
  font-weight: 600;
  color: #1e293b;
}

.fact-creator {
  color: #334155;
}

.routes-count {
  font-weight: 600;
  color: #2563eb;
}

.fact-hash {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: #475569;
  transition: color 0.15s ease;
}

.fact-hash:hover {
  color: #2563eb;
}

.fact-hash:hover .fact-copy {
  color: #2563eb;
}

.fact-copy {
  font-size: 12px;
  color: #94a3b8;
}

/* Tabs 与 表格 */
.detail-tabs {
  margin-top: 14px;
}

.routes-table :deep(th.el-table__cell) {
  background-color: #f8fafc;
  color: #475569;
  font-weight: 600;
}

.cmd-text {
  font-size: 12px;
  color: #0f172a;
  background: #f1f5f9;
  padding: 2px 6px;
  border-radius: 4px;
}

.target-text {
  font-size: 12px;
  color: #475569;
}

.text-muted {
  color: #94a3b8;
  font-size: 12px;
}

/* 控制台展开行 */
.output-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 6px 14px 12px;
  background: #f8fafc;
  border-radius: 6px;
}

.console-box {
  display: flex;
  flex-direction: column;
  border-radius: 6px;
  border: 1px solid #e2e8f0;
  overflow: hidden;
  background: #ffffff;
}

.console-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  background: #f1f5f9;
  border-bottom: 1px solid #e2e8f0;
}

.console-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
}

.stdout-label {
  color: #16a34a;
}

.stderr-label {
  color: #dc2626;
}

.console-body {
  margin: 0;
  padding: 10px;
  font-size: 12px;
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #0f172a;
  color: #e2e8f0;
}

.console-body.has-error {
  color: #fca5a5;
}

.json-view {
  max-height: 440px;
  margin: 0;
  padding: 12px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  background: #0f172a;
  color: #e2e8f0;
  font-size: 12.5px;
}

.audit-wrapper {
  padding: 12px 6px;
}

.edit-reason-box {
  margin-top: 4px;
  padding: 6px 10px;
  background: #fef3c7;
  color: #92400e;
  border-radius: 4px;
  font-size: 12px;
}

.approval-actor {
  margin-left: 8px;
  color: #475569;
  font-size: 12px;
}

.empty-detail {
  display: grid;
  place-items: center;
  min-height: 480px;
}

.editor-body {
  min-width: 0;
}

.route-editor-list {
  display: grid;
  gap: 12px;
  max-height: min(52vh, 620px);
  overflow-y: auto;
  padding-right: 4px;
}

.route-editor {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  background: #fff;
}

.route-editor-heading {
  display: grid;
  grid-template-columns: minmax(150px, 1fr) minmax(150px, 1fr) minmax(190px, 1.3fr);
  gap: 12px;
}

.route-editor-heading > div,
.route-command,
.route-response-grid > label,
.route-controls > label {
  display: grid;
  min-width: 0;
  gap: 5px;
}

.field-label {
  color: #909399;
  font-size: 12px;
  font-weight: 600;
}

.route-command {
  display: block;
  padding: 10px 12px;
  border-radius: 4px;
  background: #f7f8fa;
}

.route-command .mono-input {
  margin: 4px 0;
}

.route-command-preview {
  display: block;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 6px 8px;
  margin-top: 4px;
  background: #1e293b;
  color: #e2e8f0;
  border-radius: 4px;
  font-size: 12px;
}

.route-command-reset {
  margin-top: 6px;
}

.route-response-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.route-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 12px;
}

.route-controls > label:first-child {
  width: 132px;
}

.route-controls > label:last-child {
  width: min(240px, 100%);
}

.route-controls :deep(.el-input-number),
.route-controls :deep(.el-select) {
  width: 100%;
}

:deep(.bundle-editor-dialog) {
  width: min(96vw, 1480px);
  margin-bottom: 3vh;
}

:deep(.bundle-editor-dialog .el-dialog__body) {
  max-height: calc(100vh - 166px);
  overflow-y: auto;
}

code,
pre,
.mono-input :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

@media (max-width: 1024px) {
  .page-header {
    align-items: stretch;
    flex-direction: column;
  }
  .create-bar {
    width: 100%;
  }
  .factory-layout {
    grid-template-columns: 1fr;
  }
  .bundle-list {
    border-right: 0;
    border-bottom: 1px solid #e4e7ed;
  }
  .bundle-list :deep(.el-table) {
    height: 280px !important;
  }
  .detail-header {
    flex-direction: column;
  }
  .actions {
    justify-content: flex-start;
  }
  .output-grid,
  .route-response-grid,
  .route-editor-heading {
    grid-template-columns: 1fr;
  }
}
</style>

