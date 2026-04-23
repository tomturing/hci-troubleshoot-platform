<script setup lang="ts">
/**
 * SshFormSection.vue
 * SSH 连接表单共享组件
 * 供 CaseCreateDialog 和 SshConnectDialog 复用
 */
import { ref, watch, computed } from 'vue'
import type { TerminalAuthType } from '@/api/terminal'

// Props: 接收外部表单数据
const props = defineProps<{
  sshForm: {
    host: string
    port: string
    username: string
    password: string
    privateKey: string
    passphrase: string
  }
  authType: TerminalAuthType
}>()

// Emit: 表单数据变更
const emit = defineEmits<{
  'update:sshForm': [value: typeof props.sshForm]
  'update:authType': [value: TerminalAuthType]
}>()

// 内部维护本地副本（避免直接修改 props）
const localForm = ref({
  host: props.sshForm.host,
  port: props.sshForm.port,
  username: props.sshForm.username,
  password: props.sshForm.password,
  privateKey: props.sshForm.privateKey,
  passphrase: props.sshForm.passphrase,
})
const localAuthType = ref<TerminalAuthType>(props.authType)

// 监听 props 变化，同步到本地副本
watch(() => props.sshForm, (val) => {
  localForm.value = { ...val }
}, { deep: true })
watch(() => props.authType, (val) => {
  localAuthType.value = val
})

// 同步本地副本变更到父组件
watch(localForm, (val) => emit('update:sshForm', { ...val }), { deep: true })
watch(localAuthType, (val) => emit('update:authType', val))

// localStorage 自动填充提示
const hasAutoFill = computed(() => localForm.value.host && localForm.value.username)

// 加载上次成功的 SSH 配置（不含密码）
function loadSavedSshConfig() {
  try {
    const saved = localStorage.getItem('hci_last_ssh_config')
    if (saved) {
      const config = JSON.parse(saved)
      if (!localForm.value.host && config.host) localForm.value.host = config.host
      if (!localForm.value.port && config.port) localForm.value.port = String(config.port)
      if (!localForm.value.username && config.username) localForm.value.username = config.username
    }
  } catch {
    // ignore
  }
}

// 组件挂载时自动填充
loadSavedSshConfig()
</script>

<template>
  <div class="ssh-form-section">
    <el-form label-position="top" size="small" class="ssh-form">
      <!-- 主机地址 + 端口 -->
      <div class="form-row">
        <el-form-item label="主机地址" class="form-host">
          <el-input v-model="localForm.host" placeholder="192.168.1.100" />
        </el-form-item>
        <el-form-item label="端口" class="form-port">
          <el-input v-model="localForm.port" placeholder="22" />
        </el-form-item>
      </div>

      <!-- 用户名 -->
      <div class="form-row">
        <el-form-item label="用户名" class="form-half">
          <el-input v-model="localForm.username" placeholder="root" />
        </el-form-item>
      </div>

      <!-- 认证类型切换 -->
      <div class="form-row auth-type-switch">
        <el-radio-group v-model="localAuthType" size="small">
          <el-radio-button value="password">密码认证</el-radio-button>
          <el-radio-button value="key">密钥认证</el-radio-button>
        </el-radio-group>
      </div>

      <!-- 密码认证 -->
      <div v-if="localAuthType === 'password'" class="form-row">
        <el-form-item label="密码" class="form-full">
          <el-input
            v-model="localForm.password"
            type="password"
            placeholder="请输入密码"
            show-password
          />
        </el-form-item>
      </div>

      <!-- 密钥认证 -->
      <div v-else class="form-row key-auth-section">
        <el-form-item label="私钥" class="form-full">
          <el-input
            v-model="localForm.privateKey"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 6 }"
            placeholder="粘贴 SSH 私钥内容（如 id_rsa 文件内容）"
          />
        </el-form-item>
        <el-form-item label="私钥密码（可选）" class="form-full">
          <el-input
            v-model="localForm.passphrase"
            type="password"
            placeholder="若私钥有密码保护则填写"
            show-password
          />
        </el-form-item>
      </div>

      <!-- 自动填充提示 -->
      <p v-if="hasAutoFill" class="ssh-autofill-hint">
        💡 上次成功连接的信息已自动填充
      </p>
    </el-form>
  </div>
</template>

<style scoped>
.ssh-form-section {
  padding: 0;
}

.ssh-form {
  width: 100%;
}

.form-row {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}

.form-host {
  flex: 3;
}

.form-port {
  flex: 1;
}

.form-half {
  flex: 1;
}

.form-full {
  flex: 1;
}

.auth-type-switch {
  margin-bottom: 16px;
}

.key-auth-section {
  flex-direction: column;
  gap: 12px;
}

.ssh-autofill-hint {
  font-size: 12px;
  color: #909399;
  margin: 8px 0 0;
}
</style>