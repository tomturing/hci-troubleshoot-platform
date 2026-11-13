<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  buildAcliPluginDescriptor,
  createAcliPlugin,
  deleteAcliPlugin,
  getAcliPluginTemplates,
  listAcliPluginDeployments,
  listAcliPlugins,
  recordAcliPluginDeployment,
  updateAcliPlugin,
  type AcliPlugin,
  type DeployDescriptor,
} from '@/api/acliPlugin'

const plugins = ref<AcliPlugin[]>([])
const templates = ref<any>(null)
const loading = ref(false)
const deploying = ref(false)
const autoDeploy = ref(true)
const logs = ref<string[]>([])

const device = reactive({
  bridgeUrl: 'ws://localhost:9999',
  host: '172.28.24.1',
  port: 22,
  username: 'root',
  authType: 'password',
  password: '',
  privateKey: '',
  nodeIp: '172.28.24.1',
  caseId: 'Q2026111200001',
})

const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const form = reactive<AcliPlugin>(emptyPlugin())
const parametersText = ref('[]')
const extraFilesText = ref('[]')
const previewVisible = ref(false)
const preview = ref<DeployDescriptor | null>(null)
const historyVisible = ref(false)
const history = ref<any[]>([])
const contractVisible = ref(false)

function emptyPlugin(): AcliPlugin {
  return {
    plugin_name: '', display_name: '', title_zh: '', title_en: '',
    description_zh: '', description_en: '', vendor_name: 'HTP', change_log: '',
    version: '0.0.1', acli_version: '>=1.0.0', hci_version: '', source: 3,
    status: 1, arch: 1, container: '', interpreter: 'bash',
    script_filename: 'troubleshoot.sh', script_content: '',
    parameters: [], extra_files: [], enable_completion: true,
    passthrough_help: false, enable_pid_lock: false, timeout_seconds: 120,
    smoke_args: '',
    device_host: '172.28.24.1', device_port: 22, device_username: 'root',
    node_ip: '172.28.24.1', source_url: '', is_active: true,
  }
}

function log(event: string, detail = '') {
  const line = `[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] ${event}${detail ? ` — ${detail}` : ''}`
  logs.value = [line, ...logs.value].slice(0, 60)
}

function uuid(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

async function refresh() {
  loading.value = true
  try {
    plugins.value = await listAcliPlugins()
  } catch (e: any) {
    ElMessage.error(`加载插件列表失败: ${e.message}`)
  } finally {
    loading.value = false
  }
}

async function loadTemplates() {
  try {
    templates.value = await getAcliPluginTemplates()
  } catch (e: any) {
    log('参考模板加载失败', e.message)
  }
}

function statusTag(status?: string): { type: string; text: string } {
  switch (status) {
    case 'success': return { type: 'success', text: '已生效' }
    case 'failed': return { type: 'danger', text: '下发失败' }
    case 'rolled_back': return { type: 'warning', text: '已回滚' }
    default: return { type: 'info', text: '未下发' }
  }
}

function fillForm(source: AcliPlugin) {
  Object.assign(form, emptyPlugin(), JSON.parse(JSON.stringify(source)))
  parametersText.value = JSON.stringify(form.parameters || [], null, 2)
  extraFilesText.value = JSON.stringify(form.extra_files || [], null, 2)
}

function openCreate() {
  editingId.value = null
  fillForm(emptyPlugin())
  dialogVisible.value = true
}

function openFromTemplate() {
  if (!templates.value?.one_mt_template) {
    ElMessage.warning('参考模板尚未加载')
    return
  }
  editingId.value = null
  fillForm(templates.value.one_mt_template)
  dialogVisible.value = true
  ElMessage.info('已按 24 环境插件契约与 support 参考文档预填「1mt排障工具」')
}

function openEdit(row: AcliPlugin) {
  editingId.value = row.id || null
  fillForm(row)
  dialogVisible.value = true
}

function handleScriptFile(file: any) {
  const reader = new FileReader()
  reader.onload = () => {
    form.script_content = String(reader.result || '')
    form.script_filename = file.name || 'troubleshoot.sh'
    ElMessage.success(`已载入脚本 ${file.name}`)
  }
  reader.readAsText(file.raw)
  return false
}

function parseJsonField(text: string, label: string): any[] | null {
  try {
    const value = JSON.parse(text)
    if (!Array.isArray(value)) throw new Error('必须是数组')
    return value
  } catch (e: any) {
    ElMessage.error(`${label} 不是合法 JSON 数组: ${e.message}`)
    return null
  }
}

async function saveForm(): Promise<boolean> {
  const params = parseJsonField(parametersText.value, '参数定义')
  const extras = parseJsonField(extraFilesText.value, '附加文件')
  if (!params || !extras) return false
  form.parameters = params
  form.extra_files = extras
  if (!form.plugin_name || !form.display_name || !form.script_content.trim()) {
    ElMessage.error('插件名、显示名与原始脚本为必填项')
    return false
  }
  try {
    if (editingId.value) {
      await updateAcliPlugin(editingId.value, form)
    } else {
      const result = await createAcliPlugin(form)
      editingId.value = result.id
    }
    ElMessage.success('插件配置已保存')
    log('配置保存成功', form.plugin_name)
    await refresh()
    return true
  } catch (e: any) {
    ElMessage.error(`保存失败: ${e.message}`)
    return false
  }
}

async function saveOnly() {
  if (await saveForm()) dialogVisible.value = false
}

async function saveAndDeploy() {
  if (!(await saveForm())) return
  dialogVisible.value = false
  if (autoDeploy.value) {
    const row = plugins.value.find((p) => p.id === editingId.value)
    if (row) await deploy(row)
  }
}

async function showPreview(row: AcliPlugin) {
  try {
    preview.value = await buildAcliPluginDescriptor(row.id!)
    previewVisible.value = true
  } catch (e: any) {
    ElMessage.error(`生成产物失败: ${e.message}`)
  }
}

async function showHistory(row: AcliPlugin) {
  try {
    history.value = await listAcliPluginDeployments(row.id!)
    historyVisible.value = true
  } catch (e: any) {
    ElMessage.error(`加载下发历史失败: ${e.message}`)
  }
}

async function removePlugin(row: AcliPlugin) {
  try {
    await ElMessageBox.confirm(`确认删除插件配置「${row.display_name}」？设备侧已部署的插件不会被卸载。`, '删除确认', { type: 'warning' })
    await deleteAcliPlugin(row.id!)
    ElMessage.success('已删除')
    await refresh()
  } catch { /* 用户取消 */ }
}

// ── terminal_bridge WS 通道 ──────────────────────────────────────────────────
let ws: WebSocket | null = null
let connected = false
let connectResolve: ((ok: boolean) => void) | null = null
let execResolve: ((msg: any) => void) | null = null
let currentExecId = ''
const bridgeVersion = ref('')
const bridgeConnected = ref(false)

function closeBridge() {
  try { ws?.close() } catch { /* ignore */ }
  ws = null
  connected = false
  bridgeConnected.value = false
}

function handleMessage(raw: string) {
  let msg: any
  try { msg = JSON.parse(raw) } catch { return }
  if (msg.type === 'bridge_hello') {
    bridgeVersion.value = msg.version || msg.service_version || ''
    log('bridge_hello', bridgeVersion.value)
  } else if (msg.type === 'ssh_connected') {
    connected = true
    bridgeConnected.value = true
    log('SSH 会话已建立', device.host)
    if (connectResolve) { connectResolve(true); connectResolve = null }
  } else if (msg.type === 'ssh_error') {
    log('SSH 连接失败', `${msg.message || ''} ${msg.detail || ''}`.trim())
    if (connectResolve) { connectResolve(false); connectResolve = null }
  } else if (msg.type === 'exec_result' && msg.exec_id === currentExecId) {
    if (execResolve) { execResolve(msg); execResolve = null }
  }
}

function waitFor<T>(setter: (resolve: (v: T) => void) => void, timeoutMs: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(`${label} 超时`)), timeoutMs)
    setter((value: T) => { window.clearTimeout(timer); resolve(value) })
  })
}

async function ensureBridge(): Promise<boolean> {
  if (ws && ws.readyState === WebSocket.OPEN && connected) return true
  closeBridge()
  if (!device.host || (!device.password && !(device.authType === 'key' && device.privateKey))) {
    ElMessage.warning('请先填写目标 HCI 地址与认证信息（口令前缀或免密私钥）')
    return false
  }
  log('连接 terminal_bridge', device.bridgeUrl)
  ws = new WebSocket(device.bridgeUrl)
  ws.onmessage = (event) => handleMessage(String(event.data))
  ws.onerror = () => log('WebSocket 异常', device.bridgeUrl)
  ws.onclose = () => { connected = false; bridgeConnected.value = false }
  await waitFor<void>((resolve) => {
    if (ws!.readyState === WebSocket.OPEN) { resolve(); return }
    ws!.onopen = () => resolve()
    window.setTimeout(() => resolve(), 8000)
  }, 9000, 'WebSocket 连接')
  if (ws.readyState !== WebSocket.OPEN) {
    ElMessage.error('无法连接 terminal_bridge（请确认 Windows 侧桥已常驻）')
    return false
  }
  const traceId = 'htp-acli-' + uuid()
  ws.send(JSON.stringify({
    type: 'ssh_connect',
    case_id: device.caseId,
    host: device.host,
    port: Number(device.port) || 22,
    username: device.username,
    auth_type: device.authType,
    password: device.authType === 'key' ? '' : device.password,
    private_key: device.authType === 'key' ? device.privateKey : '',
    node_ip: device.nodeIp || device.host,
    trace_id: traceId,
  }))
  const ok = await waitFor<boolean>((resolve) => { connectResolve = resolve }, 25000, 'ssh_connect')
  if (!ok) ElMessage.error('SSH 连接失败（口令请填去掉 sangfornetwork 后缀的前缀）')
  return ok
}

function parseResult(stdout: string): any {
  const begin = stdout.indexOf('ACP_RESULT_BEGIN')
  const end = stdout.indexOf('ACP_RESULT_END')
  let result: any = {}
  if (begin >= 0 && end > begin) {
    const jsonText = stdout.slice(begin + 'ACP_RESULT_BEGIN'.length, end).trim().split('\n').find((l) => l.trim().startsWith('{')) || '{}'
    try { result = JSON.parse(jsonText) } catch { result = {} }
  }
  const listBegin = stdout.indexOf('ACP_PLUGIN_LIST_BEGIN')
  const listEnd = stdout.indexOf('ACP_PLUGIN_LIST_END')
  result.plugin_list = listBegin >= 0 && listEnd > listBegin
    ? stdout.slice(listBegin + 'ACP_PLUGIN_LIST_BEGIN'.length, listEnd).trim()
    : ''
  return result
}

async function deploy(row: AcliPlugin) {
  if (deploying.value) return
  deploying.value = true
  log('开始下发', `${row.plugin_name}@${row.device_host || device.host}`)
  try {
    const descriptor = await buildAcliPluginDescriptor(row.id!)
    log('产物已生成', `${descriptor.deploy_id} / ${descriptor.files.length} 个文件 / spec_hash=${descriptor.spec_hash.slice(0, 12)}…`)
    if (!device.password && !(device.authType === 'key' && device.privateKey)) {
      ElMessage.warning('请在设备连接区填写 SSH 口令前缀或免密私钥后再下发')
      return
    }
    if (!(await ensureBridge())) return
    const execId = uuid()
    const traceId = 'htp-acli-deploy-' + uuid()
    currentExecId = execId
    const resultPromise = waitFor<any>((resolve) => { execResolve = resolve }, 190000, '部署命令回执')
    ws!.send(JSON.stringify({
      type: 'ssh_exec_process',
      case_id: device.caseId,
      node_ip: device.nodeIp || device.host,
      exec_id: execId,
      trace_id: traceId,
      conversation_id: '',
      tool_call_id: '',
      container: '',
      timeout: 180,
      command: descriptor.deploy_command,
    }))
    log('已通过 ssh_exec_process 下发', `exec_id=${execId} trace_id=${traceId}`)
    const execResult = await resultPromise
    const parsed = parseResult(execResult.stdout || '')
    const success = execResult.exit_code === 0 && parsed.status === 'success'
    log('部署回执', `exit=${execResult.exit_code} status=${parsed.status || 'unknown'} ${parsed.error || ''}`)
    await recordAcliPluginDeployment(row.id!, {
      deploy_id: descriptor.deploy_id,
      spec_hash: descriptor.spec_hash,
      version: descriptor.version,
      device_host: row.device_host || device.host,
      node_ip: row.node_ip || device.nodeIp,
      exec_id: execId,
      trace_id: traceId,
      status: success ? 'success' : (parsed.status === 'rolled_back' ? 'rolled_back' : 'failed'),
      exit_code: typeof execResult.exit_code === 'number' ? execResult.exit_code : null,
      stdout: String(execResult.stdout || '').slice(0, 20000),
      stderr: String(execResult.stderr || '').slice(0, 20000),
      verification: { plugin_list: parsed.plugin_list, status: parsed.status || '', error: parsed.error || '' },
    })
    if (success) ElMessage.success(`插件 ${row.plugin_name} 已在目标 HCI 生效`)
    else ElMessage.error(`下发未生效：${parsed.error || execResult.stderr || '请查看下发日志'}`)
    await refresh()
  } catch (e: any) {
    log('下发异常', e.message)
    ElMessage.error(`下发异常: ${e.message}`)
  } finally {
    deploying.value = false
  }
}

const contract = computed(() => templates.value?.runtime_contract || null)
const referencePlugins = computed(() => templates.value?.reference_plugins || [])

refresh()
loadTemplates()
onBeforeUnmount(closeBridge)
</script>

<template>
  <div class="acli-plugin-container">
    <div class="page-header">
      <div>
        <h2 class="page-title">acli插件</h2>
        <p class="page-desc">
          基于 acli 手工插件机制（方案 B）维护插件全部关键信息，保存后自动经 terminal_bridge 的
          ssh_exec_process 隔离通道下发到客户 HCI，完成落盘、注册、补全与生效验证。
        </p>
      </div>
      <div class="header-actions">
        <el-button @click="contractVisible = true">24 环境契约</el-button>
        <el-button @click="openFromTemplate">从参考模板创建</el-button>
        <el-button type="primary" @click="openCreate">新建插件</el-button>
      </div>
    </div>

    <el-card shadow="never" class="device-card">
      <template #header>
        <div class="card-header">
          <span>设备连接（terminal_bridge）</span>
          <el-tag :type="bridgeConnected ? 'success' : 'info'" size="small">
            {{ bridgeConnected ? 'SSH 已连接' : '未连接' }} {{ bridgeVersion ? `· ${bridgeVersion}` : '' }}
          </el-tag>
        </div>
      </template>
      <el-form :inline="true" label-position="top" class="device-form">
        <el-form-item label="Bridge WebSocket">
          <el-input v-model="device.bridgeUrl" style="width: 210px" />
        </el-form-item>
        <el-form-item label="目标 HCI">
          <el-input v-model="device.host" style="width: 150px" />
        </el-form-item>
        <el-form-item label="端口">
          <el-input-number v-model="device.port" :min="1" :max="65535" controls-position="right" style="width: 100px" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input v-model="device.username" style="width: 100px" />
        </el-form-item>
        <el-form-item label="认证方式">
          <el-select v-model="device.authType" style="width: 120px">
            <el-option label="口令前缀" value="password" />
            <el-option label="免密私钥" value="key" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="device.authType === 'password'" label="SSH 口令前缀">
          <el-input v-model="device.password" type="password" show-password style="width: 170px"
            placeholder="去掉 sangfornetwork 后缀" />
        </el-form-item>
        <el-form-item v-else label="免密私钥（不落库）">
          <el-input v-model="device.privateKey" type="textarea" :rows="2" style="width: 260px"
            placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />
        </el-form-item>
        <el-form-item label="node_ip">
          <el-input v-model="device.nodeIp" style="width: 150px" />
        </el-form-item>
        <el-form-item label="case_id">
          <el-input v-model="device.caseId" style="width: 160px" />
        </el-form-item>
        <el-form-item label="连接">
          <el-button :loading="deploying" @click="ensureBridge">连接设备</el-button>
        </el-form-item>
      </el-form>
      <el-alert type="info" :closable="false" show-icon
        title="页面只填口令前缀，桥会自动追加 sangfornetwork 后缀；口令不落库。下发复用同一 SSH 会话，避免重复登录。" />
    </el-card>

    <el-card shadow="never" class="table-card">
      <template #header>
        <div class="card-header">
          <span>插件配置（{{ plugins.length }}）</span>
          <el-checkbox v-model="autoDeploy">保存后自动下发</el-checkbox>
        </div>
      </template>
      <el-table v-loading="loading" :data="plugins" border stripe size="small">
        <el-table-column prop="plugin_name" label="插件名" min-width="150" />
        <el-table-column prop="display_name" label="显示名" min-width="140" />
        <el-table-column prop="version" label="版本" width="90" />
        <el-table-column prop="device_host" label="目标设备" min-width="130" />
        <el-table-column label="下发状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.last_deploy_status).type" size="small">
              {{ statusTag(row.last_deploy_status).text }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="spec_hash" min-width="130">
          <template #default="{ row }">{{ (row.spec_hash || '—').slice(0, 12) }}</template>
        </el-table-column>
        <el-table-column prop="updated_at" label="更新时间" min-width="160" />
        <el-table-column label="操作" width="330" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" @click="showPreview(row)">产物预览</el-button>
            <el-button size="small" type="primary" :loading="deploying" @click="deploy(row)">下发生效</el-button>
            <el-button size="small" @click="showHistory(row)">历史</el-button>
            <el-button size="small" type="danger" plain @click="removePlugin(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="log-card">
      <template #header><span>下发链路日志</span></template>
      <div class="log-box">
        <div v-for="(line, idx) in logs" :key="idx" class="log-line">{{ line }}</div>
        <div v-if="!logs.length" class="log-empty">暂无下发记录</div>
      </div>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑 acli 插件' : '新建 acli 插件'" width="92%" top="3vh">
      <el-form label-width="150px" class="plugin-form">
        <el-tabs>
          <el-tab-pane label="基础信息">
            <el-form-item label="插件名" required>
              <el-input v-model="form.plugin_name" placeholder="小写字母开头，如 hci_1mt_check" />
            </el-form-item>
            <el-form-item label="显示名" required><el-input v-model="form.display_name" /></el-form-item>
            <el-form-item label="版本"><el-input v-model="form.version" /></el-form-item>
            <el-form-item label="标题（中/英）">
              <el-input v-model="form.title_zh" placeholder="中文" style="width: 48%" />
              <el-input v-model="form.title_en" placeholder="English" style="width: 48%; margin-left: 4%" />
            </el-form-item>
            <el-form-item label="描述（中）">
              <el-input v-model="form.description_zh" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="描述（英）">
              <el-input v-model="form.description_en" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="厂商 / 变更说明">
              <el-input v-model="form.vendor_name" style="width: 30%" />
              <el-input v-model="form.change_log" style="width: 66%; margin-left: 4%" />
            </el-form-item>
            <el-form-item label="acli / HCI 版本约束">
              <el-input v-model="form.acli_version" style="width: 48%" />
              <el-input v-model="form.hci_version" style="width: 48%; margin-left: 4%" />
            </el-form-item>
            <el-form-item label="source / status / arch">
              <el-input-number v-model="form.source" :min="1" :max="3" />
              <el-input-number v-model="form.status" :min="1" :max="1" style="margin-left: 12px" />
              <el-input-number v-model="form.arch" :min="1" :max="1" style="margin-left: 12px" />
            </el-form-item>
            <el-form-item label="参考文档 URL"><el-input v-model="form.source_url" /></el-form-item>
          </el-tab-pane>

          <el-tab-pane label="原始脚本">
            <el-form-item label="上传脚本">
              <el-upload :auto-upload="false" :limit="1" :on-change="handleScriptFile" :show-file-list="true">
                <el-button size="small">选择原始脚本</el-button>
              </el-upload>
            </el-form-item>
            <el-form-item label="脚本文件名"><el-input v-model="form.script_filename" /></el-form-item>
            <el-form-item label="解释器"><el-input v-model="form.interpreter" /></el-form-item>
            <el-form-item label="脚本内容" required>
              <el-input v-model="form.script_content" type="textarea" :rows="16" class="code-input" />
            </el-form-item>
          </el-tab-pane>

          <el-tab-pane label="参数与选项">
            <el-form-item label="参数定义 (JSON 数组)">
              <el-input v-model="parametersText" type="textarea" :rows="10" class="code-input" />
            </el-form-item>
            <el-form-item label="附加文件 (JSON 数组)">
              <el-input v-model="extraFilesText" type="textarea" :rows="5" class="code-input" />
            </el-form-item>
            <el-form-item label="Tab 补全"><el-switch v-model="form.enable_completion" /></el-form-item>
            <el-form-item label="透传 --help"><el-switch v-model="form.passthrough_help" /></el-form-item>
            <el-form-item label="并发 PID 锁"><el-switch v-model="form.enable_pid_lock" /></el-form-item>
            <el-form-item label="执行超时（秒）"><el-input-number v-model="form.timeout_seconds" :min="10" :max="600" /></el-form-item>
            <el-form-item label="生效冒烟参数">
              <el-input v-model="form.smoke_args" placeholder="如 -m env -c cpu_load；留空则只验证 --help" />
            </el-form-item>
          </el-tab-pane>

          <el-tab-pane label="目标设备">
            <el-form-item label="设备 IP"><el-input v-model="form.device_host" /></el-form-item>
            <el-form-item label="端口"><el-input-number v-model="form.device_port" :min="1" :max="65535" /></el-form-item>
            <el-form-item label="用户名"><el-input v-model="form.device_username" /></el-form-item>
            <el-form-item label="node_ip"><el-input v-model="form.node_ip" /></el-form-item>
            <el-form-item label="启用"><el-switch v-model="form.is_active" /></el-form-item>
          </el-tab-pane>
        </el-tabs>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button @click="saveOnly">仅保存</el-button>
        <el-button type="primary" :loading="deploying" @click="saveAndDeploy">
          {{ autoDeploy ? '保存并下发' : '保存' }}
        </el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="previewVisible" title="平台生成的下发产物（Builder）" size="62%">
      <template v-if="preview">
        <el-alert type="success" :closable="false" show-icon
          :title="`deploy_id=${preview.deploy_id} · spec_hash=${preview.spec_hash}`" />
        <h4>注册表项 plugin_register.json</h4>
        <pre class="code-block">{{ JSON.stringify(preview.register_item, null, 2) }}</pre>
        <h4>manifest.json</h4>
        <pre class="code-block">{{ JSON.stringify(preview.manifest, null, 2) }}</pre>
        <h4>plugin wrapper</h4>
        <pre class="code-block">{{ preview.wrapper }}</pre>
        <h4>文件清单（{{ preview.files.length }}）</h4>
        <pre class="code-block">{{ preview.files.map(f => `${f.mode}  ${f.path}  ${f.sha256}`).join('\n') }}</pre>
        <h4>HCI 部署脚本（含备份 / 校验 / 回滚 / 生效验证）</h4>
        <pre class="code-block">{{ preview.deploy_script }}</pre>
      </template>
    </el-drawer>

    <el-drawer v-model="historyVisible" title="下发回执历史" size="58%">
      <el-table :data="history" border size="small">
        <el-table-column prop="deploy_id" label="deploy_id" min-width="190" />
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column prop="exit_code" label="exit" width="70" />
        <el-table-column prop="trace_id" label="trace_id" min-width="200" />
        <el-table-column prop="created_at" label="时间" min-width="170" />
      </el-table>
    </el-drawer>

    <el-drawer v-model="contractVisible" title="24 环境实测契约与已有插件" size="52%">
      <template v-if="contract">
        <h4>运行时路径</h4>
        <pre class="code-block">{{ JSON.stringify(contract.paths, null, 2) }}</pre>
        <h4>硬约束</h4>
        <ul><li v-for="(rule, idx) in contract.hard_rules" :key="idx">{{ rule }}</li></ul>
        <h4>24 环境已有插件（{{ referencePlugins.length }}）</h4>
        <el-table :data="referencePlugins" border size="small">
          <el-table-column prop="name" label="插件名" />
          <el-table-column prop="version" label="版本" width="100" />
          <el-table-column prop="source" label="source" width="90" />
        </el-table>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.acli-plugin-container { padding: 18px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.page-title { margin: 0 0 6px; font-size: 22px; }
.page-desc { margin: 0; color: #606266; font-size: 13px; max-width: 880px; line-height: 1.6; }
.header-actions { display: flex; gap: 8px; }
.device-card, .table-card, .log-card { margin-bottom: 14px; }
.card-header { display: flex; justify-content: space-between; align-items: center; font-weight: 600; }
.device-form :deep(.el-form-item) { margin-bottom: 4px; }
.plugin-form :deep(.el-form-item) { margin-bottom: 12px; }
.code-input :deep(textarea) { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.code-block { background: #f6f8fa; border: 1px solid #e4e7ed; border-radius: 4px; padding: 10px; font-size: 12px;
  font-family: Consolas, Monaco, monospace; white-space: pre-wrap; word-break: break-all; max-height: 340px; overflow: auto; }
.log-box { max-height: 220px; overflow: auto; font-family: Consolas, Monaco, monospace; font-size: 12px; }
.log-line { padding: 2px 0; border-bottom: 1px dashed #ebeef5; }
.log-empty { color: #909399; }
</style>
