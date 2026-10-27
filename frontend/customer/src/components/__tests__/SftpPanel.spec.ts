import { describe, it, expect, vi, beforeEach } from 'vitest'
import { h, reactive } from 'vue'
import { mount } from '@vue/test-utils'
import SftpPanel from '@/components/SftpPanel.vue'

/**
 * SftpPanel 交互契约（SSH 终端下方的文件下载面板）。
 *
 * 三条不可退让的边界：
 * 1. 前端只能提交“远端路径”，面板里不存在任何本地路径输入；
 * 2. 目录与文件的行点击语义必须分开（目录=进入，文件=下载），点错就是白跑一次传输；
 * 3. 下载进行中不得重复发起（bridge 侧有并发上限，重复点击只会拿到 too_many_tasks）。
 */
let mockStore: any

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

const ElInput = {
  name: 'ElInput',
  props: ['modelValue', 'disabled', 'placeholder', 'size', 'type', 'rows'],
  emits: ['update:modelValue'],
  setup(props: any, { emit, attrs }: any) {
    return () =>
      h('input', {
        class: ['input-stub', attrs.class],
        value: props.modelValue,
        disabled: props.disabled,
        placeholder: props.placeholder,
        onInput: (e: any) => emit('update:modelValue', e.target.value),
        onKeydown: (e: any) => attrs.onKeydown?.(e),
        onFocus: () => attrs.onFocus?.(),
      })
  },
}

const ElButton = {
  name: 'ElButton',
  props: ['disabled', 'type', 'text', 'size', 'loading'],
  setup(props: any, { slots, attrs }: any) {
    return () =>
      h(
        'button',
        {
          class: ['btn-stub', attrs.class],
          disabled: props.disabled,
          title: attrs.title,
          onClick: (e: any) => !props.disabled && attrs.onClick?.(e),
        },
        slots.default?.(),
      )
  },
}

const ElProgress = {
  name: 'ElProgress',
  props: ['percentage', 'status', 'stroke-width', 'show-text'],
  setup(props: any) {
    return () => h('div', { class: 'progress-stub', 'data-percentage': props.percentage })
  },
}

const passthrough = (name: string, tag = 'div') => ({
  name,
  setup(_: any, { slots }: any) {
    return () => h(tag, slots.default?.())
  },
})

function baseStore(overrides: Record<string, unknown> = {}) {
  return reactive({
    sshConnectionState: 'connected',
    sftpPanelVisible: true,
    sftpRemoteDir: '/var/log',
    sftpParentDir: '/var',
    sftpEntries: [],
    sftpDownloadDir: '/home/engineer/Downloads/hci-sftp',
    sftpListLoading: false,
    sftpListError: '',
    sftpListTruncated: false,
    sftpTransfer: null,
    toggleSftpPanel: vi.fn(),
    sftpList: vi.fn().mockResolvedValue(undefined),
    sftpDownload: vi.fn(),
    sftpCancelDownload: vi.fn(),
    ...overrides,
  })
}

function mountPanel() {
  return mount(SftpPanel, {
    global: {
      stubs: {
        ElInput,
        ElButton,
        ElProgress,
        ElTag: passthrough('ElTag', 'span'),
        ElTooltip: passthrough('ElTooltip'),
        ElIcon: passthrough('ElIcon', 'i'),
      },
    },
  })
}

describe('SftpPanel 文件下载面板', () => {
  beforeEach(() => {
    mockStore = baseStore()
  })

  it('展示本机落地目录，且面板里不存在任何“本地路径”输入项', () => {
    const wrapper = mountPanel()
    expect(wrapper.text()).toContain('/home/engineer/Downloads/hci-sftp')
    // 反证：只有远端目录与远端文件两个输入框，落点由 bridge 决定
    expect(wrapper.findAll('input.input-stub')).toHaveLength(2)
    wrapper.unmount()
  })

  it('点击标题切换展开状态', async () => {
    const wrapper = mountPanel()
    await wrapper.find('button.sftp-toggle').trigger('click')
    expect(mockStore.toggleSftpPanel).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('目录行点击进入目录，文件行点击发起下载', async () => {
    mockStore.sftpEntries = [
      { name: 'hci', path: '/var/log/hci', is_dir: true, size: 0 },
      { name: 'messages', path: '/var/log/messages', is_dir: false, size: 2048 },
    ]
    const wrapper = mountPanel()

    await wrapper.find('.sftp-row.is-dir .sftp-row__name').trigger('click')
    expect(mockStore.sftpList).toHaveBeenCalledWith('/var/log/hci')

    const fileRow = wrapper.findAll('.sftp-row').find((row: any) => !row.classes('is-dir'))!
    await fileRow.find('.sftp-row__name').trigger('click')
    expect(mockStore.sftpDownload).toHaveBeenCalledWith('/var/log/messages')
    wrapper.unmount()
  })

  it('手动路径下载：输入绝对路径后点下载', async () => {
    const wrapper = mountPanel()
    const manualInput = wrapper.findAll('input.input-stub')[1]
    await manualInput.setValue('/var/log/hci/log.zip')
    const downloadBtn = wrapper
      .findAll('button.btn-stub')
      .find((btn: any) => btn.text() === '下载')!
    await downloadBtn.trigger('click')
    expect(mockStore.sftpDownload).toHaveBeenCalledWith('/var/log/hci/log.zip')
    wrapper.unmount()
  })

  it('下载进行中：显示进度、落地文件路径与取消按钮，并禁用新下载', async () => {
    mockStore.sftpEntries = [{ name: 'log.zip', path: '/var/log/log.zip', is_dir: false, size: 1024 }]
    mockStore.sftpTransfer = {
      taskId: 'sftp-1',
      remotePath: '/var/log/log.zip',
      phase: 'downloading',
      bytes: 512,
      totalBytes: 1024,
      percent: 50,
      localPath: '/home/engineer/Downloads/hci-sftp/log.zip.part',
      downloadDir: '/home/engineer/Downloads/hci-sftp',
      transport: 'sftp',
      status: '',
      errorType: '',
      errorMessage: '',
      sha256: '',
    }
    const wrapper = mountPanel()

    expect(wrapper.find('.progress-stub').attributes('data-percentage')).toBe('50')
    expect(wrapper.text()).toContain('下载中 50%')
    expect(wrapper.text()).toContain('/home/engineer/Downloads/hci-sftp/log.zip.part')

    const cancelBtn = wrapper
      .findAll('button.btn-stub')
      .find((btn: any) => btn.text() === '取消')!
    await cancelBtn.trigger('click')
    expect(mockStore.sftpCancelDownload).toHaveBeenCalled()

    // 已有任务在跑：文件行的下载按钮必须禁用，避免撞上 bridge 的并发上限
    const rowDownload = wrapper.find('.sftp-row__download')
    expect(rowDownload.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('下载失败展示 bridge 返回的错误文案', () => {
    mockStore.sftpTransfer = {
      taskId: 'sftp-2',
      remotePath: '/var/log/log.zip',
      phase: 'done',
      bytes: 0,
      totalBytes: 0,
      percent: 0,
      localPath: '',
      downloadDir: '',
      transport: 'exec_fallback',
      status: 'error',
      errorType: 'remote_io_failed',
      errorMessage: '打开远端文件失败: no such file',
      sha256: '',
    }
    const wrapper = mountPanel()
    expect(wrapper.text()).toContain('打开远端文件失败: no such file')
    expect(wrapper.text()).toContain('tail 回退')
    wrapper.unmount()
  })

  it('SFTP 子系统不可用时展示降级提示，手动路径入口仍然可用', () => {
    mockStore.sftpListError = '当前节点未启用 SFTP 子系统，无法浏览目录；可直接输入文件绝对路径下载（将回退到 exec 通道）'
    mockStore.sftpEntries = []
    const wrapper = mountPanel()
    expect(wrapper.find('.sftp-error').text()).toContain('未启用 SFTP 子系统')
    const downloadBtn = wrapper.findAll('button.btn-stub').find((btn: any) => btn.text() === '下载')!
    expect(downloadBtn.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('目录项被截断时给出提示', () => {
    mockStore.sftpListTruncated = true
    const wrapper = mountPanel()
    expect(wrapper.text()).toContain('仅显示前 2000 项')
    wrapper.unmount()
  })

  it('折叠状态下不渲染面板主体', () => {
    mockStore.sftpPanelVisible = false
    const wrapper = mountPanel()
    const body = wrapper.find('.sftp-body')
    // v-show：元素存在但被隐藏，避免展开时重新挂载丢失滚动位置
    expect(body.exists()).toBe(true)
    expect((body.element as HTMLElement).style.display).toBe('none')
    wrapper.unmount()
  })
})
