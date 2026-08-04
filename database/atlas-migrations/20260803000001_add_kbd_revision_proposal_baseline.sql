-- 为 Expert Revision 冻结其真实 AI Proposal 基线。
-- 历史仍保持 append-only；本迁移只补充血缘指针，不删除或改写 payload_json。

ALTER TABLE kbd_revision
    ADD COLUMN IF NOT EXISTS baseline_proposal_revision_id bigint;

WITH RECURSIVE expert_ancestry AS (
    SELECT id AS expert_revision_id,
           parent_revision_id AS ancestor_revision_id,
           1 AS depth
    FROM kbd_revision
    WHERE revision_type = 'expert'
      AND baseline_proposal_revision_id IS NULL

    UNION ALL

    SELECT ancestry.expert_revision_id,
           parent.parent_revision_id,
           ancestry.depth + 1
    FROM expert_ancestry AS ancestry
    JOIN kbd_revision AS parent ON parent.id = ancestry.ancestor_revision_id
    WHERE parent.revision_type <> 'proposal'
      AND parent.parent_revision_id IS NOT NULL
      AND ancestry.depth < 1000
), nearest_proposal AS (
    SELECT DISTINCT ON (ancestry.expert_revision_id)
           ancestry.expert_revision_id,
           ancestry.ancestor_revision_id AS proposal_revision_id
    FROM expert_ancestry AS ancestry
    JOIN kbd_revision AS proposal ON proposal.id = ancestry.ancestor_revision_id
    WHERE proposal.revision_type = 'proposal'
    ORDER BY ancestry.expert_revision_id, ancestry.depth
)
UPDATE kbd_revision AS expert
SET baseline_proposal_revision_id = nearest_proposal.proposal_revision_id
FROM nearest_proposal
WHERE expert.id = nearest_proposal.expert_revision_id
  AND expert.baseline_proposal_revision_id IS NULL;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_kbd_revision_baseline_proposal_revision_id'
          AND conrelid = 'kbd_revision'::regclass
    ) THEN
        ALTER TABLE kbd_revision
            ADD CONSTRAINT fk_kbd_revision_baseline_proposal_revision_id
            FOREIGN KEY (baseline_proposal_revision_id)
            REFERENCES kbd_revision (id)
            ON DELETE RESTRICT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kbd_revision_baseline_proposal
    ON kbd_revision (baseline_proposal_revision_id)
    WHERE baseline_proposal_revision_id IS NOT NULL;

COMMENT ON COLUMN kbd_revision.baseline_proposal_revision_id IS
    'Expert Revision 明确审核的 Proposal 基线；统计和评估配对不得按 history 顺序猜测';
