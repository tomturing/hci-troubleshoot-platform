-- 041_category_baseline_leaf_governance.sql
-- 分类基线解A（角色无关统一 ID）存量迁移 —— 确定性部分。
--
-- 背景：命名规范治理 + 叶子统一（解A）。文法唯一事实源 backend/shared/utils/category_code.py：
--   域根 {域}-L1；其余节点（叶子与分组一视同仁）统一 {域}-{序号}（3 位补零），每域共享序号空间；
--   叶子/分组纯由结构性 NOT EXISTS(parent_id) 判定。存量混用 {域}-L{级}-{序号}/{域}-L{级}-{名称} 需清洗。
--
-- 本迁移仅做「确定性」手术（无 AI 判断）：
--   1) 非合规 code 按域重编号 → {域}-{序号}（dry-run 零撞号）；
--   2) 下游引用（kbd_entry.category_id/ai_category_id、sop_document、conversation）随映射 remap；
--   3) 单子节点分组扁平化（提升唯一独子叶子、删除组）；
--   4) 清除语义验收/路由垃圾节点（E2E-*/semantic_e2e/route）及其悬空引用；
--   5) 按 parent_id 递归重建 level 与 path_labels，保证路径与层级自洽。
--
-- 不做（分离到人工签字后应用的 review_required/042）：挂在多子分组上的 KBD 逐条 LLM 重分类。
--   —— CHECK 只管 code 文法，不管叶子绑定，故与本迁移解耦：db-migrate Step1 先跑本迁移清洗数据，
--   Step3 再由 atlas 依 desired_schema.sql 施加 kb_category_code_format CHECK（存量已合规方可通过）。
--
-- 唯一调用链标记：category_leaf_governance_v1（幂等：映射 UPDATE 命中不到旧 code 即自然 no-op）。
-- 生成来源：.local/migration_plan.json（live staging 只读 dry-run，已零撞号校验）。

BEGIN;

-- ── 0) 暂时移除分类码外键（NO ACTION 且不可延迟，改名与 remap 无法同序满足）──
ALTER TABLE kbd_entry DROP CONSTRAINT IF EXISTS fk_kbd_entry_category_id;
ALTER TABLE sop_document DROP CONSTRAINT IF EXISTS fk_sop_document_category_id;

-- ── 1) 确定性重编号映射表 ──────────────────────────────────────────────
CREATE TEMP TABLE _code_remap (old_code text PRIMARY KEY, new_code text NOT NULL);
INSERT INTO _code_remap (old_code, new_code) VALUES
    ('存储-L2-001', '存储-047'),
    ('存储-L2-FC', '存储-048'),
    ('存储-L2-ISCSI', '存储-049'),
    ('存储-L2-NFS', '存储-050'),
    ('存储-L2-本地存储', '存储-051'),
    ('存储-L3-001', '存储-052'),
    ('存储-L3-002', '存储-053'),
    ('存储-L3-003', '存储-054'),
    ('存储-L3-004', '存储-055'),
    ('存储-L3-005', '存储-056'),
    ('存储-L3-006', '存储-057'),
    ('存储-L3-007', '存储-058'),
    ('存储-L3-008', '存储-059'),
    ('存储-L3-009', '存储-060'),
    ('存储-L3-010', '存储-061'),
    ('存储-L3-011', '存储-062'),
    ('存储-L3-012', '存储-063'),
    ('平台-L2-001', '平台-031'),
    ('平台-L2-002', '平台-032'),
    ('平台-L2-003', '平台-033'),
    ('平台-L2-004', '平台-034'),
    ('平台-L2-005', '平台-035'),
    ('平台-L2-006', '平台-036'),
    ('平台-L2-007', '平台-037'),
    ('平台-L2-008', '平台-038'),
    ('平台-L2-009', '平台-039'),
    ('平台-L2-010', '平台-040'),
    ('平台-L2-011', '平台-041'),
    ('平台-L2-012', '平台-042'),
    ('平台-L2-控制台登录', '平台-043'),
    ('平台-L2-集群账号密码管理', '平台-044'),
    ('平台-L3-001', '平台-045'),
    ('硬件-L2-001', '硬件-046'),
    ('硬件-L2-BIOS', '硬件-047'),
    ('硬件-L2-CPU', '硬件-048'),
    ('硬件-L2-IPMI或BMC', '硬件-049'),
    ('硬件-L2-RAID卡', '硬件-050'),
    ('硬件-L2-内存', '硬件-051'),
    ('硬件-L2-客户机硬件', '硬件-052'),
    ('硬件-L2-服务器整机', '硬件-053'),
    ('硬件-L2-电源', '硬件-054'),
    ('硬件-L2-硬盘', '硬件-055'),
    ('硬件-L2-网卡', '硬件-056'),
    ('硬件-L3-001', '硬件-057'),
    ('硬件-L3-002', '硬件-058'),
    ('硬件-L3-003', '硬件-059'),
    ('硬件-L3-004', '硬件-060'),
    ('硬件-L3-005', '硬件-061'),
    ('硬件-L3-硬盘离线或不识别', '硬件-062'),
    ('网络-L2-001', '网络-024'),
    ('网络-L2-002', '网络-025'),
    ('网络-L3-001', '网络-026'),
    ('网络-L3-002', '网络-027'),
    ('网络-L3-003', '网络-028'),
    ('网络-L3-004', '网络-029'),
    ('网络-L3-005', '网络-030'),
    ('网络-L3-006', '网络-031'),
    ('网络-L3-007', '网络-032'),
    ('网络-L3-008', '网络-033'),
    ('网络-L3-009', '网络-034'),
    ('网络-L3-010', '网络-035'),
    ('网络-L3-011', '网络-036'),
    ('网络-L3-012', '网络-037'),
    ('虚拟机-L2-001', '虚拟机-055'),
    ('虚拟机-L2-002', '虚拟机-056'),
    ('虚拟机-L2-003', '虚拟机-057'),
    ('虚拟机-L2-004', '虚拟机-058'),
    ('虚拟机-L2-005', '虚拟机-059'),
    ('虚拟机-L2-006', '虚拟机-060'),
    ('虚拟机-L2-007', '虚拟机-061'),
    ('虚拟机-L2-008', '虚拟机-062'),
    ('虚拟机-L2-009', '虚拟机-063'),
    ('虚拟机-L2-010', '虚拟机-064'),
    ('虚拟机-L2-011', '虚拟机-065'),
    ('虚拟机-L2-虚拟机集群内或跨集群迁移失败', '虚拟机-066'),
    ('虚拟机-L3-001', '虚拟机-067'),
    ('虚拟机-L3-002', '虚拟机-068'),
    ('虚拟机-L3-003', '虚拟机-069'),
    ('虚拟机-L3-004', '虚拟机-070'),
    ('虚拟机-L3-005', '虚拟机-071'),
    ('虚拟机-L3-006', '虚拟机-072'),
    ('虚拟机-L3-007', '虚拟机-073'),
    ('虚拟机-L3-008', '虚拟机-074'),
    ('虚拟机-L3-009', '虚拟机-075'),
    ('虚拟机-L4-001', '虚拟机-076'),
    ('虚拟机-L4-002', '虚拟机-077'),
    ('虚拟机-L4-003', '虚拟机-078'),
    ('虚拟机-L4-004', '虚拟机-079'),
    ('虚拟机-L4-005', '虚拟机-080'),
    ('虚拟机-L4-006', '虚拟机-081'),
    ('虚拟机-L4-007', '虚拟机-082'),
    ('虚拟机-L4-008', '虚拟机-083'),
    ('虚拟机-L4-009', '虚拟机-084'),
    ('虚拟机-L4-010', '虚拟机-085'),
    ('虚拟机-L4-011', '虚拟机-086'),
    ('虚拟机-L4-012', '虚拟机-087'),
    ('虚拟机-L4-013', '虚拟机-088'),
    ('虚拟机-L4-014', '虚拟机-089'),
    ('虚拟机-L4-015', '虚拟机-090'),
    ('虚拟机-L4-016', '虚拟机-091'),
    ('虚拟机-L4-017', '虚拟机-092'),
    ('虚拟机-L4-018', '虚拟机-093'),
    ('虚拟机-L4-019', '虚拟机-094'),
    ('虚拟机-L4-020', '虚拟机-095'),
    ('虚拟机-L4-021', '虚拟机-096'),
    ('虚拟机-L4-022', '虚拟机-097'),
    ('虚拟机-L4-023', '虚拟机-098'),
    ('虚拟机-L4-024', '虚拟机-099'),
    ('虚拟机-L4-025', '虚拟机-100'),
    ('虚拟机-L4-026', '虚拟机-101'),
    ('虚拟机-L4-027', '虚拟机-102'),
    ('虚拟机-L4-028', '虚拟机-103'),
    ('虚拟机-L4-029', '虚拟机-104'),
    ('虚拟机-L4-030', '虚拟机-105');

-- ── 2) 垃圾/悬空引用先置 NULL（避免删除节点后残留悬空）─────────────────
UPDATE conversation SET category_id = NULL WHERE category_id = ANY(ARRAY['2026-07-20','对应硬件-024']::text[]);
UPDATE kbd_entry SET ai_category_id = NULL WHERE ai_category_id = ANY(ARRAY['E2E-SEMROUTE-20260910']::text[]);
UPDATE kbd_entry SET category_id = NULL WHERE category_id = ANY(ARRAY['E2E-SEMROUTE-20260910']::text[]);

-- ── 3) 下游引用随映射 remap（旧 code -> 新 code）───────────────────────
UPDATE kbd_entry k     SET category_id   = r.new_code FROM _code_remap r WHERE k.category_id   = r.old_code;
UPDATE kbd_entry k     SET ai_category_id = r.new_code FROM _code_remap r WHERE k.ai_category_id = r.old_code;
UPDATE sop_document s  SET category_id   = r.new_code FROM _code_remap r WHERE s.category_id   = r.old_code;
UPDATE conversation c  SET category_id   = r.new_code FROM _code_remap r WHERE c.category_id   = r.old_code;

-- ── 4) 单子分组扁平化：独子上提到组的父层，再删除组 ───────────────────
UPDATE kb_category ch SET parent_id = gr.parent_id, level = gr.level
  FROM kb_category gr WHERE gr.code = '平台-L2-VMware主机' AND ch.code = '平台-017';
UPDATE kb_category ch SET parent_id = gr.parent_id, level = gr.level
  FROM kb_category gr WHERE gr.code = '硬件-L2-显卡' AND ch.code = '硬件-041';
UPDATE kb_category ch SET parent_id = gr.parent_id, level = gr.level
  FROM kb_category gr WHERE gr.code = '硬件-L2-风扇' AND ch.code = '硬件-001';
UPDATE kb_category ch SET parent_id = gr.parent_id, level = gr.level
  FROM kb_category gr WHERE gr.code = '网络-L2-分布式防火墙' AND ch.code = '网络-019';
UPDATE kb_category ch SET parent_id = gr.parent_id, level = gr.level
  FROM kb_category gr WHERE gr.code = '网络-L2-流量镜像' AND ch.code = '网络-020';
DELETE FROM kb_category WHERE code = ANY(ARRAY['平台-L2-VMware主机','硬件-L2-显卡','硬件-L2-风扇','网络-L2-分布式防火墙','网络-L2-流量镜像']::text[]);

-- ── 5) 删除垃圾节点（先叶子级、后疑似父级，满足自引用外键 NO ACTION）──
DELETE FROM kb_category WHERE code = 'E2E-SEMROUTE-20260910';
DELETE FROM kb_category WHERE code = 'E2E-semroute20260910-0';
DELETE FROM kb_category WHERE code = 'E2E-semroute20260910-1';
DELETE FROM kb_category WHERE code = 'E2E-semroute20260910-2';
DELETE FROM kb_category WHERE code = 'E2E-semroute20260910-3';
DELETE FROM kb_category WHERE code = 'E2E-semroute20260910-4';

-- ── 6) 节点 code 重命名（映射稳定保留，置于引用改写之后）───────────────
UPDATE kb_category c SET code = r.new_code FROM _code_remap r WHERE c.code = r.old_code;

-- ── 7) 递归重建 level 与 path_labels（结构自洽）────────────────────────
WITH RECURSIVE tree AS (
    SELECT id, name, parent_id, 1 AS lvl, ARRAY[name]::text[] AS names
      FROM kb_category WHERE parent_id IS NULL
    UNION ALL
    SELECT c.id, c.name, c.parent_id, t.lvl + 1, t.names || c.name
      FROM kb_category c JOIN tree t ON c.parent_id = t.id
)
UPDATE kb_category k SET level = tree.lvl, path_labels = to_jsonb(tree.names)
  FROM tree WHERE k.id = tree.id;

-- ── 8) 按 desired_schema 原定义恢复分类码外键（re-add 即自校验残留悬空）────
ALTER TABLE kbd_entry ADD CONSTRAINT fk_kbd_entry_category_id FOREIGN KEY (category_id) REFERENCES kb_category (code) ON DELETE NO ACTION;
ALTER TABLE sop_document ADD CONSTRAINT fk_sop_document_category_id FOREIGN KEY (category_id) REFERENCES kb_category (code) ON DELETE NO ACTION;

-- ── 9) 校验：仅当结果合规才提交，否则回滚 ─────────────────────────────
DO $verify$ BEGIN
    IF EXISTS (
        SELECT 1 FROM kb_category
        WHERE code IS NOT NULL
          AND NOT (code ~ '^[^-]+-L1$' OR code ~ '^[^-]+-[0-9]+$')
    ) THEN
        RAISE EXCEPTION 'category_leaf_governance_v1 校验失败：仍存在非合规 code';
    END IF;

    IF EXISTS (
        SELECT 1 FROM kbd_entry k
        WHERE k.category_id IS NOT NULL AND k.category_id <> ''
          AND NOT EXISTS (SELECT 1 FROM kb_category c WHERE c.code = k.category_id)
    ) THEN
        RAISE EXCEPTION 'category_leaf_governance_v1 校验失败：kbd_entry.category_id 出现悬空引用';
    END IF;

    RAISE NOTICE 'category_leaf_governance_v1 校验通过：code 全部合规，引用无悬空';
END $verify$;

COMMIT;
