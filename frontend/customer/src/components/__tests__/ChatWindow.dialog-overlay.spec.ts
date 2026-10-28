import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 弹框遮罩必须铺满视口，不能被「内容列限宽」规则命中。
 *
 * 现场坑（1920 屏，PR #1293 引入）：`.chat-window > * { max-width: 860px }` 把
 * el-dialog 就地渲染出来的 `.el-overlay` 也限成了 860px，于是「发现未关闭的工单」
 * 弹框只有左半屏变暗、右半屏照常，看起来像渲染坏了一半。
 *
 * 成因是 el-dialog 未开 append-to-body 时遮罩是 .chat-window 的直接子元素，
 * 而组件根元素会继承父组件的 scoped 属性，`> *` 必然命中它。
 * 这类问题只在真实浏览器里以像素形式暴露（单测环境不加载 scoped 样式），
 * 因此在源码层面钉死约束。
 */
const CWD = process.cwd()

describe('内容列限宽不得命中弹框遮罩', () => {
  const chatWindow = readFileSync(resolve(CWD, 'src/components/ChatWindow.vue'), 'utf-8')
  const globalCss = readFileSync(resolve(CWD, 'src/styles/workspace-panel.css'), 'utf-8')

  it('内容列规则排除 .el-overlay', () => {
    expect(chatWindow).toContain('.chat-window > :not(.el-overlay)')
    // 反证：无条件 `> *` 会把遮罩一起限宽（回归即失败）
    expect(chatWindow).not.toMatch(/\.chat-window\s*>\s*\*\s*\{/)
  })

  it('全局兜底：遮罩的 max-width 不被内容约束压住', () => {
    const rule = globalCss.match(/body \.el-overlay\.el-overlay\s*\{([\s\S]*?)\}/)?.[1] ?? ''
    expect(rule).toMatch(/max-width:\s*none/)
    expect(rule).toMatch(/max-height:\s*none/)
  })
})
