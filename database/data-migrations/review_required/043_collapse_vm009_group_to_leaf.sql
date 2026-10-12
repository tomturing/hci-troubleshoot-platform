-- 043_collapse_vm009_group_to_leaf.sql —— 需专家签字后手工应用（REQUIRES HUMAN SIGN-OFF）。
--
-- 作用（分类树「折叠分组为叶子」，为 042 的逆操作）：
--   1) 删除「虚拟机-009（虚拟机集群内或跨集群迁移失败）」下的**全部直接子节点**（现为 L4 叶子 010~015）；
--   2) 删除前，把这些子节点上关联的**所有 KBD/SOP 及全库按 code 引用分类的列**统一改绑到父节点「虚拟机-009」；
--   3) 子节点清空后，「虚拟机-009」在结构上自动由「分组」退化为「叶子」，并合法承接原 L4 的全部内容；
--   4) 递归重建 level 与 path_labels，保证折叠后层级与路径自洽。
--
-- 第一性原理（为什么必须先改绑、再删，且不能置空）：
--   - kbd_entry.category_id / sop_document.category_id 对 kb_category(code) 的外键为
--     `ON DELETE NO ACTION`（不可延迟、非 CASCADE、非 SET NULL）：只要还有任意一行引用某分类码，
--     删除该分类必被数据库拒绝。故「自动上移到 009」与「分类变空」都不会自发发生——
--     唯一可行路径是先把引用改到仍存在的 009（FK 全程满足），再删空壳子节点。
--   - 应用层 category_repo.delete() 亦会在「有子分类 / 有已发布 KBD·SOP 引用」时直接抛错阻断，
--     不提供任何级联上移能力。故本操作只能以数据迁移形态在库内完成。
--
-- 幂等性 & 安全性：
--   - 子节点集合**动态取自 DB**（parent_id = 009.id），不硬编码 010~015，跨环境可移植；
--   - 只把引用列从「子节点旧码」改到「已存在的 009 码」，不改写任何 kb_category.code，
--     故 fk_kbd_entry_category_id / fk_sop_document_category_id 全程满足，无需临时移除外键；
--   - 若 009 已是叶子（无子节点，如重复执行），子集为空 → 所有 UPDATE/DELETE 自然 no-op，verify 通过。
--
-- 前置安全断言（$pre$）：若 009 的任一直接子节点自身仍含子节点（即存在 L5 孙层，非纯 L4 叶子），
--   则「折叠为叶子」的前提不成立且会孤立孙节点，立即 RAISE EXCEPTION 整体回滚，须先人工处理更深层级。
--
-- 语义提醒（对抗性审查）：010~015 按「冷/热迁移 × 跨/同存储」细分，全部并回 009 会**丢失该层语义粒度**，
--   后续检索/诊断只能落到单个粗粒度叶子。请确认此为期望的收敛，而非误操作。
--
-- 落点：位于 data-migrations/review_required/ 子目录；migration-runner 仅 glob 顶层 *.sql，
--   故本迁移不会被自动应用，须人工复核后手工执行。
-- 唯一调用链标记：category_collapse_vm009_to_leaf_v1。

BEGIN;

-- ── 0) 前置断言：009 必须存在；其所有直接子节点必须都是叶子（无孙层），否则整体回滚 ──────
DO $pre$
DECLARE
    v_parent_id       int;
    n_nonleaf_children int;
BEGIN
    SELECT id INTO v_parent_id FROM kb_category WHERE code = '虚拟机-009';
    IF v_parent_id IS NULL THEN
        RAISE EXCEPTION 'category_collapse_vm009_to_leaf_v1 前置失败：未找到 虚拟机-009';
    END IF;

    SELECT COUNT(*) INTO n_nonleaf_children
      FROM kb_category ch
     WHERE ch.parent_id = v_parent_id
       AND EXISTS (SELECT 1 FROM kb_category gc WHERE gc.parent_id = ch.id);
    IF n_nonleaf_children > 0 THEN
        RAISE EXCEPTION
            'category_collapse_vm009_to_leaf_v1 前置失败：虚拟机-009 下有 % 个子节点自身仍含子节点（非纯 L4 叶子），不可安全折叠，请先处理更深层级',
            n_nonleaf_children;
    END IF;
END $pre$;

-- ── 1) 动态收集 009 的全部直接子节点旧码（随事务提交自动 DROP）──────────────────────────
CREATE TEMP TABLE _vm009_children(old_code text) ON COMMIT DROP;
INSERT INTO _vm009_children(old_code)
SELECT c.code
  FROM kb_category c
 WHERE c.parent_id = (SELECT id FROM kb_category WHERE code = '虚拟机-009')
   AND c.code IS NOT NULL;

-- ── 2) 把「全库按 code 引用这些子分类」的列统一改绑到 虚拟机-009 ────────────────────────
--   清单以「全库按 kb_category.code 引用点」为事实源穷举（共 10 列，与 resequence_domain 对齐）：
--   有 FK：kbd_entry.category_id / sop_document.category_id；
--   无 FK 但存 code：其余 8 列（category_scope 含通配 '*'，等值匹配天然不命中，安全）。
UPDATE kbd_entry SET category_id = '虚拟机-009'
 WHERE category_id IN (SELECT old_code FROM _vm009_children);
UPDATE kbd_entry SET ai_category_id = '虚拟机-009'
 WHERE ai_category_id IN (SELECT old_code FROM _vm009_children);
UPDATE sop_document SET category_id = '虚拟机-009'
 WHERE category_id IN (SELECT old_code FROM _vm009_children);
UPDATE conversation SET category_id = '虚拟机-009'
 WHERE category_id IN (SELECT old_code FROM _vm009_children);
UPDATE offline_signal_collector_mapping SET category_scope = '虚拟机-009'
 WHERE category_scope IN (SELECT old_code FROM _vm009_children);
UPDATE diagnosis_candidate SET category_id = '虚拟机-009'
 WHERE category_id IN (SELECT old_code FROM _vm009_children);
UPDATE diagnosis_run SET resolved_category = '虚拟机-009'
 WHERE resolved_category IN (SELECT old_code FROM _vm009_children);
UPDATE diagnosis_run SET selected_category = '虚拟机-009'
 WHERE selected_category IN (SELECT old_code FROM _vm009_children);
UPDATE diagnosis_session SET resolved_category = '虚拟机-009'
 WHERE resolved_category IN (SELECT old_code FROM _vm009_children);
UPDATE diagnosis_session SET selected_category = '虚拟机-009'
 WHERE selected_category IN (SELECT old_code FROM _vm009_children);

-- ── 3) 删除已清空的子节点（此时无 KBD/SOP 引用、且均为叶子 → FK NO ACTION 不阻断）─────────
DELETE FROM kb_category
 WHERE parent_id = (SELECT id FROM kb_category WHERE code = '虚拟机-009');

-- ── 4) 递归重建 level 与 path_labels（折叠后层级/路径自洽；对全树幂等重算）─────────────────
WITH RECURSIVE tree AS (
    SELECT id, name, parent_id, 1 AS lvl, ARRAY[name]::text[] AS names
      FROM kb_category WHERE parent_id IS NULL
    UNION ALL
    SELECT c.id, c.name, c.parent_id, t.lvl + 1, t.names || c.name
      FROM kb_category c JOIN tree t ON c.parent_id = t.id
)
UPDATE kb_category k SET level = tree.lvl, path_labels = to_jsonb(tree.names)
  FROM tree WHERE k.id = tree.id;

-- ── 5) 校验门禁：任一不满足即整体回滚 ────────────────────────────────────────────────────
DO $verify$
DECLARE
    v_009_id       int;
    n_child_left   int;
    n_009_has_child int;
    n_dangling     int;
BEGIN
    SELECT id INTO v_009_id FROM kb_category WHERE code = '虚拟机-009';

    -- a) 原子节点应已全部删除
    SELECT COUNT(*) INTO n_child_left
      FROM kb_category WHERE code IN (SELECT old_code FROM _vm009_children);
    -- b) 009 必须已成为叶子（无任何子节点）
    SELECT COUNT(*) INTO n_009_has_child FROM kb_category WHERE parent_id = v_009_id;
    -- c) 全库不得再有任何列引用已删子码（无悬空）
    SELECT
        (SELECT COUNT(*) FROM kbd_entry WHERE category_id IN (SELECT old_code FROM _vm009_children))
      + (SELECT COUNT(*) FROM kbd_entry WHERE ai_category_id IN (SELECT old_code FROM _vm009_children))
      + (SELECT COUNT(*) FROM sop_document WHERE category_id IN (SELECT old_code FROM _vm009_children))
      + (SELECT COUNT(*) FROM conversation WHERE category_id IN (SELECT old_code FROM _vm009_children))
      + (SELECT COUNT(*) FROM offline_signal_collector_mapping WHERE category_scope IN (SELECT old_code FROM _vm009_children))
      + (SELECT COUNT(*) FROM diagnosis_candidate WHERE category_id IN (SELECT old_code FROM _vm009_children))
      + (SELECT COUNT(*) FROM diagnosis_run WHERE resolved_category IN (SELECT old_code FROM _vm009_children) OR selected_category IN (SELECT old_code FROM _vm009_children))
      + (SELECT COUNT(*) FROM diagnosis_session WHERE resolved_category IN (SELECT old_code FROM _vm009_children) OR selected_category IN (SELECT old_code FROM _vm009_children))
      INTO n_dangling;

    IF n_child_left > 0 THEN
        RAISE EXCEPTION 'category_collapse_vm009_to_leaf_v1 校验失败：仍有 % 个 虚拟机-009 子节点未删除', n_child_left;
    END IF;
    IF n_009_has_child > 0 THEN
        RAISE EXCEPTION 'category_collapse_vm009_to_leaf_v1 校验失败：虚拟机-009 仍有 % 个子节点，未成为叶子', n_009_has_child;
    END IF;
    IF n_dangling > 0 THEN
        RAISE EXCEPTION 'category_collapse_vm009_to_leaf_v1 校验失败：全库仍有 % 处引用已删子分类码（悬空）', n_dangling;
    END IF;

    RAISE NOTICE 'category_collapse_vm009_to_leaf_v1 校验通过：虚拟机-009 已折叠为叶子，原 L4 内容全部归并至 009，无悬空引用';
END $verify$;

COMMIT;
