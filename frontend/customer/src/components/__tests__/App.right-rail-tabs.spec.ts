import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 右栏页签（SSH 终端 / 文件下载）结构契约。
 *
 * 背景：文件下载原先嵌在 SSH 终端底部（24px 折叠条），看不见目录也压矮终端。
 * 现在改成右栏两个平级页签，两条不可退让的边界靠源码断言锁住：
 * 1. 右栏必须有两个入口，且「文件下载」只能走 store 的 openSftpPanel()
 *    ——那里才有"未登录先弹统一 SSH 登录框"的门禁，绕过去就等于没有门禁；
 * 2. 终端组件不得再把 SftpPanel 嵌回底部（否则会出现两套文件下载入口）。
 */
const APP = resolve(process.cwd(), 'src/App.vue')
const TERMINAL = resolve(process.cwd(), 'src/components/TerminalPanel.vue')

/** 取出右侧 PanelShell 的整段模板（以 side="right" 为起点） */
function rightPanelBlock(source: string): string {
  const start = source.indexOf('side="right"')
  expect(start).toBeGreaterThan(-1)
  const end = source.indexOf('</PanelShell>', start)
  expect(end).toBeGreaterThan(start)
  return source.slice(start, end)
}

describe('App 右栏页签', () => {
  const appSource = readFileSync(APP, 'utf-8')
  const block = rightPanelBlock(appSource)

  it('右栏提供两个平级入口（SSH 终端 + 文件下载）', () => {
    const railBtns = [...block.matchAll(/class="rail-btn"/g)]
    expect(railBtns).toHaveLength(2)
    expect(block).toContain('content="SSH 终端"')
    expect(block).toContain('content="文件下载"')
  })

  it('文件下载入口走 openSftpPanel（未登录先走统一登录门禁）', () => {
    expect(block).toContain('@click="chatStore.openSftpPanel()"')
    // 反证：不能直接开面板，否则未登录也能进到发不出请求的空列表
    expect(block).not.toMatch(/showTerminalSidebar\s*=\s*true/)
  })

  it('两个页签用 v-show 切换，页签状态由 store 的 rightPanelTab 决定', () => {
    expect(block).toContain('v-show="chatStore.rightPanelTab === \'terminal\'"')
    expect(block).toContain('v-show="chatStore.rightPanelTab === \'sftp\'"')
    // 反证：v-if 会销毁 xterm 实例并断开会话，文件下载正是靠这条会话
    expect(block).not.toContain('v-if="chatStore.rightPanelTab')
  })
})

describe('TerminalPanel 不再内嵌文件下载', () => {
  const source = readFileSync(TERMINAL, 'utf-8')

  it('不再 import / 渲染 SftpPanel', () => {
    expect(source).not.toMatch(/import\s+SftpPanel/)
    expect(source).not.toMatch(/<SftpPanel/)
  })
})
