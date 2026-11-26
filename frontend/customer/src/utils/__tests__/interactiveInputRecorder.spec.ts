import { describe, expect, it } from 'vitest'
import {
  InteractiveInputTracker,
  extractActiveLogicalLine,
  extractCommandFromPromptLine,
  isSensitivePromptLine,
  type XtermLike,
} from '@/utils/interactiveInputRecorder'

function createFakeXterm(lines: Array<{ text: string; isWrapped?: boolean }>, cursorY: number, type = 'normal'): XtermLike {
  return {
    buffer: {
      active: {
        type,
        baseY: 0,
        cursorY,
        length: lines.length,
        getLine: (y: number) => {
          const item = lines[y]
          if (!item) return undefined
          return {
            isWrapped: Boolean(item.isWrapped),
            translateToString: (trimRight?: boolean) => (trimRight ? item.text.trimEnd() : item.text),
          }
        },
      },
    },
  }
}

describe('interactiveInputRecorder - 模式二实时交互输入提取与安全脱敏', () => {
  it('能剥离常见 Linux / HCI Shell 提示符并还原命令', () => {
    expect(extractCommandFromPromptLine('[root@hci-node01 /sf/log]# ls -la /sf/log/today')).toBe('ls -la /sf/log/today')
    expect(extractCommandFromPromptLine('root@hci-node01:~# df -h')).toBe('df -h')
    expect(extractCommandFromPromptLine('(venv) ops@host:/app$ uv run pytest')).toBe('uv run pytest')
    expect(extractCommandFromPromptLine('bash-5.1# acli status')).toBe('acli status')
    expect(extractCommandFromPromptLine('# whoami')).toBe('whoami')
    expect(extractCommandFromPromptLine('$ uname -a')).toBe('uname -a')
    expect(extractCommandFromPromptLine('[root@hci-node01 ~]#   ')).toBe('')
  })

  it('能合并自动折行（soft-wrap）的逻辑命令行', () => {
    const xterm = createFakeXterm(
      [
        { text: 'root@hci:~# acli log_collect create --modules ', isWrapped: false },
        { text: 'core,network --date-from 2026-11-26   ', isWrapped: true },
      ],
      1,
    )
    const snap = extractActiveLogicalLine(xterm)
    expect(snap.isAlternate).toBe(false)
    expect(extractCommandFromPromptLine(snap.line)).toBe(
      'acli log_collect create --modules core,network --date-from 2026-11-26',
    )
  })

  it('Tab 自动补全与方向键 ↑ 历史召回后按回车，按屏幕缓冲区记录最终完整命令', () => {
    const tracker = new InteractiveInputTracker()
    // 用户只敲了前缀 + Tab，远端 PTY 回显把屏幕行补全成了完整路径
    expect(tracker.consume('cd /sf/l\t')).toEqual([])
    const xtermAfterTab = createFakeXterm(
      [{ text: '[root@hci-node01 ~]# cd /sf/log/today/' }],
      0,
    )
    expect(tracker.consume('\r', xtermAfterTab)).toEqual([
      { command: 'cd /sf/log/today/', isControl: false },
    ])

    // 用户按方向键 ↑ 调出历史命令并回车
    expect(tracker.consume('\x1b[A')).toEqual([])
    const xtermAfterHistory = createFakeXterm(
      [{ text: '[root@hci-node01 today]# tail -n 50 kernel.log' }],
      0,
    )
    expect(tracker.consume('\r', xtermAfterHistory)).toEqual([
      { command: 'tail -n 50 kernel.log', isControl: false },
    ])
  })

  it('安全红线：密码/口令提示行绝不录制任何明文', () => {
    expect(isSensitivePromptLine('[sudo] password for root: ')).toBe(true)
    expect(isSensitivePromptLine("root@10.0.0.1's password:")).toBe(true)
    expect(isSensitivePromptLine('Enter passphrase for key /root/.ssh/id_rsa: ')).toBe(true)
    expect(isSensitivePromptLine('请输入密码：')).toBe(true)
    expect(isSensitivePromptLine('[root@hci ~]# echo password')).toBe(false)

    const tracker = new InteractiveInputTracker()
    tracker.consume('SuperSecret!123')
    const passwordScreen = createFakeXterm(
      [{ text: '[sudo] password for root: ' }],
      0,
    )
    expect(tracker.consume('\r', passwordScreen)).toEqual([])
  })

  it('全屏交互程序（vim / top / less，alternate buffer）内不产生命令列表噪音', () => {
    const tracker = new InteractiveInputTracker()
    const vimScreen = createFakeXterm([{ text: '~' }], 0, 'alternate')
    expect(tracker.consume('jjj:wq\r', vimScreen)).toEqual([])
  })

  it('控制信号 Ctrl+C / Ctrl+D / Ctrl+Z 录制可读标记并清空当前输入缓冲', () => {
    const tracker = new InteractiveInputTracker()
    tracker.consume('sleep 100')
    expect(tracker.consume('\x03')).toEqual([{ command: '^C', isControl: true }])
    expect(tracker.consume('\x04')).toEqual([{ command: '^D', isControl: true }])
    expect(tracker.consume('\x1a')).toEqual([{ command: '^Z', isControl: true }])
  })

  it('支持 bracketed paste 多行粘贴与 CRLF 去重', () => {
    const tracker = new InteractiveInputTracker()
    const events = tracker.consume('\x1b[200~uname -a\r\ndf -h\n\x1b[201~')
    expect(events).toEqual([
      { command: 'uname -a', isControl: false },
      { command: 'df -h', isControl: false },
    ])
  })

  it('屏幕缓冲不可用时本地行编辑（退格 / Ctrl+U / Ctrl+W）兜底正确', () => {
    const tracker = new InteractiveInputTracker()
    tracker.consume('lss\x7f -la')
    expect(tracker.consume('\r')).toEqual([{ command: 'ls -la', isControl: false }])

    tracker.consume('wrong cmd\x15whoami')
    expect(tracker.consume('\r')).toEqual([{ command: 'whoami', isControl: false }])

    tracker.consume('cat /tmp/bad\x17/etc/hosts')
    expect(tracker.consume('\r')).toEqual([{ command: 'cat /etc/hosts', isControl: false }])
  })
})
