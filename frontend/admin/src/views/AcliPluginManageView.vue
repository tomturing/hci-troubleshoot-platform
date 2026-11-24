<script setup lang="ts">
/**
 * acli 插件管理页（仅规格维护）。
 *
 * 页面只维护插件规格：列表 / 新建 / 编辑 / 下载信息模板。保存只落
 * 不可变 revision，**不再下发、不轮询任务**——插件到达节点由客户侧
 * acli_sync 第二阶段持票据拉取平台 manifest 驱动（链路收口 D1/S4c）。
 * 页面不接触 SSH 口令 / 私钥，不连桥，也不选择目标环境。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import {
  downloadTemplate,
  getTemplate,
  listPlugins,
  saveSpec,
  type AcliPluginItem,
  type AcliPluginSpec,
  type AcliPluginTemplateResponse,
} from '@/api/acliPlugin'

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
const templateInfo = ref<AcliPluginTemplateResponse | null>(null)
const loading = ref(false)
const saving = ref(false)
const formVisible = ref(false)
const activeTab = ref('basic')
const isEditing = ref(false)
const form = ref<AcliPluginSpec>(blankSpec())

const smokeHint = computed(() =>
  form.value.smoke_args
    ? `设备侧将由 acli_sync 第二阶段真实执行：/sf/bin/acli --timeout 60 ${form.value.plugin_name || '<插件名>'} ${form.value.smoke_args}`
    : '必须填写冒烟参数，只跑 --help 不算生效',
)

function isPublished(row: AcliPluginItem): boolean {
  // 保存即发布：有期望 revision 就算已发布。平台不接收节点侧回传（D1 删掉了
  // 回执端点与下发表），因此本页只回答「期望态是什么」，不回答「节点上现在是什么」。
  return Boolean(row.desired_spec_hash)
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
    templateInfo.value = await getTemplate()
  } catch {
    ElMessage.warning('模板加载失败，可稍后重试')
  }
}

function openCreate() {
  isEditing.value = false
  // 新建时保留模板字段结构、清空示例值，避免误把 lmt 示例直接保存。
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

function validateBeforeSave(): boolean {
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
  // 与后端 MAX_PLUGIN_FILE_BYTES / 桥侧 acliPluginMaxFileBytes 同口径（UTF-8 字节数）。
  // 提前拦截，避免保存成功但客户侧整单拒收（manifest_file_too_large）的误导。
  if (new TextEncoder().encode(spec.script_content).length > 1024 * 1024) {
    ElMessage.warning('原始脚本超过 1MiB 单文件上限，请精简后再保存')
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

async function handleSave() {
  if (!validateBeforeSave()) return
  if (form.value.update_mode === 'builtin_override') {
    try {
      await ElMessageBox.confirm(
        '将覆盖 HCI 出厂内置插件规格：保留 built_in / source / 原 id。客户侧 acli_sync 收敛时设备侧会强制备份原目录、冒烟失败自动回滚。请确认已填写现场注册项原 id 与变更说明。',
        '覆盖内置插件确认',
        { type: 'warning', confirmButtonText: '确认保存规格', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  saving.value = true
  try {
    const result = await saveSpec(form.value)
    if (result.revision == null) {
      ElMessage.info('规格未变化（同一 spec_hash 幂等复用原 revision）')
    } else {
      ElMessage.success(`规格已保存为 r${result.revision}，将在客户下次触发 acli 同步时收敛到节点`)
    }
    formVisible.value = false
    await loadPlugins()
  } catch (error: any) {
    const status = error?.response?.status
    const errors = error?.response?.data?.detail?.errors
    if (Array.isArray(errors) && errors.length) {
      // 规格校验失败：逐条展示具体字段原因（如「script_content 不能为空」）。
      ElMessage.error(errors.join('；'))
    } else if (status === 401 || status === 403) {
      // 鉴权失效不是内容错误：入口的 hci:auth-error 监听会清令牌并跳登录，
      // 这里补一句明确文案，避免再次显示成「保存规格失败」把用户带偏。
      ElMessage.error('登录已过期或无管理权限，请重新登录后再保存')
    } else {
      ElMessage.error('保存规格失败')
    }
  } finally {
    saving.value = false
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
</script>

<template>
  <div class="acli-plugin-page">
    <div class="page-header">
      <div>
        <h2>acli 插件</h2>
        <p class="subtitle">维护 HCI acli 插件规格；保存即发布，节点收敛由客户侧 acli 同步（第二阶段）自动完成</p>
      </div>
      <div class="header-actions">
        <el-button :icon="Download" @click="handleDownloadTemplate">下载信息模板</el-button>
        <el-button :icon="Refresh" @click="loadPlugins">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建插件</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" show-icon class="boundary-hint">
      <template #title>插件规格是平台唯一事实源</template>
      页面只维护插件本身的信息；目标节点、SSH 凭据与下发通道不在本页出现。插件何时到达节点，
      完全由客户在 custom-ui 连接 HCI 后触发的 acli_sync 第二阶段（拉取平台 manifest、差量收敛）决定。
      本页因此不展示节点实际状态：要看收敛结果，到 custom-ui 的「插件同步」回执或桥日志核对。
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
        <el-table-column label="规格" width="120">
          <template #default="{ row }">
            <el-tag :type="isPublished(row) ? 'success' : 'info'" size="small">
              {{ isPublished(row) ? `已发布 r${row.revision ?? '-'}` : '未发布' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="spec_hash" label="spec_hash" min-width="140">
          <template #default="{ row }">
            <span class="hash-cell" :title="row.desired_spec_hash">{{ (row.desired_spec_hash || '').slice(0, 12) || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :icon="Edit" @click="openEdit(row)">编辑</el-button>
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
                覆盖出厂插件需二次确认；客户侧收敛时设备侧会强制备份并在冒烟失败时回滚。
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
            <el-form-item label="执行超时(秒)">
              <el-input-number v-model="form.timeout_seconds" :min="1" :max="300" />
            </el-form-item>
            <el-form-item label="Tab 补全"><el-switch v-model="form.enable_completion" /></el-form-item>
            <el-form-item label="帮助透传脚本"><el-switch v-model="form.passthrough_help" /></el-form-item>
            <el-form-item label="启用 PID 锁"><el-switch v-model="form.enable_pid_lock" /></el-form-item>
          </el-tab-pane>

          <el-tab-pane label="校验与预览" name="review">
            <el-alert type="success" :closable="false" show-icon title="平台服务端负责的校验">
              保存时会校验 manifest 15 字段、逐文件 SHA-256、wrapper 透传、注册项结构，
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
            <p class="field-hint">这些字段属于运行环境，页面不收集、服务端规格也不保存。</p>
          </el-tab-pane>
        </el-tabs>
      </el-form>

      <template #footer>
        <div class="drawer-footer">
          <span class="footer-spacer" />
          <el-button @click="formVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="handleSave">保存规格</el-button>
        </div>
      </template>
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
.footer-spacer { flex: 1; }
.field-hint { margin-top: 4px; color: #909399; font-size: 12px; line-height: 1.5; }
.field-hint.warning { color: #b45309; }
.dynamic-row { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
.tag-cloud { display: flex; flex-wrap: wrap; gap: 6px; }
.code-editor :deep(textarea) { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.hash-cell { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: #6b7280; }
</style>
