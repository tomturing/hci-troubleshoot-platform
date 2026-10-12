import { describe, expect, it } from 'vitest'
import { buildCategoryTree, collectAncestorPathKeys, flattenOptions, resolveCategoryNodeKey, type RawCategoryNode } from '../useCategories'

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

describe('collectAncestorPathKeys（子树取消全展开后，选中项祖先链展开）', () => {
  it('叶子返回 自身 → 父 → 域根 的稳定主键链', () => {
    // id=3(热迁移失败) → id=2(迁移类) → id=1(虚拟机域根)
    expect(collectAncestorPathKeys(nodes, 3)).toEqual([3, 2, 1])
  })

  it('域根自身返回仅含根的单元素', () => {
    expect(collectAncestorPathKeys(nodes, 1)).toEqual([1])
  })

  it('targetKey 为空或不在节点集时返回空数组（不伪造展开）', () => {
    expect(collectAncestorPathKeys(nodes, null)).toEqual([])
    expect(collectAncestorPathKeys(nodes, undefined)).toEqual([])
    expect(collectAncestorPathKeys(nodes, 999)).toEqual([])
  })

  it('环状脏数据不导致死循环（seen 集合防御）', () => {
    const cyclic: RawCategoryNode[] = [
      { code: 'A', name: 'A', level: 2, id_in_db: 10, parent_id: 11, path_labels: ['A'] },
      { code: 'B', name: 'B', level: 2, id_in_db: 11, parent_id: 10, path_labels: ['B'] },
    ]
    expect(collectAncestorPathKeys(cyclic, 10)).toEqual([10, 11])
  })
})

describe('resolveCategoryNodeKey（展开/折叠事件回写 expandedKeys 的稳定主键）', () => {
  it('从事件第一个实参（节点 data 对象）取 id_in_db', () => {
    // el-tree 的 node-expand/node-collapse 第一个实参是 node.data 本身
    expect(resolveCategoryNodeKey({ id_in_db: 24, code: '虚拟机-024' })).toBe(24)
  })

  it('id_in_db 缺失时回退 id', () => {
    expect(resolveCategoryNodeKey({ id: 7, code: 'x' })).toBe(7)
  })

  it('id_in_db 为 0 视为有效主键（不被 falsy 吞掉）', () => {
    expect(resolveCategoryNodeKey({ id_in_db: 0, id: 99 })).toBe(0)
  })

  it('空值/非对象返回 null（不误写展开集合）', () => {
    expect(resolveCategoryNodeKey(null)).toBeNull()
    expect(resolveCategoryNodeKey(undefined)).toBeNull()
    expect(resolveCategoryNodeKey('str')).toBeNull()
    expect(resolveCategoryNodeKey({ code: 'no-key' })).toBeNull()
  })

  it('回归：折叠后再展开兄弟，折叠项 key 已被移除不会残留重弹', () => {
    // 复现用户现象的纯逻辑版：以 expandedKeys 数组模拟写入口径
    const k20 = resolveCategoryNodeKey({ id_in_db: 20 })
    const k24 = resolveCategoryNodeKey({ id_in_db: 24 })
    let expanded: (number | string)[] = [1]
    // 展开 020 → 加入
    expanded = [...expanded, k20!]
    // 折叠 020 → 移除（修复前此处因取不到 key 而不移除，导致残留）
    expanded = expanded.filter((x) => x !== k20)
    // 展开 024 → 加入并触发 watcher 重放：020 已不在集合内，不会被重新展开
    expanded = [...expanded, k24!]
    expect(expanded).toEqual([1, 24])
    expect(expanded).not.toContain(20)
  })
})
