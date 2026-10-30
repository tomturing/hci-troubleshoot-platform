/**
 * 判断「客户机上正在跑的 bridge」是不是「平台当前分发的那个构建」。
 *
 * 背景（2026-10-30 Q2026103000994）：交付源包守卫只存在于新版 bridge 里，
 * 而终端客户不会自动更新 exe —— 平台把新 exe 放进下载目录，现场机器上跑的还是旧版，
 * 修复等于没上线，且用户毫无察觉（旧版只是继续按老逻辑每片 500）。
 *
 * 底层事实：bridge 连上浏览器时会下发 `bridge_hello`，其 `message` 就是
 * `getVersion()`（构建期 `-X main.Version=<git describe>` 注入）；构建脚本同时把同一个
 * 版本号写进 `public/downloads/bridge-version.json`。两边同源，才能做等值比较。
 *
 * 边界（对抗性审查）：
 *   - 任一侧取不到版本（老 bridge 未带 message、清单 404/离线）→ 一律**不提示**。
 *     提示是优化项，不能因为清单缺失就让用户以为自己环境坏了。
 *   - 只比版本串，不比 commit：`git describe --tags --always` 在非 tag 提交上本身就带
 *     commit 后缀，版本相同即源码相同；再比 commit 会让「同一个 tag 重新出包」误报。
 *   - 措辞不得断言「你的版本旧」：用户可能自编译了更新的 bridge。
 */

export interface PublishedBridgeVersion {
  version: string
  commit?: string
  build_time?: string
}

/** 与构建脚本落盘位置严格一致；改任一侧必须同步改另一侧。 */
export const BRIDGE_VERSION_MANIFEST_URL = '/downloads/bridge-version.json'

/** bridge_hello.message / 清单里的 version 都按字符串处理，脏值统一折成空串。 */
export function normalizeBridgeVersion(raw: unknown): string {
  return typeof raw === 'string' ? raw.trim() : ''
}

/**
 * 是否与平台分发版本不一致。
 *
 * 返回 false 的两种情况必须区分开：真的相同，以及**无从判断**。调用方据此决定是否提示，
 * 而不是把「判断不了」当成「不一致」——那会让每次清单抖动都变成一次全量报警。
 */
export function isBridgeVersionStale(
  running: unknown,
  published: PublishedBridgeVersion | null | undefined,
): boolean {
  const actual = normalizeBridgeVersion(running)
  const expected = normalizeBridgeVersion(published?.version)
  if (!actual || !expected) return false
  return actual !== expected
}

/** 读取平台分发的 bridge 版本清单；任何失败都返回 null（fail-open，不阻断任何流程）。 */
export async function loadPublishedBridgeVersion(
  fetcher?: typeof fetch,
): Promise<PublishedBridgeVersion | null> {
  const doFetch = fetcher ?? (typeof fetch === 'function' ? fetch : undefined)
  if (!doFetch) return null
  try {
    // no-store：清单随每次发版更新，读到缓存等于把「是不是最新版」这条判断做废。
    const res = await doFetch(BRIDGE_VERSION_MANIFEST_URL, { cache: 'no-store' })
    if (!res || !res.ok) return null
    const data = (await res.json()) as Partial<PublishedBridgeVersion> | null
    const version = normalizeBridgeVersion(data?.version)
    if (!version) return null
    return {
      version,
      commit: normalizeBridgeVersion(data?.commit) || undefined,
      build_time: normalizeBridgeVersion(data?.build_time) || undefined,
    }
  } catch {
    return null
  }
}
