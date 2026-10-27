/**
 * 错误提示的存活时长策略（全局，一处定义）。
 *
 * 第一性原理：错误提示该活多久，取决于「用户有没有读完」，而不取决于「过了几秒」。
 * Element Plus 的 ElMessage 默认 3s 自动关闭，对一句话的「保存成功」够用，对一条带
 * 路径、退出码、命令原文的报错（例如
 * `IP: 172.28.24.2 /sf/data/... 可用空间仅为 1.42GB，小于 6GB，请清理后重试！[FATA0001]
 * executing command in container: command terminated with exit code 1`）完全不够——
 * 用户实测的反馈是「来不及看就消失了，还没看完具体的报错就没有了」。
 *
 * 因此把「必须由人确认」写成全局策略：duration=0（永不自动关闭）+ showClose（给出口）。
 * 放在这里而不是要求 27 个调用点各自记得传 duration —— 后者必然漏，而且新增调用点默认
 * 又会退回 3s，问题会反复出现。
 *
 * 边界：
 * - 只接管 error（红色）。success / warning / info 仍按默认时长消失，避免正常流程刷屏；
 * - 调用方显式传的 duration 会被覆盖：这是产品策略，不该由某个调用点单独降级；
 * - 幂等：重复 install 不会套娃包装。
 */
import { ElMessage } from 'element-plus'

/** 0 = 不自动关闭，直到用户点关闭按钮 */
const PERSISTENT_DURATION = 0

let installed = false

type MessageInput = string | Record<string, unknown>

/**
 * 安装「错误提示不自动关闭」策略。
 *
 * 必须在任何业务代码弹出错误提示之前调用一次（`main.ts` 顶层）。
 */
export function installPersistentErrorMessages(): void {
  if (installed) return
  installed = true

  const baseError = (ElMessage.error as (options?: MessageInput) => unknown).bind(ElMessage)

  ElMessage.error = ((input: MessageInput) => {
    const options = typeof input === 'string' ? { message: input } : { ...input }
    // duration 放在展开之后：策略优先于调用点，禁止被 3s 改回去
    return baseError({ showClose: true, ...options, duration: PERSISTENT_DURATION })
  }) as typeof ElMessage.error
}
