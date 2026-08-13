<script setup lang="ts">
import { computed, ref } from 'vue'
import OfflineDiagnosisPanel from '@/components/OfflineDiagnosisPanel.vue'
import { buildOfflineDiagnosisUrl } from '@/utils/offlineDiagnosis'

const query = new URLSearchParams(window.location.search)
const initialCaseId = query.get('case_id')?.trim() || ''
const caseId = ref(initialCaseId)
const caseIdInput = ref(initialCaseId)
const workspaceVisible = ref(true)
const canOpen = computed(() => Boolean(caseIdInput.value.trim()))

/** 支持从独立地址手工输入已有工单号，并把地址同步成可复制链接。 */
function openWorkspace() {
  const normalizedCaseId = caseIdInput.value.trim()
  if (!normalizedCaseId) return
  caseId.value = normalizedCaseId
  workspaceVisible.value = true
  window.history.replaceState(null, '', buildOfflineDiagnosisUrl(normalizedCaseId))
}
</script>

<template>
  <div class="offline-page">
    <section v-if="!caseId" class="case-entry">
      <div class="brand">HCI Offline Diagnosis（离线诊断）</div>
      <h1>打开工单的离线诊断工作区</h1>
      <p>输入已有工单号后，下载离线采集工具、上传采集结果并查看工程师审核后的诊断报告。</p>
      <el-input
        v-model="caseIdInput"
        size="large"
        placeholder="请输入工单号，例如 Q202607310001"
        @keyup.enter="openWorkspace"
      />
      <el-button type="primary" size="large" :disabled="!canOpen" @click="openWorkspace">
        进入离线诊断
      </el-button>
      <a href="/" class="back-link">返回原 Customer UI（客户界面）</a>
    </section>

    <OfflineDiagnosisPanel
      v-else
      v-model="workspaceVisible"
      :case-id="caseId"
      standalone
    />
  </div>
</template>

<style scoped>
.offline-page {
  min-height: 100vh;
  background: var(--el-bg-color-page);
}

.case-entry {
  width: min(620px, calc(100% - 32px));
  margin: 0 auto;
  padding-top: 15vh;
  text-align: center;
}

.brand {
  margin-bottom: 16px;
  color: var(--el-color-primary);
  font-size: 16px;
  font-weight: 600;
}

h1 {
  margin: 0 0 14px;
  color: var(--el-text-color-primary);
  font-size: 30px;
}

p {
  margin: 0 0 28px;
  color: var(--el-text-color-secondary);
  line-height: 1.7;
}

.case-entry :deep(.el-input) {
  margin-bottom: 18px;
}

.back-link {
  display: block;
  margin-top: 24px;
  color: var(--el-text-color-secondary);
  text-decoration: none;
}

.back-link:hover {
  color: var(--el-color-primary);
}
</style>
