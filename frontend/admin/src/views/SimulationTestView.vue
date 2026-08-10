<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

type BuildState = 'idle' | 'building' | 'succeeded' | 'failed'
const endpoint = (import.meta.env.VITE_HCI_SIM_CONTROL_PLANE_URL || '/api/hci-sim').replace(/\/$/, '')
const kbdId = ref(localStorage.getItem('hci-sim:kbd-id') || '')
const title = ref(localStorage.getItem('hci-sim:title') || '')
const description = ref(localStorage.getItem('hci-sim:description') || '')
const buildState = ref<BuildState>('idle')
const progress = ref(0)
const logs = ref<string[]>([])
const expanded = ref<string[]>(['true'])
const connection = ref<Record<string, unknown> | null>(null)
const environmentContext = ref<Record<string, unknown> | null>(null)
const submitting = ref(false)
const canStart = computed(() => buildState.value === 'succeeded' && !!connection.value)

watch([kbdId, title, description], () => {
  localStorage.setItem('hci-sim:kbd-id', kbdId.value)
  localStorage.setItem('hci-sim:title', title.value)
  localStorage.setItem('hci-sim:description', description.value)
})

async function buildEnvironment() {
  if (!/^\d{1,20}$/.test(kbdId.value.trim())) return ElMessage.error('请输入有效 KBD_ID')
  buildState.value = 'building'; progress.value = 10; logs.value = [`开始构建 KBD ${kbdId.value.trim()}`]; connection.value = null; environmentContext.value = null
  try {
    const response = await fetch(`${endpoint}/v1/simulations/build`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kbd_id: kbdId.value.trim() }) })
    if (!response.ok) throw new Error(`控制面 HTTP ${response.status}`)
    const result = await response.json()
    progress.value = 100; buildState.value = 'succeeded'; connection.value = result.connection || result; environmentContext.value = result.environment_context || null; expanded.value = ['true']
    logs.value.push('Bundle 校验通过', '环境已就绪，TestRun 与 Agent context 已绑定')
    ElMessage.success('仿真环境构建成功')
  } catch (error) {
    buildState.value = 'failed'; progress.value = 0; logs.value.push(error instanceof Error ? error.message : String(error)); ElMessage.error('环境构建失败；可直接重试，表单内容会保留')
  }
}

async function createTestRun() {
  if (!canStart.value) return
  if (!title.value.trim() || !description.value.trim()) return ElMessage.warning('请填写标题和描述')
  submitting.value = true
  try {
    const response = await fetch(`${endpoint}/v1/simulations/test-runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kbd_id: kbdId.value.trim(), title: title.value.trim(), description: description.value.trim(), connection: connection.value, environment_context: environmentContext.value }) })
    if (!response.ok) throw new Error(`控制面 HTTP ${response.status}`)
    const result = await response.json(); ElMessage.success(`已创建仿真工单 ${result.case_id || result.test_run_id || ''}`)
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : String(error)) } finally { submitting.value = false }
}

onMounted(() => { if (kbdId.value) logs.value = ['已恢复上次输入；请点击环境构建'] })
</script>

<template>
  <el-card class="simulation-card">
    <template #header><div class="card-header"><span>仿真测试</span><el-tag type="info">sim-ssh · hci_sim</el-tag></div></template>
    <el-alert title="仅允许连接 hci-sim 受管运行时；不会回退到真实 HCI。失败重试会保留当前表单。" type="info" show-icon :closable="false" />
    <el-form label-width="110px" class="sim-form">
      <el-form-item label="KBD_ID"><el-input v-model="kbdId" placeholder="例如 23821" clearable /></el-form-item>
      <el-form-item label="构建状态"><el-progress :percentage="progress" :status="buildState === 'failed' ? 'exception' : buildState === 'succeeded' ? 'success' : undefined" /><el-tag v-if="buildState === 'building'" type="warning">构建中</el-tag><el-tag v-else-if="buildState === 'succeeded'" type="success">已就绪</el-tag><el-tag v-else-if="buildState === 'failed'" type="danger">失败</el-tag></el-form-item>
      <el-form-item><el-button type="primary" :loading="buildState === 'building'" @click="buildEnvironment">环境构建</el-button><el-button type="success" :disabled="!canStart" @click="expanded = ['true']">开始测试</el-button></el-form-item>
    </el-form>
    <el-collapse v-if="logs.length" v-model="expanded" class="logs"><el-collapse-item title="构建详细日志" name="true"><pre>{{ logs.join('\n') }}</pre></el-collapse-item></el-collapse>
    <el-divider />
    <el-form label-width="110px" @submit.prevent="createTestRun"><el-form-item label="标题"><el-input v-model="title" placeholder="只需补充标题" /></el-form-item><el-form-item label="描述"><el-input v-model="description" type="textarea" :rows="3" placeholder="只需补充描述" /></el-form-item><el-form-item><el-button type="primary" :disabled="!canStart" :loading="submitting" @click="createTestRun">连接 SSH 并创建工单</el-button></el-form-item></el-form>
    <el-descriptions v-if="connection" title="仿真 SSH 信息" :column="2" border size="small"><el-descriptions-item label="主机">{{ connection.host || connection.hostname || '由控制面返回' }}</el-descriptions-item><el-descriptions-item label="端口">{{ connection.port || 2222 }}</el-descriptions-item><el-descriptions-item label="用户名">{{ connection.username || 'sim' }}</el-descriptions-item><el-descriptions-item label="TestRun">{{ connection.test_run_id || '已绑定' }}</el-descriptions-item></el-descriptions>
  </el-card>
</template>

<style scoped>
.simulation-card { max-width: 960px; margin: 0 auto; }
.card-header { display: flex; justify-content: space-between; align-items: center; font-weight: 600; }
.sim-form { margin-top: 20px; }
.logs pre { max-height: 260px; overflow: auto; white-space: pre-wrap; background: #101820; color: #d7f9e9; padding: 12px; border-radius: 4px; }
</style>
