<script setup lang="ts">
/**
 * CaseCreateDialog.vue
 * 创建工单弹框 v2 — 仅负责工单基本信息
 *
 * SSH 流程已移至 SshConnectDialog + SshFlowPanel，
 * 本组件只保留：标题、描述、AI 助手选择，以及两个入口按钮。
 */
import { ref, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()

const isCreating = ref(false)

const form = reactive({
  title: '',
  description: '',
  assistantType: '',
})

// 同步默认助手
watch(
  () => chatStore.selectedAssistant,
  (val) => {
    if (val && !form.assistantType) {
      form.assistantType = val
    }
  },
  { immediate: true },
)

// 弹框打开时同步标题/描述（来自 pendingUserMessage 提取）
watch(
  () => chatStore.showCaseTemplate,
  (val) => {
    if (val) {
      form.title = chatStore.caseTemplate.title
      form.description = chatStore.caseTemplate.description
    }
  },
)

function validateForm(): boolean {
  if (!form.title.trim()) {
    ElMessage.warning('请填写工单标题')
    return false
  }
  if (!form.description.trim()) {
    ElMessage.warning('请填写问题描述')
    return false
  }
  return true
}

// 连接 SSH 并创建工单（两步流：先创建工单，再弹 SSH 弹框）
async function handleConnectAndCreate() {
  if (!validateForm()) return
  isCreating.value = true
  try {
    await chatStore.createCaseAndOpenSsh({
      title: form.title,
      description: form.description,
      assistantType: form.assistantType || undefined,
    })
    // showCaseTemplate 在 createCaseAndOpenSsh 内部已置 false
  } catch (e: any) {
    ElMessage.error(`创建工单失败：${e.response?.data?.detail || e.message}`)
  } finally {
    isCreating.value = false
  }
}

// 无 SSH 创建工单
function handleNoSSHCreate() {
  if (!validateForm()) return
  chatStore.createCaseWithoutSSH(
    { title: form.title, description: form.description },
    form.assistantType || undefined,
  )
}

// 取消
function handleCancel() {
  chatStore.cancelCreateCase()
}
</script>

<template>
  <el-dialog
    v-model="chatStore.showCaseTemplate"
    title="创建工单"
    width="520px"
    :close-on-click-modal="false"
    align-center
    class="case-create-dialog"
  >
    <!-- 基本信息表单 -->
    <el-form label-position="top" class="create-form">
      <el-form-item label="标题">
        <el-input
          v-model="form.title"
          placeholder="简要描述问题"
          maxlength="100"
          show-word-limit
        />
      </el-form-item>
      <el-form-item label="描述">
        <el-input
          v-model="form.description"
          type="textarea"
          :autosize="{ minRows: 3, maxRows: 8 }"
          placeholder="详细描述您遇到的问题..."
        />
      </el-form-item>
      <el-form-item label="AI 助手" v-if="chatStore.showAssistantSelector">
        <el-select v-model="form.assistantType" placeholder="选择 AI 助手" style="width: 100%">
          <el-option
            v-for="assistant in chatStore.assistants"
            :key="assistant.type"
            :label="assistant.display_name"
            :value="assistant.type"
            :disabled="!assistant.available"
          >
            <div class="assistant-option">
              <span>{{ assistant.display_name }}</span>
              <el-tag v-if="assistant.is_default" size="small" type="success">默认</el-tag>
            </div>
          </el-option>
        </el-select>
      </el-form-item>
    </el-form>

    <!-- 操作按钮 -->
    <template #footer>
      <div class="dialog-footer">
        <el-button
          type="primary"
          :loading="isCreating"
          class="btn-connect"
          @click="handleConnectAndCreate"
        >
          🖥 连接 SSH 并创建工单
        </el-button>
        <el-button @click="handleCancel">取消</el-button>
        <el-tooltip
          content="若无 SSH，自动化能力将大大降低，AI 需要您手动提供更多信息"
          placement="top"
          effect="dark"
        >
          <el-button class="btn-no-ssh" @click="handleNoSSHCreate">
            ⚠️ 无 SSH 创建工单
          </el-button>
        </el-tooltip>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.create-form {
  padding: 8px 0;
}

.assistant-option {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: space-between;
}

.dialog-footer {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.btn-connect {
  width: 100%;
}

.btn-no-ssh {
  width: 100%;
  background: #f5f7fa !important;
  border-color: #dcdfe6 !important;
  color: #909399 !important;
  font-size: 12px !important;
}

.btn-no-ssh:hover {
  background: #ebeef5 !important;
  color: #606266 !important;
}
</style>
