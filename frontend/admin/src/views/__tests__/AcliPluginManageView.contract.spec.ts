/**
 * acli 插件管理页的架构契约守卫（纯静态，不 mount 组件）。
 *
 * 链路收口定稿（2026-11-18 v2.0，D1）后的模型：
 *   页面只维护规格（列表/新建/编辑/模板下载）→ 服务端校验并落不可变 revision；
 *   插件到达节点完全由客户侧 acli_sync 第二阶段（票据拉平台 manifest）驱动。
 *
 * 因此这里锁死的是「页面不再出现任何下发/连桥/任务轮询形态」；而「服务端 acli
 * 插件模块不再出现连桥客户端」那条住在 case-service 单测里（见下）——上两版分别在
 * 两个方向上跑偏过。
 *
 * 守卫按**形态**扫描而不是按被删文件的名字断言：旧链路的标识符必须在代码里 0 命中。
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { basename, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// vitest 的 happy-dom 环境里 import.meta.url 不是 file:// scheme，
// 不能用 fileURLToPath；pnpm 从包目录或 workspace 根启动时 cwd 不同，故全部候选都试。
const ROOTS = [process.cwd(), resolve(process.cwd(), 'frontend/admin'), resolve(process.cwd(), '../..'), resolve(process.cwd(), '../../..')]

function locate(relativePath: string): string | null {
  for (const base of ROOTS) {
    const candidate = resolve(base, relativePath)
    if (existsSync(candidate)) return candidate
  }
  return null
}

function readSource(relativePath: string): string {
  const found = locate(relativePath)
  if (!found) throw new Error(`找不到契约源文件: ${relativePath}`)
  return readFileSync(found, 'utf-8')
}

/** 递归列出待扫源码；跳过依赖/产物，也跳过本守卫自己（它必须能写下被禁的名字）。 */
function listSources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (['node_modules', 'dist', '__pycache__', '__tests__', '.git'].includes(entry.name)) continue
      listSources(resolve(dir, entry.name), out)
    } else if (/\.(py|ts|vue)$/.test(entry.name)) {
      out.push(resolve(dir, entry.name))
    }
  }
  return out
}

// 「主动连桥 / 下发编排」的形态标记：用通用协议与动词，而不是被删模块的文件名——
// 守卫自己不该成为旧名字存续的理由（D1 的 grep 零残留要能真的 0 命中）。
const OUTBOUND_TOKENS = ['ssh_connect', 'ws://', 'wss://', 'paramiko', 'websockets', 'deploy', 'relay', 'lease']

function findOutboundTokens(files: string[]): string[] {
  return files.flatMap((file) => {
    const source = readFileSync(file, 'utf-8').toLowerCase()
    return OUTBOUND_TOKENS.filter((token) => source.includes(token)).map((token) => `${basename(file)} → ${token}`)
  })
}

const viewSource = readSource('src/views/AcliPluginManageView.vue')
const apiSource = readSource('src/api/acliPlugin.ts')
const templateSource = viewSource.split('<template>')[1] ?? ''

// 页面只维护规格：凭据字段与「新建连接/连桥执行」的形态一律不允许出现。
const CREDENTIAL_TOKENS = ['password', 'private_key', 'passphrase', 'ssh_connect']
const DEPLOY_TOKENS = [
  'acliPluginBridge',
  'executeDeployPlan',
  'listBridgeSessions',
  'reportDeployResult',
  'publishPlugin',
  'listDeployments',
  'getDeployment',
  'deploy_id',
  'ssh_exec_process',
  'ws://',
]

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

  it('下发链路形态零残留：页面与 API 不连桥、不执行、不轮询任务', () => {
    for (const token of DEPLOY_TOKENS) {
      expect(viewSource).not.toContain(token)
      expect(apiSource).not.toContain(token)
    }
  })

  it('API 封装只提交插件规格，不含会话/桥地址', () => {
    expect(apiSource).toContain('saveSpec(')
    expect(apiSource).toContain('`${BASE}/specs`')
    expect(apiSource).not.toContain('case_id')
    expect(apiSource).not.toContain('node_ip')
    expect(apiSource).not.toContain('bridge_url')
  })

  // 服务端形态扫描不在本文件：前端门禁 job 是 sparse-checkout（只检出 frontend/），
  // 从这里去读 backend/ 在 CI 里拿不到源文件。同一条不变量住在
  // backend/case-service/tests/unit/test_acli_plugin_admin.py
  // 的 test_server_acli_plugin_modules_contain_no_outbound_bridge_forms。

  it('管理端插件页面与 API 不重新长出浏览器直连桥的通道', () => {
    const srcRoot = locate('frontend/admin/src')
    expect(srcRoot).toBeTruthy()
    const files = listSources(srcRoot!).filter((file) => /acliplugin/i.test(basename(file)))
    expect(files.length).toBeGreaterThanOrEqual(2)
    expect(findOutboundTokens(files)).toEqual([])
  })

  it('新增插件信息模板可下载，内置覆盖需二次确认', () => {
    expect(viewSource).toContain('下载信息模板')
    expect(viewSource).toContain('ElMessageBox.confirm')
    expect(viewSource).toContain('builtin_override')
  })

  // 收口后平台没有任何节点侧回传通道（回执端点与下发表已删）。“节点是否已对齐”是个
  // 无源字段：留着它就只会恒显一个永远不绿的告警标签，把“平台不知道”做成“平台知道且不行”。
  it('页面不假称知道节点收敛状态（无回传通道就没有这一列）', () => {
    for (const token of ['current_spec_hash', '节点已对齐', '等待客户同步']) {
      expect(viewSource).not.toContain(token)
      expect(apiSource).not.toContain(token)
    }
    // 但必须告诉管理员去哪里看真实结果，不能把问题静默掉。
    expect(viewSource).toContain('插件同步')
  })
})
