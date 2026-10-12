-- 044_reclassify_published_kbd_category.sql —— 需专家签字后手工应用（REQUIRES HUMAN SIGN-OFF）。
--
-- 落点：位于 data-migrations/review_required/ 子目录；migration-runner 仅 glob 顶层 *.sql，
--   故本迁移不会被自动应用（也不会在 CI 空库上跑）——因为它引用具体 support_id 与环境相关的
--   分类码（如 虚拟机-077/080），只对有真实数据的环境有意义，需人工按环境复核后执行。
--   已在 staging 手工应用并记账 migration_history(version='044')；晋级生产前需人工确认目标码后重跑。
--
--
-- 背景：对全部 192 条已发布 KBD 的人工确认分类（kbd_entry.category_id，非 ai_category_id）
--   做全量审计，结构层面 192/192 干净（无空/悬空/停用/非叶子）。但语义复核发现 6 条
--   错分（多因历史上折叠「虚拟机迁移」分组时被批量改绑到 虚拟机-006，而内容实为创建/迁移/网络），
--   经审核人逐条确认目标叶子后定向改绑。
--
-- 唯一调用链标记：kbd_reclassify_published_v1。
--
-- 幂等性 & 安全性：本迁移只把 kbd_entry.category_id 从一个「已存在叶子 code」改到另一个
--   「已存在叶子 code」，不改写任何 kb_category.code，故 fk_kbd_entry_category_id
--   （REFERENCES kb_category(code)）始终满足；重复执行 SET 同值即自然 no-op。
--   category_id 不参与 embedding 输入（embedding 用问题侧字段），改绑不触发向量重算。
--
-- 目标叶子（均已核验 active=TRUE 且为叶子）：
--   虚拟机-001 虚拟机创建失败 / 虚拟机-017 集群内或跨集群迁移失败 /
--   虚拟机-077 虚拟机网络丢包或不通 / 虚拟机-080 虚拟机网卡状态异常。

BEGIN;

-- ── 1) 定向改绑 6 条（按 support_id 精确命中，幂等）──────────────────────────────
UPDATE kbd_entry SET category_id = '虚拟机-001' WHERE support_id = '41357' AND status = 'published';  -- 单主机维护模式致存储离线、创建虚拟机卡10%
UPDATE kbd_entry SET category_id = '虚拟机-001' WHERE support_id = '40026' AND status = 'published';  -- 桌面云不支持 Ubuntu、虚拟机安装异常需创建 HCI 类型
UPDATE kbd_entry SET category_id = '虚拟机-001' WHERE support_id = '39292' AND status = 'published';  -- 回收站授权未彻底删除→创建同类型配额不足
UPDATE kbd_entry SET category_id = '虚拟机-017' WHERE support_id = '39562' AND status = 'published';  -- 跨集群迁移后原存储卷未释放、垃圾清理残留
UPDATE kbd_entry SET category_id = '虚拟机-077' WHERE support_id = '38978' AND status = 'published';  -- 英伟达 DLS 配置 IP、网卡连接被超时断开
UPDATE kbd_entry SET category_id = '虚拟机-080' WHERE support_id = '25893' AND status = 'published';  -- UOS 跨集群迁移后取不到 IP（HWADDR 不匹配）→网卡状态异常

-- ── 2) 校验：6 条须已落到期望叶子；期望码须均为有效叶子；否则整体回滚 ──────────────
DO $verify$
DECLARE
    n_bad_bind   int;   -- 改绑后 category_id 与期望不符的条数
    n_bad_leaf   int;   -- 期望目标码中存在「非叶子/不存在/停用」的个数
    n_off_group  int;   -- 全库仍绑在非叶子分组上的已发布 KBD 数（不变量）
BEGIN
    -- 2.1 逐条比对期望值
    SELECT COUNT(*) INTO n_bad_bind
      FROM (VALUES
              ('41357','虚拟机-001'),('40026','虚拟机-001'),('39292','虚拟机-001'),
              ('39562','虚拟机-017'),('38978','虚拟机-077'),('25893','虚拟机-080')
           ) AS exp(sid, want)
      JOIN kbd_entry k ON k.support_id = exp.sid
     WHERE k.category_id IS DISTINCT FROM exp.want;

    -- 2.2 期望目标码必须都是「存在 + active + 叶子」
    SELECT COUNT(*) INTO n_bad_leaf
      FROM (VALUES ('虚拟机-001'),('虚拟机-017'),('虚拟机-077'),('虚拟机-080')) AS t(code)
     WHERE NOT EXISTS (
         SELECT 1 FROM kb_category c
          WHERE c.code = t.code AND c.is_active = TRUE
            AND NOT EXISTS (SELECT 1 FROM kb_category ch WHERE ch.parent_id = c.id)
     );

    -- 2.3 全局不变量：不得有已发布 KBD 绑在非叶子分组上
    SELECT COUNT(*) INTO n_off_group
      FROM kbd_entry k
     WHERE k.status = 'published'
       AND k.category_id IS NOT NULL AND k.category_id <> ''
       AND EXISTS (
           SELECT 1 FROM kb_category ch
            WHERE ch.parent_id = (SELECT id FROM kb_category c WHERE c.code = k.category_id)
       );

    IF n_bad_bind > 0 THEN
        RAISE EXCEPTION 'kbd_reclassify_published_v1 校验失败：% 条改绑后 category_id 与期望不符', n_bad_bind;
    END IF;
    IF n_bad_leaf > 0 THEN
        RAISE EXCEPTION 'kbd_reclassify_published_v1 校验失败：% 个期望目标码非有效叶子（不存在/停用/分组）', n_bad_leaf;
    END IF;
    IF n_off_group > 0 THEN
        RAISE EXCEPTION 'kbd_reclassify_published_v1 校验失败：全库仍有 % 个已发布 KBD 绑在非叶子分组', n_off_group;
    END IF;

    RAISE NOTICE 'kbd_reclassify_published_v1 校验通过：6 条已落到期望叶子，全库已发布 KBD 无分组绑定';
END $verify$;

COMMIT;
