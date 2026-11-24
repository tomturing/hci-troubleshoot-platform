<script setup lang="ts">
/**
 * 管理端真浏览器验收页（仅 dev）：挂载产品里同一个 LogCollectAttribution。
 *
 * fixture 由 scripts/gen_log_collect_admin_verify_fixture.py 调用真实 derive() 生成，
 * 因此截图证明的是产品派生逻辑与产品组件，而不是另画一份。
 */
import { computed } from 'vue'
import { formatAcliVersion, formatVersionArchitecture } from '@hci/shared'
import LogCollectAttribution from '../src/views/components/LogCollectAttribution.vue'
import fixture from './mock-admin.json'

const detail = fixture.detail

// 任务进展详情的版本字段验收：与产品组件共用 @hci/shared 的拼接函数。
const isVersionArchScenario =
  new URLSearchParams(window.location.search).get('scenario') === 'version-arch'
const versionArchitectureText = computed(() => formatVersionArchitecture('x86_64', '6.10.0_R2'))
const acliVersionText = computed(() => formatAcliVersion('1.0.1', '2026-09-20 17:22:30'))
</script>

<template>
  <main class="verify-page">
    <header>
      <h1>日志收集管理端 · 链路定界真浏览器验收</h1>
      <p>
        场景 Q2026111300155（P1 已上传、等待 5888 发布归档名）· 数据来自真实
        <code>derive()</code> · 组件来自 <code>LogCollectAttribution.vue</code>
      </p>
    </header>

    <section class="card">
      <h2>1. 详情抽屉顶部「首个异常点」摘要卡</h2>
      <p class="note">客户只看一句话与调用链；判据、证据入口只在管理端展开。</p>
      <LogCollectAttribution
        variant="summary"
        :display="detail.display"
        :error-code="detail.error_code"
        :fallback-trace-id="detail.trace_id"
      />
    </section>

    <section class="card">
      <h2>2. S1-S8 阶段时间线（感叹号里是固定设计说明弹框）</h2>
      <p class="note">阶段状态查不到即 unknown，不伪造；段/组件说明来自同一错误字典，按需弹框查看。</p>
      <LogCollectAttribution variant="panel" :display="detail.display" />
    </section>

    <section v-if="isVersionArchScenario" class="card" data-testid="version-arch-section">
      <h2>3. 任务进展详情：版本架构 / acli版本</h2>
      <p class="note">与客户侧探测卡共用 @hci/shared 的拼接函数，标签与取值口径一致。</p>
      <el-descriptions :column="1" border size="small">
        <el-descriptions-item label="版本架构">{{ versionArchitectureText }}</el-descriptions-item>
        <el-descriptions-item label="acli版本">{{ acliVersionText }}</el-descriptions-item>
      </el-descriptions>
    </section>
  </main>
</template>

<style>
body {
  margin: 0;
  background: #f5f7fa;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}
.verify-page {
  max-width: 1680px;
  margin: 0 auto;
  padding: 20px 24px 40px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.verify-page h1 {
  margin: 0 0 6px;
  font-size: 22px;
}
.verify-page p {
  margin: 0;
  font-size: 13px;
  color: #606266;
}
.verify-page code {
  padding: 1px 5px;
  background: #eef1f6;
  border-radius: 3px;
  font-size: 12px;
}
.card {
  padding: 14px 16px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
}
.card h2 {
  margin: 0 0 8px;
  font-size: 15px;
}
.note {
  margin: 0 0 10px;
  font-size: 12px;
}
</style>