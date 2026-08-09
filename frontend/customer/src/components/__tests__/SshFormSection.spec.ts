import { describe, expect, it, beforeEach, afterEach } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import { nextTick } from 'vue'
import SshFormSection from '../SshFormSection.vue'

const emptyForm = () => ({
  host: '',
  port: '22',
  username: '',
  password: '',
  privateKey: '',
  passphrase: '',
  executionMode: undefined as 'sim-ssh' | undefined,
  testRunId: '',
})

describe('SshFormSection 仿真租约默认值与重试保留', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => localStorage.clear())

  it('切换到仿真租约时使用 hci-sim 默认 host/port/user', async () => {
    const ordinaryForm = { ...emptyForm(), password: 'ordinary-password', testRunId: 'stale-run' }
    const wrapper = shallowMount(SshFormSection, {
      props: { sshForm: ordinaryForm, authType: 'password', allowLease: true },
    })

    const state = (wrapper.getCurrentComponent() as any).setupState
    state.localAuthType = 'lease'
    await nextTick()

    expect(state.localForm.host).toBe('172.28.24.21')
    expect(state.localForm.port).toBe('2222')
    expect(state.localForm.username).toBe('sim')
    expect(state.localForm.executionMode).toBe('sim-ssh')
    expect(state.localForm.password).toBe('')
    expect(state.localForm.testRunId).toBe('')
    wrapper.unmount()
  })

  it('重试重新挂载表单时不覆盖本次填写的连接信息', () => {
    localStorage.setItem('hci_last_ssh_config', JSON.stringify({
      host: '172.28.25.1', port: '22', username: 'admin',
    }))
    const userForm = { ...emptyForm(), host: '172.28.24.21', port: '22001', username: 'sim', password: 'lease' }
    const wrapper = shallowMount(SshFormSection, {
      props: { sshForm: userForm, authType: 'lease', allowLease: true },
    })

    const state = (wrapper.getCurrentComponent() as any).setupState
    expect(state.localForm.host).toBe('172.28.24.21')
    expect(state.localForm.port).toBe('22001')
    expect(state.localForm.username).toBe('sim')
    expect(state.localForm.password).toBe('lease')
    wrapper.unmount()
  })
})
