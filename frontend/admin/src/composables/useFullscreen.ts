import { getCurrentInstance, onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

/**
 * 内嵌页面的全屏切换。
 *
 * 为什么不用 CSS 模拟全屏：内嵌页在 iframe 里滚动，模拟全屏要自己算 fixed 定位、
 * 遮罩和 z-index，遇到 Element Plus 的弹层（下拉、对话框）还会被盖住。
 * 浏览器原生 Fullscreen 把元素直接铺满屏幕，父子 flex 链天然撑满，弹层层级也正常。
 *
 * 全屏元素取「页面根容器」而不是 iframe：这样工具栏与页签仍可见，
 * 用户能在全屏内切换页签、点重新连接，退出入口也一直在。
 */

/** 兼容旧版 Safari 的前缀实现（只有 WebKit 需要）。 */
interface WebkitDocument extends Document {
  webkitFullscreenElement?: Element | null
  webkitExitFullscreen?: () => Promise<void> | void
}

interface WebkitElement extends HTMLElement {
  webkitRequestFullscreen?: () => Promise<void> | void
}

export function useFullscreen(target: Ref<HTMLElement | null>) {
  const isFullscreen = ref(false)

  const doc = typeof document === 'undefined' ? null : (document as WebkitDocument)
  const supported = Boolean(doc && (doc.fullscreenEnabled || (doc as any).webkitFullscreenEnabled))

  function currentElement(): Element | null {
    if (!doc) return null
    return doc.fullscreenElement ?? doc.webkitFullscreenElement ?? null
  }

  function sync() {
    isFullscreen.value = Boolean(currentElement() && currentElement() === target.value)
  }

  async function toggle() {
    if (!doc) return
    const el = target.value as WebkitElement | null

    if (currentElement()) {
      if (doc.exitFullscreen) await doc.exitFullscreen()
      else await doc.webkitExitFullscreen?.()
      sync()
      return
    }

    if (!el) return
    if (el.requestFullscreen) await el.requestFullscreen()
    else await el.webkitRequestFullscreen?.()
    sync()
  }

  // 只在组件 setup 内绑定生命周期：单测直接调用本函数时没有实例，
  // 无条件注册会刷 onMounted 警告；这种场景下 toggle() 内的 sync() 已能保证状态正确。
  if (getCurrentInstance()) {
    onMounted(() => {
      doc?.addEventListener('fullscreenchange', sync)
      doc?.addEventListener('webkitfullscreenchange', sync)
    })

    onBeforeUnmount(() => {
      doc?.removeEventListener('fullscreenchange', sync)
      doc?.removeEventListener('webkitfullscreenchange', sync)
    })
  }

  return { isFullscreen, supported, toggle }
}
