/**
 * 客户侧报错文案的唯一出口。
 *
 * 格式固定为「组件标签：问题描述；建议动作」。三段五组件、问题描述、建议动作
 * 全部来自 contracts/log_collect_display.json 的 codegen 产物，前端不另写第二份；
 * 管理员侧另用 faultDetail() 取判据、证据与调用链。
 *
 * 为什么不逐处手写文案：同一句 x509 错误曾在横幅、弹框、重试提示里出现三种
 * 说法，客户截图回来仍要人肉反查是哪一层。统一出口后，任何报错都能一眼定界。
 */
import { ElMessage } from 'element-plus'
import { FAULT_CATALOG, FAULT_DEFAULT_CODE, UI_TEXT, classifyFault, formatFaultText } from '@hci/shared'

/** 已知错误码优先（调用点比文本更清楚是哪一段），否则按契约关键词归类。 */
export function customerFaultText(error: unknown, preferredCode?: string | null): string {
  if (preferredCode && preferredCode in FAULT_CATALOG) return formatFaultText(preferredCode)
  const guessed = classifyFault(error)
  // 没有权威码时，不能把「未命中关键词」说成「平台没给码」：
  // 诊断卡与任务详情里已有完整原因，这里只引导去看，不编造结论。
  if (guessed === FAULT_DEFAULT_CODE) return UI_TEXT.fallback.no_code
  return formatFaultText(guessed)
}

/** 链路故障弹框：客户截图反馈时，这一行就是定位所需的全部信息。 */
export function showCustomerFault(error: unknown, preferredCode?: string | null): void {
  ElMessage.error(customerFaultText(error, preferredCode))
}
