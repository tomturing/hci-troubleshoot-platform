/**
 * 评分评价 API 封装
 * 提供会话评分提交功能
 */

import type { AxiosInstance, AxiosResponse } from 'axios'

/** 评分提交请求参数 */
export interface EvaluateRequest {
  /** 评分 1-5 星 */
  score: number
  /** 可选的补充说明 */
  feedback?: string
}

/** 评分提交响应 */
export interface EvaluateResponse {
  /** 评价记录 ID */
  evaluation_id?: string | null
  /** 用户评分 */
  score: number
  /** 综合质量分 */
  composite_score: number
  /** 提示消息 */
  message: string
}

/** 创建评分 API 客户端 */
export function createEvaluateApi(client: AxiosInstance) {
  return {
    /**
     * 提交会话评分
     * @param conversationId 会话 ID
     * @param data 评分数据
     * @returns 评价响应
     */
    submit(
      conversationId: string,
      data: EvaluateRequest,
    ): Promise<AxiosResponse<EvaluateResponse>> {
      return client.post<EvaluateResponse>(`/conversations/${conversationId}/evaluate`, data)
    },
  }
}
