-- 042_rebalance_group_bound_kbds.sql —— 需专家签字后手工应用（REQUIRES HUMAN SIGN-OFF）。
--
-- 作用（解A 叶子绑定治理，承接 041 头注预告的 review_required/042）：
--   1) 把错挂在「多子分组节点（非叶子）」上的 KBD（含 published/draft/unpublishable）按
--      关键词规则逐条重挂到正确叶子；
--   2) 修正 虚拟机-049 的命名错配 —— 名为「虚拟机中勒索病毒」但其子节点 050~053 全为网络类，
--      实为网络分组，改名为「虚拟机网络异常」；
--   3) 递归重建 level 与 path_labels，保证 049 改名后路径与层级自洽。
--
-- 规则化而非硬编码 support_id：用「有序 UPDATE（先具体后兜底）」复刻关键词规则，
--   已被前序规则改走 category_id 的行不再被后续命中，等价于 Python 优先级分派；
--   跨环境可移植、幂等（分组上无绑定即自然 no-op）。
-- 规则来源：live staging 只读 dry-run + 回滚试跑校验。错挂分组的真实 code 为：
--   虚拟机-009（迁移组，83 条 = 81 pub/1 draft/1 other）—— 注意 015 是其叶子而非分组；
--   虚拟机-049（网络组，1 条）；虚拟机-065（软件组，1 条）。合计 85 条。
--   叶子归属经 gt_children 实测：009→010~015，049→050~053，065→066~069（均为叶子）。
--   回滚式 dry-run 实测迁移后分布：010:3 / 011:27 / 012:50 / 015:3 / 052:1 / 068:1 = 85；
--   分组零残留、已发布 KBD 绑非叶子=0、049 path_labels 级联自洽 —— verify 门禁通过。
--
-- 取代：本文件替换 PR #1132 遗留的 042_kbd_reclassify_off_groups.sql —— 后者有 42 条
--   UPDATE 把 KBD 又绑回分组 虚拟机-015/049（code 语义错位），从未应用且属缺陷产物，已删除。
--
-- 前置：必须在 041（category_leaf_governance_v1）之后执行 —— 叶子须已是迁移后新码。
-- 落点：位于 data-migrations/review_required/ 子目录；migration-runner 仅 glob 顶层 *.sql，
--   故本迁移不会被自动应用，须人工复核后手工执行。
-- 唯一调用链标记：category_rebalance_group_bound_v1。
--
-- 幂等性 & 安全性：本迁移只把 kbd_entry.category_id 从「分组 code」改到另一个「已存在叶子 code」，
--   不改写任何 kb_category.code，故 fk_kbd_entry_category_id / fk_sop_document_category_id
--   （REFERENCES kb_category(code)）始终满足，无需临时移除外键。

BEGIN;

-- ── 1) 修正 049 命名错配：网络分组却名为勒索病毒 → 更名「虚拟机网络异常」──────────────
UPDATE kb_category
   SET name = '虚拟机网络异常'
 WHERE code = '虚拟机-049' AND name = '虚拟机中勒索病毒';

-- ── 2) 重挂错挂在分组「虚拟机-009（虚拟机集群内或跨集群迁移失败）」上的 KBD ──────────────
--   叶子：010 集群内热·跨存储 / 011 集群内热·同存储 / 012 跨集群热 / 013 集群内冷·跨存储 /
--         014 集群内冷·同存储 / 015 跨集群冷。
--   维度：跨集群/集群内 × 冷/热（缺省热） × 跨存储/同存储（缺省同）。优先级由具体到兜底：
--   ① 跨集群 + 冷迁移 → 015
UPDATE kbd_entry SET category_id = '虚拟机-015'
 WHERE category_id = '虚拟机-009'
   AND (title LIKE '%跨集群%' OR title LIKE '%跨群%' OR title LIKE '%集群到集群%')
   AND title LIKE '%冷迁移%';
--   ② 跨集群（缺省热迁移） → 012
UPDATE kbd_entry SET category_id = '虚拟机-012'
 WHERE category_id = '虚拟机-009'
   AND (title LIKE '%跨集群%' OR title LIKE '%跨群%' OR title LIKE '%集群到集群%');
--   ③ 集群内 + 冷迁移 + 跨存储 → 013
UPDATE kbd_entry SET category_id = '虚拟机-013'
 WHERE category_id = '虚拟机-009' AND title LIKE '%冷迁移%' AND title LIKE '%跨存储%';
--   ④ 集群内 + 冷迁移（缺省同存储） → 014
UPDATE kbd_entry SET category_id = '虚拟机-014'
 WHERE category_id = '虚拟机-009' AND title LIKE '%冷迁移%';
--   ⑤ 集群内 + 热迁移 + 跨存储 → 010
UPDATE kbd_entry SET category_id = '虚拟机-010'
 WHERE category_id = '虚拟机-009' AND title LIKE '%跨存储%';
--   ⑥ 兜底：集群内 + 热迁移 + 同存储 → 011
UPDATE kbd_entry SET category_id = '虚拟机-011'
 WHERE category_id = '虚拟机-009';

-- ── 3) 重挂错挂在分组「虚拟机-049（网络异常）」上的 KBD → 网络叶子 ─────────────────────
--   ① IP 相关（冲突 / 获取不到 / 拿不到 / 不显示） → 052
UPDATE kbd_entry SET category_id = '虚拟机-052'
 WHERE category_id = '虚拟机-049'
   AND title ILIKE '%IP%'
   AND (title LIKE '%冲突%' OR title LIKE '%获取不到%' OR title LIKE '%拿不到%' OR title LIKE '%不显示%');
--   ② 网卡相关 → 053
UPDATE kbd_entry SET category_id = '虚拟机-053'
 WHERE category_id = '虚拟机-049' AND title LIKE '%网卡%';
--   ③ 延时 / 延迟 → 051
UPDATE kbd_entry SET category_id = '虚拟机-051'
 WHERE category_id = '虚拟机-049' AND (title LIKE '%延时%' OR title LIKE '%延迟%');
--   ④ 兜底：丢包 / 网络不通 → 050
UPDATE kbd_entry SET category_id = '虚拟机-050'
 WHERE category_id = '虚拟机-049';

-- ── 4) 重挂错挂在分组「虚拟机-065（内部软件使用异常）」上的 KBD → 软件叶子 ──────────────
--   ① 极域（电子教室软件） → 069
UPDATE kbd_entry SET category_id = '虚拟机-069'
 WHERE category_id = '虚拟机-065' AND title LIKE '%极域%';
--   ② 安装 / 卸载 / 部署 → 066
UPDATE kbd_entry SET category_id = '虚拟机-066'
 WHERE category_id = '虚拟机-065'
   AND (title LIKE '%安装%' OR title LIKE '%卸载%' OR title LIKE '%部署%');
--   ③ 闪退 / 卡死 / 崩溃 / 使用异常 → 067
UPDATE kbd_entry SET category_id = '虚拟机-067'
 WHERE category_id = '虚拟机-065'
   AND (title LIKE '%闪退%' OR title LIKE '%卡死%' OR title LIKE '%崩溃%' OR title LIKE '%使用异常%');
--   ④ 兜底：操作系统组件（更新 / 激活 / 蓝屏等） → 068
UPDATE kbd_entry SET category_id = '虚拟机-068'
 WHERE category_id = '虚拟机-065';

-- ── 5) 同步 ai_category_id（若曾建议同一分组码，保持与人工绑定一致）───────────────────
UPDATE kbd_entry
   SET ai_category_id = category_id
 WHERE ai_category_id IN ('虚拟机-009', '虚拟机-049', '虚拟机-065');

-- ── 6) 递归重建 level 与 path_labels（049 改名后路径与层级自洽）────────────────────────
WITH RECURSIVE tree AS (
    SELECT id, name, parent_id, 1 AS lvl, ARRAY[name]::text[] AS names
      FROM kb_category WHERE parent_id IS NULL
    UNION ALL
    SELECT c.id, c.name, c.parent_id, t.lvl + 1, t.names || c.name
      FROM kb_category c JOIN tree t ON c.parent_id = t.id
)
UPDATE kb_category k SET level = tree.lvl, path_labels = to_jsonb(tree.names)
  FROM tree WHERE k.id = tree.id;

-- ── 7) 校验：不得再有「已发布 KBD/SOP」绑在非叶子分组上；否则整体回滚 ──────────────────
DO $verify$
DECLARE
    n_kbd int;
    n_sop int;
BEGIN
    SELECT COUNT(*) INTO n_kbd
      FROM kbd_entry k
     WHERE k.status = 'published'
       AND k.category_id IS NOT NULL AND k.category_id <> ''
       AND EXISTS (
           SELECT 1 FROM kb_category ch
            WHERE ch.parent_id = (SELECT id FROM kb_category c WHERE c.code = k.category_id)
       );
    SELECT COUNT(*) INTO n_sop
      FROM sop_document s
     WHERE s.status = 'published'
       AND s.category_id IS NOT NULL AND s.category_id <> ''
       AND EXISTS (
           SELECT 1 FROM kb_category ch
            WHERE ch.parent_id = (SELECT id FROM kb_category c WHERE c.code = s.category_id)
       );

    IF n_kbd > 0 OR n_sop > 0 THEN
        RAISE EXCEPTION
            'category_rebalance_group_bound_v1 校验失败：仍有 % 个已发布 KBD、% 个已发布 SOP 绑在非叶子分组（需补充对应分组的重挂规则后重跑）',
            n_kbd, n_sop;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM kb_category WHERE code = '虚拟机-049' AND name = '虚拟机网络异常') THEN
        RAISE EXCEPTION 'category_rebalance_group_bound_v1 校验失败：虚拟机-049 未完成改名';
    END IF;

    RAISE NOTICE 'category_rebalance_group_bound_v1 校验通过：已发布 KBD/SOP 无分组绑定，049 已更名';
END $verify$;

COMMIT;
