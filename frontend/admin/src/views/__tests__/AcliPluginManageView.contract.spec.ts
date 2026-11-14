/**
 * acli 插件管理页的架构契约守卫（纯静态，不 mount 组件）。
 *
 * PR #1508 的教训是「页面能跑通」不等于「链路正确」：这里锁死管理页
 * 不出现任何 SSH 凭据 / 目标主机 / bridge 地址 / WS 协议字段，也不直接
 * 构造部署命令或连 bridge；下发只能经管理端 API 交给服务端编排。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const viewSource = readFileSync(
  fileURLToPath(new URL('../AcliPluginManageView.vue', import.meta.url)),
  'utf8',
)
const apiSource = readFileSync(
  fileURLToPath(new URL('../../api/acliPlugin.ts', import.meta.url)),
  'utf8',
)
const templateSource = viewSource.split('<template>')[1] ?? ''

const FORBIDDEN = [
  'password',
  'private_key',
  'node_ip',
  'bridge_url',
  'ws://',
  'wss://',
  'ssh_connect',
  'ssh_exec_process',
  'exec_result',
]

describe('acli 插件管理页架构契约', () => {
  it('页面模板不出现任何运行环境字段或 bridge 协议', () => {
    for (const token of FORBIDDEN) {
      expect(templateSource.toLowerCase()).not.toContain(token)
    }
  })

  it('API 封装只提交插件规格与环境代号', () => {
    for (const token of FORBIDDEN) {
      expect(apiSource.toLowerCase()).not.toContain(token)
    }
    expect(apiSource).toContain('publishPlugin(spec: AcliPluginSpec, envCode: string)')
    expect(apiSource).toContain("client.post(`${BASE}/plugins`, { spec, env_code: envCode })")
  })

  it('保存后通过任务状态接口轮询，由服务端收敛回执', () => {
    expect(viewSource).toContain('publishPlugin(form.value, envCode.value)')
    expect(viewSource).toContain('getDeployment(deployId)')
    expect(viewSource).toContain('downloadTemplate')
  })

  it('新增插件信息模板可下载，内置覆盖需二次确认', () => {
    expect(viewSource).toContain('下载信息模板')
    expect(viewSource).toContain('ElMessageBox.confirm')
    expect(viewSource).toContain('builtin_override')
  })
})
