/**
 * Markdown 渲染工具
 * 使用 marked 解析 + DOMPurify 做 XSS 防护
 */
import { marked, Renderer } from 'marked'
import DOMPurify from 'dompurify'

/**
 * 自定义渲染器：保护围栏代码块内换行不被 breaks:true 转为 <br>
 * breaks:true 会将普通文本中的单个 \n 转为 <br>，但代码块内不应该处理。
 * 通过自定义 code renderer 直接输出转义后的代码内容，跳过 marked 的换行处理。
 */
const renderer = new Renderer()
renderer.code = function ({ text, lang }: { text: string; lang?: string }) {
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  const langAttr = lang ? ` class="language-${lang}"` : ''
  return `<pre><code${langAttr}>${escaped}</code></pre>\n`
}

// 配置 marked 选项
marked.setOptions({
  breaks: true, // 支持 GFM 换行（普通文本单个 \n → <br>）
  gfm: true,    // 启用 GitHub Flavored Markdown
})
marked.use({ renderer })

/**
 * 渲染 Markdown 为安全的 HTML
 * @param text Markdown 文本
 * @returns 安全的 HTML 字符串
 */
export function renderMarkdown(text: string): string {
  if (!text) return ''

  // 1. 使用 marked 解析 Markdown
  const rawHtml = marked.parse(text) as string

  // 2. 使用 DOMPurify 进行 XSS 防护
  // 配置白名单，允许安全的 HTML 标签和属性
  const cleanHtml = DOMPurify.sanitize(rawHtml, {
    ALLOWED_TAGS: [
      // 文本格式化
      'p', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'del', 'ins',
      // 标题
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      // 列表
      'ul', 'ol', 'li',
      // 引用
      'blockquote',
      // 代码
      'code', 'pre',
      // 链接
      'a',
      // 表格
      'table', 'thead', 'tbody', 'tr', 'th', 'td',
      // 其他
      'hr', 'span', 'div',
    ],
    ALLOWED_ATTR: [
      'href', 'title', 'target', 'rel', // 链接属性
      'class', // 允许 class 用于样式
      'data-*', // 允许 data-* 属性用于扩展
    ],
    // 为所有链接添加安全属性
    ADD_ATTR: ['target', 'rel'],
  })

  return cleanHtml
}

/**
 * 代码块语言类型
 */
export type CodeBlockLanguage = 'bash' | 'sh' | 'shell' | 'python' | 'javascript' | 'typescript' | 'json' | 'yaml' | 'sql' | 'plaintext'

/**
 * 代码块信息
 */
export interface CodeBlock {
  /** 代码内容 */
  code: string
  /** 语言标识 */
  language: string
  /** 是否为命令块（bash/sh/shell） */
  isCommand: boolean
}

/**
 * 从 Markdown 文本中提取所有代码块
 * 用于后续挂载命令卡片组件
 * @param text Markdown 文本
 * @returns 代码块数组
 */
export function extractCodeBlocks(text: string): CodeBlock[] {
  if (!text) return []

  // 正则匹配代码块（兼容流式输出时未闭合的情况）：```language\ncode(```|EOF)
  const codeBlockRegex = /```(?:[ \t]*)([\w-]*)(?:\r?\n)([\s\S]*?)(?:```|$)/g
  const blocks: CodeBlock[] = []
  let match

  while ((match = codeBlockRegex.exec(text)) !== null) {
    const language = match[1] || 'plaintext'
    const code = match[2].trim()
    const isCommand = ['bash', 'sh', 'shell'].includes(language.toLowerCase())

    blocks.push({
      code,
      language,
      isCommand,
    })
  }

  return blocks
}

/**
 * 判断语言标识是否为命令类型
 */
export function isCommandLanguage(lang: string): boolean {
  return ['bash', 'sh', 'shell'].includes(lang.toLowerCase())
}