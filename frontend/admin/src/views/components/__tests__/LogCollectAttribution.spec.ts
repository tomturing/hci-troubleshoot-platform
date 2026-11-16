/**
 * 链路定界组件单测：阶段状态颜色不替代文字，固定设计说明按需弹框。
 */
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'
import LogCollectAttribution from '../LogCollectAttribution.vue'

const makeStage = (key: string, status: string, label: string) => ({
  key,
  label: `环节${key}`,
  status,
  status_label: label,
  criteria: '客观判据',
  fault_code: null,
  customer_text: null,
  scope_label: '本地',
  scopes: [],
  components: [],
})

const display = {
  phase: 'settled',
  phase_label: '已结束',
  conclusion: 'partial',
  conclusion_label: '已交付 · 有疑点',
  conclusion_tone: 'warning',
  conditions: [],
  progress_percent: 100,
  trace_id: 'trace-1',
  stages: [
    makeStage('S1', 'unknown', '未知'),
    makeStage('S2', 'success', '成功'),
    makeStage('S3', 'failed', '失败'),
  ],
}

async function mountPanel() {
  const wrapper = mount(LogCollectAttribution, {
    props: { variant: 'panel', display },
    global: { plugins: [ElementPlus] },
  })
  await flushPromises()
  return wrapper
}

describe('LogCollectAttribution', () => {
  it('阶段状态同时给出文字标签与颜色类名', async () => {
    const wrapper = await mountPanel()
    const tags = wrapper.findAll('[data-testid="stage-status"]')
    expect(tags.map((item) => item.text().trim())).toEqual(['未知', '成功', '失败'])
    // 未知状态带 tooltip，testid 落在触发器上，颜色类名在实际 el-tag 上。
    const tagClasses = tags.map((item) => {
      const tag = item.find('.el-tag')
      return (tag.exists() ? tag : item).classes().join(' ')
    })
    expect(tagClasses[0]).toContain('el-tag--info')
    expect(tagClasses[1]).toContain('el-tag--success')
    expect(tagClasses[2]).toContain('el-tag--danger')
    wrapper.unmount()
  })

  it('感叹号点击才展示固定设计说明，且不与工单结论混淆', async () => {
    const wrapper = await mountPanel()
    expect(document.body.textContent).not.toContain('三段五组件设计说明')
    await wrapper.get('[data-testid="attribution-help"]').trigger('click')
    await flushPromises()
    const body = document.body.textContent ?? ''
    expect(body).toContain('三段五组件设计说明（固定契约，与当前工单无关）')
    expect(body).toContain('固定枚举')
    expect(document.querySelectorAll('.ref-card').length).toBe(5)
    wrapper.unmount()
  })
})
