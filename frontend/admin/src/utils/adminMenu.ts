import { markRaw, type Component } from 'vue'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

export interface AdminRouteLike {
  path: string
  meta?: Record<string, unknown>
}

export interface AdminMenuItem {
  path: string
  title: string
  icon: string
  children?: AdminMenuItem[]
}

/** 共享父菜单：工具管理下挂 agent工具 与 acli插件 两个子页面。 */
const PARENT_MENUS: Record<string, { path: string; title: string; icon: string; order: number }> = {
  tools: { path: '/tools-group', title: '工具管理', icon: 'Setting', order: 8 },
}

/**
 * 从图标包的实际导出集合生成静态字典，新增路由图标时不再需要同步维护第二份清单。
 * markRaw 避免图标组件进入响应式代理，保证动态组件首屏稳定挂载。
 */
const menuIconMap: Record<string, Component> = Object.fromEntries(
  Object.entries(ElementPlusIconsVue).map(([name, component]) => [
    name,
    markRaw(component as Component),
  ]),
) as Record<string, Component>

const fallbackIcon = markRaw(ElementPlusIconsVue.Tools)
const warnedIconNames = new Set<string>()

/** 将路由图标名称解析为始终可渲染的组件，并记录配置错误。 */
export function resolveMenuIcon(iconName: unknown): Component {
  if (typeof iconName === 'string' && menuIconMap[iconName]) {
    return menuIconMap[iconName]
  }

  const missingIconName = String(iconName || '(空)')
  if (!warnedIconNames.has(missingIconName)) {
    warnedIconNames.add(missingIconName)
    console.warn(`[菜单图标] 未找到图标 ${missingIconName}，已使用 Tools 回退图标`)
  }
  return fallbackIcon
}

/** 缺失或非法排序值统一排在菜单末尾，并保留可观测告警。 */
export function getRouteOrder(route: AdminRouteLike): number {
  const rawOrder = route.meta?.order
  if (typeof rawOrder === 'number' && Number.isFinite(rawOrder)) return rawOrder

  console.warn(`[菜单路由排序] 路由 ${route.path} 的 meta.order 缺失或不是合法数字，已按末尾排序处理`)
  return Number.MAX_SAFE_INTEGER
}

/** 根据路由元数据构造侧边栏菜单，确保排序规则只有一个事实来源。 */
export function buildMenuItems(routes: readonly AdminRouteLike[]): AdminMenuItem[] {
  const ordered = [...routes]
    .filter((route) => route.meta?.icon && !route.meta?.hidden)
    .map((route) => {
      const parentKey = typeof route.meta?.parent === 'string' ? route.meta.parent : ''
      const parent = PARENT_MENUS[parentKey]
      const item: AdminMenuItem = {
        path: route.path,
        title: String(route.meta?.title || ''),
        icon: String(route.meta?.icon || ''),
      }
      return { item, parentKey, effectiveOrder: parent ? parent.order : getRouteOrder(route) }
    })
    .sort((a, b) => a.effectiveOrder - b.effectiveOrder)

  const result: AdminMenuItem[] = []
  const groups = new Map<string, AdminMenuItem>()
  for (const { item, parentKey } of ordered) {
    const parent = PARENT_MENUS[parentKey]
    if (!parent) {
      result.push(item)
      continue
    }
    let group = groups.get(parentKey)
    if (!group) {
      group = { path: parent.path, title: parent.title, icon: parent.icon, children: [] }
      groups.set(parentKey, group)
      result.push(group)
    }
    group.children!.push(item)
  }
  return result
}
