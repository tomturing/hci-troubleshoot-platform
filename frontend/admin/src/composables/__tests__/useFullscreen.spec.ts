import { afterEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { useFullscreen } from '../useFullscreen'

/** jsdom 没有 Fullscreen API，按浏览器行为手动补一个可控实现。 */
function stubFullscreen(element: HTMLElement) {
  const request = vi.fn(async () => {
    Object.defineProperty(document, 'fullscreenElement', { value: element, configurable: true })
    document.dispatchEvent(new Event('fullscreenchange'))
  })
  const exit = vi.fn(async () => {
    Object.defineProperty(document, 'fullscreenElement', { value: null, configurable: true })
    document.dispatchEvent(new Event('fullscreenchange'))
  })

  Object.defineProperty(element, 'requestFullscreen', { value: request, configurable: true })
  Object.defineProperty(document, 'exitFullscreen', { value: exit, configurable: true })
  Object.defineProperty(document, 'fullscreenElement', { value: null, configurable: true })
  return { request, exit }
}

afterEach(() => {
  Object.defineProperty(document, 'fullscreenElement', { value: null, configurable: true })
})

describe('useFullscreen', () => {
  it('首次点击进入全屏，再次点击退出，状态跟随浏览器事件', async () => {
    const element = document.createElement('div')
    const { request, exit } = stubFullscreen(element)
    const target = ref<HTMLElement | null>(element)
    const { isFullscreen, toggle } = useFullscreen(target)

    expect(isFullscreen.value).toBe(false)

    await toggle()
    expect(request).toHaveBeenCalledTimes(1)
    expect(isFullscreen.value).toBe(true)

    await toggle()
    expect(exit).toHaveBeenCalledTimes(1)
    expect(isFullscreen.value).toBe(false)
  })

  it('容器未挂载时点击不抛错', async () => {
    stubFullscreen(document.createElement('div'))
    const target = ref<HTMLElement | null>(null)
    const { isFullscreen, toggle } = useFullscreen(target)

    await expect(toggle()).resolves.toBeUndefined()
    expect(isFullscreen.value).toBe(false)
  })
})
