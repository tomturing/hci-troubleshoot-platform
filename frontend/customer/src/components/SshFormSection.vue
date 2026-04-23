<script setup lang="ts">
/**
 * SshFormSection.vue
 * SSH 连接表单共享组件
 * 供 CaseCreateDialog 和 SshConnectDialog 复用
 */
import { ref, reactive, onMounted, watch, computed } from 'vue'
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

// 内部引用（方便模板绑定）
const form = reactive(props.sshForm)
const auth = ref(props.authType)

// 同步到父组件
watch(form, (val) => emit('update:sshForm', val), { deep: true })
watch(auth, (val) => emit('update:authType', val))

// localStorage 自动填充提示
const hasAutoFill = computed(() => form.host && form.username)

// 加载上次成功的 SSH 配置（不含密码）
function loadSavedSshConfig() {
  try {
    const saved = localStorage.getItem('hci_last_ssh_config')
    if (saved) {
      const config = JSON.parse(saved)
      if (!form.host && config.host) form.host = config.host
      if (!form.port && config.port) form.port = String(config.port)
      if (!form.username && config.username) form.username = config.username
    }
  } catch {
    // ignore
  }
}

// 组件挂载时自动填充
onMounted(() => {
  loadSavedSshConfig()
})
</script>

<template>
  <div class="ssh-form-section">
    <el-form label-position="top" size="small" class="ssh-form">
      <!-- 主机地址 + 端口 -->
      <div class="form-row">
        <el-form-item label="主机地址" class="form-host">
          <el-input v-model="form.host" placeholder="192.168.1.100" />
        </el-form-item>
        <el-form-item label="端口" class="form-port">
          <el-input v-model="form.port" placeholder="22" />
        </el-form-item>
      </div>

      <!-- 用户名 -->
      <div class="form-row">
        <el-form-item label="用户名" class="form-half">
          <el-input v-model="form.username" placeholder="root" />
        </el-form-item>
      </div>

      <!-- 认证类型切换 -->
      <div class="form-row auth-type-switch">
        <el-radio-group v-model="auth" size="small">
          <el-radio-button value="password">密码认证</el-radio-button>
          <el-radio-button value="key">密钥认证</el-radio-button>
        </el-radio-group>
      </div>

      <!-- 密码认证 -->
      <div v-if="auth === 'password'" class="form-row">
        <el-form-item label="密码" class="form-full">
          <el-input
            v-model="form.password"
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
            v-model="form.privateKey"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 6 }"
            placeholder="粘贴 SSH 私钥内容（如 id_rsa 文件内容）"
          />
        </el-form-item>
        <el-form-item label="私钥密码（可选）" class="form-full">
          <el-input
            v-model="form.passphrase"
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