import { describe, expect, it } from 'vitest'
import { buildCategoryTree, flattenOptions, type RawCategoryNode } from '../useCategories'

// 模拟后端 grouped=true 返回：虚拟机域根(id=1) → 迁移类分组(id=2) → 两个叶子(id=3,4)
const nodes: RawCategoryNode[] = [
  { code: '虚拟机-L1', name: '虚拟机', level: 1, id_in_db: 1, parent_id: null, path_labels: ['虚拟机'] },
  { code: '虚拟机-001', name: '迁移类', level: 2, id_in_db: 2, parent_id: 1, path_labels: ['虚拟机', '迁移类'] },
  { code: '虚拟机-002', name: '热迁移失败', level: 3, id_in_db: 3, parent_id: 2, path_labels: ['虚拟机', '迁移类', '热迁移失败'] },
  { code: '虚拟机-003', name: '冷迁移失败', level: 3, id_in_db: 4, parent_id: 2, path_labels: ['虚拟机', '迁移类', '冷迁移失败'] },
]

describe('buildCategoryTree 分组层级还原（解A：code 不表征角色）', () => {
  const tree = buildCategoryTree(nodes)

  it('以域根为树根，分组作为可展开父节点承载 children', () => {
    expect(tree).toHaveLength(1)
    expect(tree[0].code).toBe('虚拟机-L1')
    // 域根下挂分组
    const group = tree[0].children.find((c) => c.code === '虚拟机-001')
    expect(group).toBeDefined()
    expect(group!.isLeaf).toBe(false)
    expect(group!.children.map((c) => c.code)).toEqual(['虚拟机-002', '虚拟机-003'])
  })

  it('叶子节点 isLeaf=true 且无 children', () => {
    const leaf = buildCategoryTree(nodes)[0].children[0].children[0]
    expect(leaf.isLeaf).toBe(true)
    expect(leaf.children).toHaveLength(0)
  })

  it('label 同时含 code 与 name，保证筛选下拉可展示分组名又可搜索编码', () => {
    expect(tree[0].label).toBe('虚拟机-L1  虚拟机')
  })
})

describe('flattenOptions', () => {
  it('扁平化按 code 升序且保留 path_labels', () => {
    const flat = flattenOptions(nodes)
    expect(flat.map((o) => o.code)).toEqual(['虚拟机-001', '虚拟机-002', '虚拟机-003', '虚拟机-L1'])
    expect(flat[0].path_labels).toEqual(['虚拟机', '迁移类'])
  })
})
