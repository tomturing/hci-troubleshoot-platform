/**
 * 兼容层：拼接逻辑已上提到 @hci/shared，由客户侧与管理端共用。
 *
 * 保留本文件是为了不改动验收页里对 /src/utils/... 的动态 import 路径；
 * 这里只做转发，不存放任何第二份实现。
 */
export { formatVersionArchitecture, formatAcliVersion } from '@hci/shared'
