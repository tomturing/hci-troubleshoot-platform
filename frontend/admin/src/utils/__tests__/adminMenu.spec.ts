import { describe, expect, it, vi } from 'vitest'

import { buildMenuItems, getRouteOrder, resolveMenuIcon } from '../adminMenu'
import router from '../../router'

describe('admin menu', () => {
  it('按 order 将用户和工单置于仪表盘与可观测性之间，并将仿真入口置于离线诊断之后', () => {
    const routes = [
      { path: '/simulation', meta: { title: '仿真测试', icon: 'VideoPlay', order: 12 } },
      { path: '/offline-diagnosis', meta: { title: '离线诊断', icon: 'FirstAidKit', order: 11 } },
      { path: '/dashboard', meta: { title: '仪表盘', icon: 'Odometer', order: 1 } },
      { path: '/cases', meta: { title: '工单管理', icon: 'Tickets', order: 3 } },
      { path: '/observability', meta: { title: '可观测性', icon: 'DataAnalysis', order: 4 } },
      { path: '/clients', meta: { title: '用户管理', icon: 'User', order: 2 } },
      { path: '/simulation/bundle-factory', meta: { title: 'Bundle工厂', icon: 'Box', order: 12.5 } },
    ]

    expect(buildMenuItems(routes).map((item) => item.title)).toEqual([
      '仪表盘', '用户管理', '工单管理', '可观测性', '离线诊断', '仿真测试', 'Bundle工厂',
    ])
  })

  it('未知排序值排末尾并记录告警', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(getRouteOrder({ path: '/broken', meta: { order: '9' } })).toBe(Number.MAX_SAFE_INTEGER)
    expect(warn).toHaveBeenCalledOnce()
    warn.mockRestore()
  })

  it('实际路由菜单顺序与产品分组一致', () => {
    expect(buildMenuItems(router.getRoutes()).map((item) => item.title)).toEqual([
      '仪表盘', '用户管理', '工单管理', '可观测性', '分类基线', 'Catalog基线',
      'KBD管理', 'SOP管理', '工具管理', '技能管理', 'Prompt管理', '离线诊断', 'console审计', '仿真测试', 'Bundle工厂', '模板实例库',
    ])
  })

  it('所有已导出的 Element Plus 图标都能解析，未知值有可见回退', () => {
    expect(resolveMenuIcon('Setting')).toBeTruthy()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(resolveMenuIcon('IconThatDoesNotExist')).toBeTruthy()
    expect(warn).toHaveBeenCalledOnce()
    warn.mockRestore()
  })
})
