<script setup lang="ts">
/**
 * 管理端真浏览器验收页（仅 dev）：挂载产品里同一个 LogCollectAttribution。
 *
 * fixture 由 scripts/gen_log_collect_admin_verify_fixture.py 调用真实 derive() 生成，
 * 因此截图证明的是产品派生逻辑与产品组件，而不是另画一份。
 */
import LogCollectAttribution from '../src/views/components/LogCollectAttribution.vue'
import fixture from './mock-admin.json'

const detail = fixture.detail
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
      <h2>2. 「链路定界」页签：段/组件表（含问题描述、建议动作）+ S1-S8 时间线</h2>
      <p class="note">问题描述与建议动作直接读错误字典，与客户弹框同源；阶段状态查不到即 unknown，不伪造。</p>
      <LogCollectAttribution variant="panel" :display="detail.display" />
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