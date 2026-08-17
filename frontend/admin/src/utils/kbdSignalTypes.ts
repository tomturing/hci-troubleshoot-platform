// ============ 关键信号 v2 数据模型（RFC §7 前端原生读 v2 对象化，2026-07-22） ============
// GET 边界直接返回 v2 文档，前端不再归一/适配，直接基于该结构渲染与编辑；
// 回写时仍发回完整 v2 文档（{schema_version, signals}），后端 update_kbd_entry 幂等归约。
// 2026-08-17：从 KbdReviewView.vue 抽出为独立模块，供信号 JSON 导入/导出等纯逻辑复用。

export interface SignalV2 {
  id?: number | string
  role?: 'must' | 'should' | 'exclude' | 'context'
  acquire: { tool: string; args: Record<string, any> }
  match: { type?: string; pattern?: string | string[]; mode?: string; expected?: boolean; value?: number; [key: string]: any } | null
  orchestrate: Record<string, any>
  provenance?: Record<string, any>
  review?: { require_human_confirm?: boolean }
}

export interface SignalsDoc {
  schema_version: number
  signals: SignalV2[]
  rejected_candidates?: Array<{
    candidate: unknown
    reason_code?: 'write_signal' | 'not_exists' | 'run_failed'
    reason: string
  }>
  verification_contract?: Record<string, any>
  generation_metadata?: Record<string, any>
  publish_validation?: Record<string, any>
}

export interface ChangeAnnotation {
  path?: string
  signal_id?: string
  reason_code: string
  note?: string
}
