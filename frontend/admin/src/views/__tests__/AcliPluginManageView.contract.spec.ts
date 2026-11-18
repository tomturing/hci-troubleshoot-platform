/**
 * acli 插件管理页的架构契约守卫（纯静态，不 mount 组件）。
 *
 * 链路模型（与 customer-ui 的 acli_sync / log_collect 一致）：
 *   页面 → 桥（浏览器自己连）→ 已登录 SSH 会话执行
 *   服务端只生成命令、接收回执落库，**不连桥、不持凭据**。
 *
 * 因此这里锁死的是「页面绝不新建 SSH 连接、绝不接触口令/私钥」，
 * 以及「服务端不得重新出现连桥客户端」——上两版分别在两个方向上跑偏过。
 */
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// vitest 的 happy-dom 环境里 import.meta.url 不是 file:// scheme，
// 不能用 fileURLToPath；pnpm 从包目录或 workspace 根启动时 cwd 不同，故两种都兼容。
function readSource(relativePath: string): string {
  for (const base of [process.cwd(), resolve(process.cwd(), 'frontend/admin')]) {
    try {
      return readFileSync(resolve(base, relativePath), 'utf8')
    } catch {
      // 尝试下一个候选根目录
    }
  }
  throw new Error(`找不到契约源文件: ${relativePath}`)
}

const viewSource = readSource('src/views/AcliPluginManageView.vue')
const apiSource = readSource('src/api/acliPlugin.ts')
const bridgeSource = readSource('src/api/acliPluginBridge.ts')
const templateSource = viewSource.split('<template>')[1] ?? ''

// 页面只复用已有会话：凭据字段与「新建连接」的消息一律不允许出现。
const CREDENTIAL_TOKENS = ['password', 'private_key', 'passphrase', 'ssh_connect']

describe('acli 插件管理页架构契约', () => {
  it('页面与 API 封装不出现任何 SSH 凭据字段', () => {
    for (const token of CREDENTIAL_TOKENS) {
      expect(viewSource.toLowerCase()).not.toContain(token)
      expect(apiSource.toLowerCase()).not.toContain(token)
    }
  })

  it('页面模板不出现凭据字段', () => {
    for (const token of CREDENTIAL_TOKENS) {
      expect(templateSource.toLowerCase()).not.toContain(token)
    }
  })

  it('执行由浏览器发起：页面必须经 acliPluginBridge 连桥执行并回报结果', () => {
    expect(viewSource).toContain("from '@/api/acliPluginBridge'")
    expect(viewSource).toContain('executeDeployPlan(')
    expect(viewSource).toContain('listBridgeSessions(')
    expect(viewSource).toContain('reportDeployResult(')
  })

  it('桥通道只允许 list_sessions 与 ssh_exec_process，不允许建会话', () => {
    expect(bridgeSource).toContain("type: 'list_sessions'")
    expect(bridgeSource).toContain("type: 'ssh_exec_process'")
    for (const token of CREDENTIAL_TOKENS) {
      expect(bridgeSource.toLowerCase()).not.toContain(token)
    }
  })

  it('API 封装只提交插件规格与会话标识，不含桥地址', () => {
    expect(apiSource).toContain('publishPlugin(')
    expect(apiSource).toContain('case_id: session.caseId')
    expect(apiSource).toContain('node_ip: session.nodeIp')
    expect(apiSource).not.toContain('bridge_url')
    expect(apiSource).not.toContain('ws://')
    expect(apiSource).not.toContain('listSessions')
  })

  it('服务端不得重新出现「主动连桥」的客户端与配置', () => {
    for (const base of [process.cwd(), resolve(process.cwd(), '../..'), resolve(process.cwd(), '../../..')]) {
      const bridgeModule = resolve(base, 'backend/case-service/app/services/acli_plugin_bridge.py')
      const relayModule = resolve(base, 'backend/case-service/app/services/acli_plugin_relay.py')
      if (existsSync(resolve(base, 'backend/case-service'))) {
        expect(existsSync(bridgeModule)).toBe(false)
        expect(existsSync(relayModule)).toBe(false)
        break
      }
    }
  })

  it('新增插件信息模板可下载，内置覆盖需二次确认', () => {
    expect(viewSource).toContain('下载信息模板')
    expect(viewSource).toContain('ElMessageBox.confirm')
    expect(viewSource).toContain('builtin_override')
  })
})
