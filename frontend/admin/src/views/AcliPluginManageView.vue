<script setup lang="ts">
/**
 * acli 插件管理页。
 *
 * 这是配置管理页，不是调试面板：只维护插件规格，保存后由平台服务端自动
 * 编排下发。页面全程不出现 node_ip / SSH 口令 / 私钥 / bridge 地址 / 部署命令，
 * 也不建立任何 WebSocket 连接。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import {
  downloadTemplate,
  getDeployment,
  getTemplate,
  listDeployments,
  listSessions,
  listPlugins,
  publishPlugin,
  type AcliPluginDeployment,
  type AcliPluginSession,
  type AcliPluginItem,
  type AcliPluginSpec,
  type AcliPluginTemplateResponse,
} from '@/api/acliPlugin'

type TagType = 'success' | 'warning' | 'danger' | 'info'

function blankSpec(): AcliPluginSpec {
  return {
    update_mode: 'third_party',
    plugin_name: '',
    plugin_uuid: '',
    display_name: '',
    title_zh: '',
    title_en: '',
    description_zh: '',
    description_en: '',
    vendor_name: 'HTP',
    change_log: '',
    version: '0.0.1',
    acli_version: '>=1.0.0',
    hci_version: '',
    source: 3,
    status: 1,
    arch: 1,
    container: '',
    interpreter: 'bash',
    script_filename: 'troubleshoot.sh',
    script_content: '',
    parameters: [],
    extra_files: [],
    enable_completion: true,
    passthrough_help: false,
    enable_pid_lock: false,
    timeout_seconds: 120,
    smoke_args: '',
    source_url: '',
  }
}

const plugins = ref<AcliPluginItem[]>([])
const sessions = ref<AcliPluginSession[]>([])
const templateInfo = ref<AcliPluginTemplateResponse | null>(null)
const loading = ref(false)
const publishing = ref(false)
const formVisible = ref(false)
const progressVisible = ref(false)
const historyVisible = ref(false)
const activeTab = ref('basic')
const isEditing = ref(false)
const selectedSessionKey = ref('')
const form = ref<AcliPluginSpec>(blankSpec())
const deployments = ref<AcliPluginDeployment[]>([])
const selectedDeployment = ref<AcliPluginDeployment | null>(null)
const currentDeployment = ref<AcliPluginDeployment | null>(null)
const currentDeployId = ref('')
const idempotent = ref(false)
const historyPluginName = ref('')
let polling = false

const selectedSession = computed(() => sessions.value.find(item => item.key === selectedSessionKey.value) || null)
const smokeHint = computed(() =>
  form.value.smoke_args
    ? `设备侧将真实执行：/sf/bin/acli --timeout 60 ${form.value.plugin_name || '<插件名>'} ${form.value.smoke_args}`
    : '必须填写冒烟参数，只跑 --help 不算生效',
)

function statusTag(status?: string): TagType {
  switch (status) {
    case 'succeeded': return 'success'
    case 'failed': return 'danger'
    case 'running':
    case 'claimed':
    case 'pending': return 'warning'
    default: return 'info'
  }
}

function statusText(status?: string): string {
  const mapping: Record<string, string> = {
    never: '尚未下发',
    pending: '等待下发',
    claimed: '已领取',
    running: '下发中',
    succeeded: '已生效',
    failed: '下发失败',
  }
  return mapping[status || ''] || status || '未知'
}

function isEffective(row: AcliPluginItem): boolean {
  return Boolean(row.current_spec_hash) && row.current_spec_hash === row.desired_spec_hash
}

async function loadPlugins() {
  loading.value = true
  try {
    plugins.value = await listPlugins()
  } catch {
    ElMessage.error('加载插件列表失败')
  } finally {
    loading.value = false
  }
}

async function loadMeta() {
  try {
    const [sessionData, templateData] = await Promise.all([listSessions(), getTemplate()])
    sessions.value = sessionData
    templateInfo.value = templateData
    if (!sessions.value.some(item => item.key === selectedSessionKey.value) && sessions.value.length) {
      selectedSessionKey.value = sessions.value[0].key
    }
  } catch {
    ElMessage.warning('模板或环境列表加载失败，可稍后重试')
  }
}

function openCreate() {
  isEditing.value = false
  // 新建时保留模板字段结构、清空示例值，避免误把 lmt 示例直接下发。
  form.value = { ...blankSpec(), ...JSON.parse(JSON.stringify(templateInfo.value?.template ?? {})) }
  form.value.plugin_name = ''
  form.value.plugin_uuid = ''
  form.value.display_name = ''
  form.value.title_zh = ''
  form.value.title_en = ''
  form.value.script_content = ''
  form.value.smoke_args = ''
  form.value.change_log = ''
  form.value.parameters = []
  form.value.extra_files = []
  activeTab.value = 'basic'
  formVisible.value = true
}

function openEdit(row: AcliPluginItem) {
  isEditing.value = true
  form.value = JSON.parse(JSON.stringify(row.spec_json ?? blankSpec()))
  activeTab.value = 'basic'
  formVisible.value = true
}

function addParameter() {
  form.value.parameters = [...form.value.parameters, { flag: '', description: '', required: false, default: '' }]
}

function removeParameter(index: number) {
  form.value.parameters.splice(index, 1)
}

function addExtraFile() {
  form.value.extra_files = [...form.value.extra_files, { path: '', mode: '0644', content: '' }]
}

function removeExtraFile(index: number) {
  form.value.extra_files.splice(index, 1)
}

function validateBeforePublish(): boolean {
  const spec = form.value
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(spec.plugin_name)) {
    ElMessage.warning('插件名必须是小写字母开头的 [a-z0-9_] 标识')
    activeTab.value = 'basic'
    return false
  }
  if (!/^\d+\.\d+\.\d+/.test(spec.version)) {
    ElMessage.warning('版本号需形如 0.0.1')
    activeTab.value = 'basic'
    return false
  }
  if (!spec.script_content.trim()) {
    ElMessage.warning('请粘贴插件原始脚本')
    activeTab.value = 'script'
    return false
  }
  if (!spec.smoke_args.trim() || spec.smoke_args.trim() === '--help') {
    ElMessage.warning('请填写可真实跑通的生效冒烟参数（不能只填 --help）')
    activeTab.value = 'options'
    return false
  }
  if (spec.update_mode === 'builtin_override' && !spec.plugin_uuid.trim()) {
    ElMessage.warning('覆盖内置插件必须填写现场注册项原 id（plugin_uuid）')
    activeTab.value = 'basic'
    return false
  }
  return true
}

async function handlePublish() {
  if (!validateBeforePublish()) return
  if (form.value.update_mode === 'builtin_override') {
    try {
      await ElMessageBox.confirm(
        '将覆盖 HCI 出厂内置插件：保留 built_in / source / 原 id，强制备份原目录，冒烟失败自动回滚。请确认已填写现场注册项原 id 与变更说明。',
        '覆盖内置插件确认',
        { type: 'warning', confirmButtonText: '确认覆盖并下发', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  publishing.value = true
  try {
    if (!selectedSession.value) {
      ElMessage.warning('请先在 custom-ui 登录目标 HCI，使 terminal_bridge 存在已连接的 SSH 会话')
      return
    }
    const result = await publishPlugin(form.value, {
      caseId: selectedSession.value.case_id,
      nodeIp: selectedSession.value.node_ip,
    })
    currentDeployId.value = result.deploy_id
    idempotent.value = result.idempotent
    currentDeployment.value = null
    formVisible.value = false
    progressVisible.value = true
    await pollDeployment(result.deploy_id)
    await loadPlugins()
  } catch (error: any) {
    const errors = error?.response?.data?.detail?.errors
    ElMessage.error(Array.isArray(errors) ? errors.join('；') : '保存并下发失败')
  } finally {
    publishing.value = false
  }
}

async function pollDeployment(deployId: string) {
  polling = true
  for (let attempt = 0; attempt < 80 && polling; attempt += 1) {
    if (!progressVisible.value) break
    try {
      const deployment = await getDeployment(deployId)
      currentDeployment.value = deployment
      if (deployment.status === 'succeeded') {
        ElMessage.success('插件已在目标环境实时生效')
        polling = false
        return
      }
      if (deployment.status === 'failed') {
        ElMessage.error(`下发失败：${deployment.error_code || '未知原因'}`)
        polling = false
        return
      }
    } catch {
      // 状态接口尚未就绪时继续轮询，由后台状态机保证收敛。
    }
    await new Promise(resolve => setTimeout(resolve, 1500))
  }
  polling = false
  ElMessage.warning('仍在下发中，可在「下发历史」里继续查看')
}

async function openHistoryByName(pluginName: string) {
  historyPluginName.value = pluginName
  selectedDeployment.value = null
  historyVisible.value = true
  try {
    deployments.value = await listDeployments(pluginName)
  } catch {
    ElMessage.error('加载下发历史失败')
  }
}

async function showDeployment(row: AcliPluginDeployment) {
  selectedDeployment.value = row
  try {
    selectedDeployment.value = await getDeployment(row.deploy_id)
  } catch {
    // 列表已是最新回执，详情失败时保留列表行。
  }
}

async function handleDownloadTemplate() {
  try {
    const info = templateInfo.value ?? await getTemplate()
    downloadTemplate(info.template)
    ElMessage.success('已下载新增插件信息模板')
  } catch {
    ElMessage.error('模板下载失败')
  }
}

onMounted(() => {
  void loadPlugins()
  void loadMeta()
})

onBeforeUnmount(() => {
  polling = false
})
</script>

<template>
  <div class="acli-plugin-page">
    <div class="page-header">
      <div>
        <h2>acli 插件</h2>
        <p class="subtitle">维护 HCI acli 插件规格，保存后由平台自动下发并在目标环境实时生效</p>
      </div>
      <div class="header-actions">
        <el-button :icon="Download" @click="handleDownloadTemplate">下载信息模板</el-button>
        <el-button :icon="Refresh" @click="loadPlugins">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建插件</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" show-icon class="boundary-hint">
      <template #title>插件规格与目标环境分离</template>
      页面只维护插件本身的信息；目标节点、SSH 凭据与下发通道由平台服务端托管，不在页面填写或展示。
    </el-alert>

    <el-card shadow="never" class="table-card">
      <el-table :data="plugins" v-loading="loading" row-key="plugin_name" empty-text="暂无插件，点击「新建插件」开始">
        <el-table-column prop="plugin_name" label="插件名" min-width="180" />
        <el-table-column prop="display_name" label="显示名" min-width="150" />
        <el-table-column label="版本" width="110">
          <template #default="{ row }">{{ row.spec_json?.version || '-' }}</template>
        </el-table-column>
        <el-table-column label="类型" width="130">
          <template #default="{ row }">
            <el-tag :type="row.update_mode === 'builtin_override' ? 'warning' : 'info'" size="small">
              {{ row.update_mode === 'builtin_override' ? '内置覆盖' : '第三方新增' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="生效状态" width="120">
          <template #default="{ row }">
            <el-tag :type="isEffective(row) ? 'success' : 'warning'" size="small">
              {{ isEffective(row) ? '已生效' : '待下发' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近下发" width="130">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.last_deploy_status)" size="small">
              {{ statusText(row.last_deploy_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="last_deploy_at" label="下发时间" min-width="170">
          <template #default="{ row }">{{ row.last_deploy_at || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button link type="info" @click="openHistoryByName(row.plugin_name)">下发历史</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer
      v-model="formVisible"
      :title="isEditing ? `编辑插件 · ${form.plugin_name}` : '新建插件'"
      size="72%"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="130px" label-position="left">
        <el-tabs v-model="activeTab">
          <el-tab-pane label="基础信息" name="basic">
            <el-form-item label="插件名" required>
              <el-input v-model="form.plugin_name" placeholder="hci_lmt_check" :disabled="isEditing" />
              <div class="field-hint">小写字母开头，仅 [a-z0-9_]，创建后作为 acli 插件标识</div>
            </el-form-item>
            <el-form-item label="显示名"><el-input v-model="form.display_name" /></el-form-item>
            <el-form-item label="版本" required><el-input v-model="form.version" placeholder="0.0.1" /></el-form-item>
            <el-form-item label="更新模式">
              <el-radio-group v-model="form.update_mode">
                <el-radio-button value="third_party">新增第三方插件</el-radio-button>
                <el-radio-button value="builtin_override">覆盖内置插件</el-radio-button>
              </el-radio-group>
              <div v-if="form.update_mode === 'builtin_override'" class="field-hint warning">
                覆盖出厂插件需二次确认；平台会强制备份并在冒烟失败时回滚。
              </div>
            </el-form-item>
            <el-form-item v-if="form.update_mode === 'builtin_override'" label="现场注册项原 id" required>
              <el-input v-model="form.plugin_uuid" placeholder="从 HCI plugin_register.json 读取的原 id" />
            </el-form-item>
            <el-form-item label="中文标题"><el-input v-model="form.title_zh" /></el-form-item>
            <el-form-item label="英文标题"><el-input v-model="form.title_en" /></el-form-item>
            <el-form-item label="中文说明"><el-input v-model="form.description_zh" type="textarea" :rows="2" /></el-form-item>
            <el-form-item label="英文说明"><el-input v-model="form.description_en" type="textarea" :rows="2" /></el-form-item>
            <el-form-item label="厂商"><el-input v-model="form.vendor_name" /></el-form-item>
            <el-form-item label="变更说明"><el-input v-model="form.change_log" type="textarea" :rows="2" /></el-form-item>
            <el-form-item label="acli 版本约束"><el-input v-model="form.acli_version" /></el-form-item>
            <el-form-item label="HCI 版本约束"><el-input v-model="form.hci_version" placeholder="留空表示不限制" /></el-form-item>
            <el-form-item label="来源类型">
              <el-select v-model="form.source" :disabled="form.update_mode === 'builtin_override'">
                <el-option label="1 官方包" :value="1" />
                <el-option label="2 运维包" :value="2" />
                <el-option label="3 第三方包" :value="3" />
              </el-select>
            </el-form-item>
            <el-form-item label="资料来源"><el-input v-model="form.source_url" placeholder="使用说明链接（可选）" /></el-form-item>
          </el-tab-pane>

          <el-tab-pane label="脚本与文件" name="script">
            <el-form-item label="脚本文件名" required>
              <el-input v-model="form.script_filename" placeholder="troubleshoot.sh" />
            </el-form-item>
            <el-form-item label="解释器">
              <el-select v-model="form.interpreter">
                <el-option label="bash" value="bash" />
                <el-option label="sh" value="sh" />
              </el-select>
            </el-form-item>
            <el-form-item label="原始脚本" required>
              <el-input v-model="form.script_content" type="textarea" :rows="12" class="code-editor" spellcheck="false" />
              <div class="field-hint">HCI 定制系统无 base64 / python3；脚本只依赖 bash 内建、jq、flock、sha256sum。</div>
            </el-form-item>
            <el-divider content-position="left">附加文件（可选）</el-divider>
            <div v-for="(file, index) in form.extra_files" :key="`file-${index}`" class="dynamic-row">
              <el-input v-model="file.path" placeholder="lib/helper.sh" style="width: 34%" />
              <el-select v-model="file.mode" style="width: 16%">
                <el-option label="0644" value="0644" />
                <el-option label="0755" value="0755" />
              </el-select>
              <el-input v-model="file.content" type="textarea" :rows="2" placeholder="文件内容" style="width: 42%" />
              <el-button link type="danger" @click="removeExtraFile(index)">移除</el-button>
            </div>
            <el-button link type="primary" @click="addExtraFile">+ 添加附加文件</el-button>
          </el-tab-pane>

          <el-tab-pane label="参数与选项" name="options">
            <el-divider content-position="left">参数声明</el-divider>
            <div v-for="(param, index) in form.parameters" :key="`param-${index}`" class="dynamic-row">
              <el-input v-model="param.flag" placeholder="-m" style="width: 18%" />
              <el-input v-model="param.description" placeholder="参数说明" style="width: 42%" />
              <el-input v-model="param.default" placeholder="默认值" style="width: 20%" />
              <el-checkbox v-model="param.required">必填</el-checkbox>
              <el-button link type="danger" @click="removeParameter(index)">移除</el-button>
            </div>
            <el-button link type="primary" @click="addParameter">+ 添加参数</el-button>
            <el-divider content-position="left">生效与验证</el-divider>
            <el-form-item label="生效冒烟参数" required>
              <el-input v-model="form.smoke_args" placeholder="-m env -c cpu_load" />
              <div class="field-hint">{{ smokeHint }}</div>
            </el-form-item>
            <el-form-item label="下发超时(秒)">
              <el-input-number v-model="form.timeout_seconds" :min="1" :max="300" />
            </el-form-item>
            <el-form-item label="Tab 补全"><el-switch v-model="form.enable_completion" /></el-form-item>
            <el-form-item label="帮助透传脚本"><el-switch v-model="form.passthrough_help" /></el-form-item>
            <el-form-item label="启用 PID 锁"><el-switch v-model="form.enable_pid_lock" /></el-form-item>
          </el-tab-pane>

          <el-tab-pane label="校验与预览" name="review">
            <el-alert type="success" :closable="false" show-icon title="平台服务端负责的校验">
              保存时会校验 manifest 15 字段、逐文件 SHA-256、wrapper 透传、注册表原子更新，
              并生成不可变 spec_hash；页面不做最终校验，也不组装部署命令。
            </el-alert>
            <el-divider content-position="left">manifest.json 必填字段</el-divider>
            <div class="tag-cloud">
              <el-tag v-for="field in templateInfo?.manifest_required_fields || []" :key="field" size="small" effect="plain">
                {{ field }}
              </el-tag>
            </div>
            <el-divider content-position="left">出厂内置插件（覆盖时只能选这些）</el-divider>
            <div class="tag-cloud">
              <el-tag v-for="name in templateInfo?.builtin_plugins || []" :key="name" size="small" type="warning" effect="plain">
                {{ name }}
              </el-tag>
            </div>
            <el-divider content-position="left">规格中禁止出现的运行环境字段</el-divider>
            <div class="tag-cloud">
              <el-tag v-for="field in templateInfo?.forbidden_fields || []" :key="field" size="small" type="danger" effect="plain">
                {{ field }}
              </el-tag>
            </div>
            <p class="field-hint">这些字段由服务端环境表与 Secret 解析，页面不收集、不展示、不落库。</p>
          </el-tab-pane>
        </el-tabs>
      </el-form>

      <template #footer>
        <div class="drawer-footer">
          <span class="env-label">目标会话</span>
          <el-select v-model="selectedSessionKey" style="width: 260px" placeholder="custom-ui 已登录的 HCI">
            <el-option
              v-for="item in sessions"
              :key="item.key"
              :label="`${item.case_id} · ${item.node_ip}`"
              :value="item.key"
            />
          </el-select>
          <span class="footer-spacer" />
          <el-button @click="formVisible = false">取消</el-button>
          <el-button type="primary" :loading="publishing" @click="handlePublish">保存并下发</el-button>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="progressVisible" title="下发进度" width="600px" :close-on-click-modal="false">
      <p class="progress-hint">配置已保存，平台服务端正在通过 terminal_bridge 下发到目标环境。</p>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="下发任务">{{ currentDeployId }}</el-descriptions-item>
        <el-descriptions-item label="重复保存">{{ idempotent ? '幂等复用原任务' : '新任务' }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="statusTag(currentDeployment?.status)" size="small">
            {{ statusText(currentDeployment?.status) }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="trace_id">{{ currentDeployment?.trace_id || '-' }}</el-descriptions-item>
        <el-descriptions-item label="错误码">{{ currentDeployment?.error_code || '-' }}</el-descriptions-item>
        <el-descriptions-item label="重试次数">{{ currentDeployment?.attempt ?? '-' }}</el-descriptions-item>
      </el-descriptions>
      <div v-if="currentDeployment?.verification?.smoke_output" class="smoke-output">
        <div class="smoke-title">设备侧冒烟输出</div>
        <pre>{{ currentDeployment.verification.smoke_output }}</pre>
      </div>
      <template #footer>
        <el-button @click="progressVisible = false">关闭</el-button>
        <el-button type="primary" @click="openHistoryByName(form.plugin_name)">
          查看下发历史
        </el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="historyVisible" :title="`下发历史 · ${historyPluginName}`" size="62%">
      <el-table :data="deployments" row-key="deploy_id" empty-text="暂无下发记录" @row-click="showDeployment">
        <el-table-column prop="deploy_id" label="下发任务" min-width="220" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="env_code" label="环境" width="90" />
        <el-table-column prop="attempt" label="尝试" width="80" />
        <el-table-column prop="error_code" label="错误码" min-width="140" />
        <el-table-column prop="created_at" label="创建时间" min-width="170" />
      </el-table>
      <div v-if="selectedDeployment" class="detail-panel">
        <el-divider content-position="left">任务回执详情</el-divider>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="exec_id">{{ selectedDeployment.exec_id || '-' }}</el-descriptions-item>
          <el-descriptions-item label="trace_id">{{ selectedDeployment.trace_id || '-' }}</el-descriptions-item>
          <el-descriptions-item label="spec_hash">{{ selectedDeployment.spec_hash }}</el-descriptions-item>
          <el-descriptions-item label="退出码">
            {{ selectedDeployment.bridge_result?.exit_code ?? '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="错误说明" :span="2">
            {{ selectedDeployment.error_message || '-' }}
          </el-descriptions-item>
        </el-descriptions>
        <div v-if="selectedDeployment.verification?.smoke_output" class="smoke-output">
          <div class="smoke-title">设备侧冒烟输出</div>
          <pre>{{ selectedDeployment.verification.smoke_output }}</pre>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.acli-plugin-page { padding: 20px 24px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.page-header h2 { margin: 0 0 6px; font-size: 20px; font-weight: 600; }
.subtitle { margin: 0; color: #6b7280; font-size: 13px; }
.header-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.boundary-hint { margin-bottom: 16px; }
.table-card { border-radius: 10px; }
.drawer-footer { display: flex; align-items: center; gap: 12px; }
.env-label { color: #4b5563; font-size: 13px; }
.footer-spacer { flex: 1; }
.field-hint { margin-top: 4px; color: #909399; font-size: 12px; line-height: 1.5; }
.field-hint.warning { color: #b45309; }
.dynamic-row { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
.tag-cloud { display: flex; flex-wrap: wrap; gap: 6px; }
.code-editor :deep(textarea) { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.progress-hint { margin: 0 0 14px; color: #6b7280; font-size: 13px; }
.smoke-output { margin-top: 16px; }
.smoke-title { margin-bottom: 6px; color: #374151; font-size: 13px; font-weight: 600; }
.smoke-output pre { max-height: 220px; overflow: auto; padding: 12px; border-radius: 6px; background: #f5f7fa; font-size: 12px; white-space: pre-wrap; }
.detail-panel { margin-top: 18px; }
</style>
