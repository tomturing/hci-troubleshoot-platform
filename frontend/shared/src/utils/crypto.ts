/**
 * UUID 生成工具函数
 *
 * J-1 改进：统一 UUID 生成逻辑，解决非安全上下文（HTTP Ingress）下
 * `crypto.randomUUID` 不可用导致白屏的问题（参考 K3s集群健壮性改进计划 J-1）。
 *
 * 优先使用 Web Crypto API（HTTPS / localhost），
 * 降级到时间戳 + 随机数（HTTP 非安全上下文）。
 *
 * 禁止在各组件中重复实现降级逻辑（DRY 原则）。
 */

/**
 * 生成 UUID（v4 格式或兼容格式）。
 *
 * - HTTPS / localhost：使用 `crypto.randomUUID()` 生成标准 UUIDv4
 * - HTTP 非安全上下文：使用时间戳 + Math.random 生成兼容格式（非加密安全）
 *
 * @returns UUID 字符串
 */
export function generateUUID(): string {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        return crypto.randomUUID()
    }
    // 降级方案：在 HTTP 非安全上下文（如 NodePort HTTP Ingress）下生成兼容 ID
    // 格式与 UUIDv4 对齐但非加密安全，仅用于前端会话 ID 等非安全场景
    const timestamp = Date.now().toString(16).padStart(12, '0')
    const random = Math.random().toString(16).slice(2).padEnd(20, '0')
    return [
        timestamp.slice(0, 8),
        timestamp.slice(8, 12),
        '4' + random.slice(0, 3), // version 4
        ((parseInt(random.slice(3, 4), 16) & 0x3) | 0x8).toString(16) + random.slice(4, 7), // variant
        random.slice(7, 19),
    ].join('-')
}
