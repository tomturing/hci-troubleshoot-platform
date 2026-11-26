/**
 * interactiveInputRecorder.ts
 * 模式二（实时交互终端）的输入命令提取与安全脱敏录制器。
 *
 * 设计原理（第一性原理）：
 * 1. 模式二中用户按键直接透传远端 PTY，Tab 自动补全、方向键 ↑↓ 翻历史、左右编辑后的
 *    最终真实命令呈现在 xterm 屏幕缓冲区光标所在逻辑行中；
 * 2. 在回车（\r/\n）发往远端前的瞬间读取当前逻辑行并剥离 shell prompt，即可得到与模式一
 *    同构的完整命令文本写入 /api/terminal/operations，使 TerminalReplay 的命令列表、
 *    时间轴回放、搜索、导出能力与模式一完全一致；
 * 3. 安全红线（对抗性审查）：当屏幕处于密码/口令输入提示（如 sudo/ssh/passwd，远端关闭回显）
 *    时，绝不落库任何按键明文；当处于全屏交互程序（vim/top/less，alternate buffer）时，
 *    不将光标导航单键写入命令列表，避免污染历史回放并打爆写接口。
 */

export interface XtermBufferLineLike {
  isWrapped?: boolean
  translateToString(trimRight?: boolean): string
}

export interface XtermBufferLike {
  type?: string
  baseY?: number
  cursorY?: number
  length?: number
  getLine(y: number): XtermBufferLineLike | undefined
}

export interface XtermLike {
  buffer?: {
    active?: XtermBufferLike
  }
}

export interface ScreenLineSnapshot {
  /** 当前是否处于全屏备用缓冲区（vim / top / less 等） */
  isAlternate: boolean
  /** 光标所在的完整逻辑行文本（合并软折行） */
  line: string
  /** 是否成功从 xterm 缓冲区读取 */
  available: boolean
}

export interface RecordedInteractiveInput {
  /** 要写入 terminal_operations 的命令或控制标识（如 `ls -la`、`^C`） */
  command: string
  /** 是否为控制信号（如 Ctrl+C / Ctrl+D / Ctrl+Z），非普通命令提交 */
  isControl: boolean
}

/** 敏感口令提示模式：命中时必须阻止任何按键或缓冲落库 */
const SENSITIVE_PROMPT_PATTERNS: RegExp[] = [
  /(?:password|passwd|passphrase|pin\s*code|enter\s+pin|verification\s+code|auth(?:entication)?\s+code|secret|token)\b[^\n]*[:：]\s*$/i,
  /(?:密码|口令|密钥短语|验证码)[^\n]*[:：]\s*$/,
]

/**
 * 常见远端 Shell 提示符前缀匹配（按具体到宽泛排序）：
 * - `[root@hci-node01 /sf/log]# cmd`
 * - `(venv) root@node:/var/log$ cmd`
 * - `root@node:~# cmd`
 * - `bash-5.1# cmd` / `sh-4.2$ cmd`
 * - 极简提示符 `# cmd` / `$ cmd` / `> cmd`
 */
const SHELL_PROMPT_PREFIX_PATTERNS: RegExp[] = [
  /^\s*(?:\([^)]+\)\s*)?\[[^\]]+\]\s*[#$%>]\s*/,
  /^\s*(?:\([^)]+\)\s*)?[^\s#$%>]+@[^\s#$%>]+(?::[^\s#$%>]*)?\s*[#$%>]\s*/,
  /^\s*(?:bash|sh|zsh|ash|dash|ksh)(?:-[\d.]+)?\s*[#$%>]\s*/,
  /^\s*[#$%>]\s+/,
]

/**
 * 从 xterm 活动缓冲区读取光标所在的完整逻辑行（包含自动折行）。
 */
export function extractActiveLogicalLine(xterm: XtermLike | null | undefined): ScreenLineSnapshot {
  const active = xterm?.buffer?.active
  if (!active || typeof active.getLine !== 'function') {
    return { isAlternate: false, line: '', available: false }
  }
  if (active.type === 'alternate') {
    return { isAlternate: true, line: '', available: true }
  }

  const cursorRow = (active.baseY ?? 0) + (active.cursorY ?? 0)
  const totalLines = active.length ?? cursorRow + 1
  if (cursorRow < 0 || cursorRow >= totalLines) {
    return { isAlternate: false, line: '', available: true }
  }

  let startRow = cursorRow
  while (startRow > 0) {
    const cur = active.getLine(startRow)
    if (!cur?.isWrapped) break
    startRow--
  }

  let endRow = cursorRow
  while (endRow + 1 < totalLines) {
    const next = active.getLine(endRow + 1)
    if (!next?.isWrapped) break
    endRow++
  }

  let combined = ''
  for (let row = startRow; row <= endRow; row++) {
    const lineObj = active.getLine(row)
    if (!lineObj) continue
    combined += lineObj.translateToString(row === endRow)
  }

  return { isAlternate: false, line: combined, available: true }
}

/**
 * 判断当前提示行是否为密码/口令等敏感输入提示。
 */
export function isSensitivePromptLine(line: string): boolean {
  const trimmed = line.trimEnd()
  if (!trimmed) return false
  return SENSITIVE_PROMPT_PATTERNS.some((pattern) => pattern.test(trimmed))
}

/**
 * 从带有 shell prompt 的整行文本中剥离提示符，提取实际执行的命令。
 */
export function extractCommandFromPromptLine(line: string): string {
  const cleaned = line.replace(/\r/g, '').trimEnd()
  if (!cleaned.trim()) return ''

  for (const pattern of SHELL_PROMPT_PREFIX_PATTERNS) {
    const match = cleaned.match(pattern)
    if (match) {
      return cleaned.slice(match[0].length).trim()
    }
  }

  // 未命中标准提示符时，过滤孤立的提示符符号，其余按原始文本返回
  const trimmed = cleaned.trim()
  if (/^[#$%>]$/.test(trimmed)) return ''
  return trimmed
}

/**
 * 模式二交互输入跟踪器：
 * 将 xterm.onData 产生的字节流转化为与模式一同构的命令行/控制键录制事件。
 */
export class InteractiveInputTracker {
  private localLine = ''
  private hasNavigationOrCompletion = false
  private inEscapeSeq = false

  /** 重置内部状态（断开连接、清屏或切换会话时调用） */
  reset(): void {
    this.localLine = ''
    this.hasNavigationOrCompletion = false
    this.inEscapeSeq = false
  }

  /**
   * 消费一段即将发往远端 PTY 的输入数据，返回本次需要落库录制的命令或控制事件。
   * 必须在把 data 发给远端之前调用，以确保 xterm 屏幕上仍停留在回车前的完整命令行。
   */
  consume(data: string, xterm?: XtermLike | null): RecordedInteractiveInput[] {
    if (!data) return []

    const snapshot = extractActiveLogicalLine(xterm)
    // 全屏交互程序（vim / top / less 等）内不产生命令列表条目，防止噪声与写接口风暴
    if (snapshot.isAlternate) {
      this.reset()
      return []
    }

    // 剥离 bracketed paste 包裹标记（\x1b[200~ 与 \x1b[201~）
    const normalized = data.replace(/\x1b\[200~/g, '').replace(/\x1b\[201~/g, '')
    const events: RecordedInteractiveInput[] = []
    let lineCommitIndex = 0

    for (let i = 0; i < normalized.length; i++) {
      const ch = normalized[i]
      const code = ch.charCodeAt(0)

      // 跳过 CSI / SS3 / ESC 转义序列（上下左右方向键、Home/End、功能键等）
      if (this.inEscapeSeq) {
        if (code >= 0x40 && code <= 0x7e) {
          this.inEscapeSeq = false
        }
        continue
      }
      if (ch === '\x1b') {
        this.inEscapeSeq = true
        this.hasNavigationOrCompletion = true
        // 单独 Esc 后若紧跟非 '[' / 'O'，只消耗下一字符即可
        const next = normalized[i + 1]
        if (!next) {
          this.inEscapeSeq = false
        } else if (next !== '[' && next !== 'O') {
          this.inEscapeSeq = false
          i++
        } else {
          i++ // 跳过 '[' 或 'O'，等终结符 @-~
        }
        continue
      }

      // 回车/换行：提交一条完整命令
      if (ch === '\r' || ch === '\n') {
        // 跳过 CRLF 的第二个字符，避免重复提交同一条命令
        if (ch === '\r' && normalized[i + 1] === '\n') {
          i++
        }
        const committed = this.commitCurrentLine(snapshot, lineCommitIndex === 0)
        lineCommitIndex++
        if (committed) {
          events.push({ command: committed, isControl: false })
        }
        this.localLine = ''
        this.hasNavigationOrCompletion = false
        continue
      }

      // 控制信号：Ctrl+C / Ctrl+D / Ctrl+Z 录制可读标记并清空当前行
      if (ch === '\x03') {
        this.localLine = ''
        this.hasNavigationOrCompletion = false
        events.push({ command: '^C', isControl: true })
        continue
      }
      if (ch === '\x04') {
        this.localLine = ''
        this.hasNavigationOrCompletion = false
        events.push({ command: '^D', isControl: true })
        continue
      }
      if (ch === '\x1a') {
        this.localLine = ''
        this.hasNavigationOrCompletion = false
        events.push({ command: '^Z', isControl: true })
        continue
      }

      // Tab 补全：标记由远端补全改写了当前行，回车时优先采信屏幕缓冲区
      if (ch === '\t') {
        this.hasNavigationOrCompletion = true
        continue
      }

      // 行编辑控制键：退格 / Ctrl+U / Ctrl+W
      if (ch === '\x7f' || ch === '\b') {
        this.localLine = this.localLine.slice(0, -1)
        continue
      }
      if (ch === '\x15') {
        this.localLine = ''
        continue
      }
      if (ch === '\x17') {
        this.localLine = this.localLine.replace(/\S+\s*$/, '')
        continue
      }

      // 其余不可见控制字符忽略
      if (code < 0x20) {
        continue
      }

      this.localLine += ch
    }

    return events
  }

  private commitCurrentLine(snapshot: ScreenLineSnapshot, canUseScreenSnapshot: boolean): string {
    // 安全红线：一旦屏幕停留在密码/口令提示行，绝不落库任何明文
    if (snapshot.available && isSensitivePromptLine(snapshot.line)) {
      return ''
    }

    if (canUseScreenSnapshot && snapshot.available && snapshot.line.trim()) {
      const fromScreen = extractCommandFromPromptLine(snapshot.line)
      if (fromScreen) {
        return fromScreen
      }
      // 屏幕存在非空提示行但剥离后为空：
      // - 若使用过方向键/Tab 或本地无未回显字符，说明用户敲了空回车，不录制；
      // - 若本地有字符且屏幕尚未回显（例如极快输入），允许安全回退。
      if (this.hasNavigationOrCompletion || !this.localLine.trim()) {
        return ''
      }
    }

    return this.localLine.trim()
  }
}
