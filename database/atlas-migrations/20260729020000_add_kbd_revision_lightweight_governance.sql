-- KBD 轻治理版本基础：只增加一张 Proposal/Expert append-only 表和最小 head 指针。
-- 不改变任何现有 dynamic_resource_active，也不回填或发布存量 KBD。

ALTER TABLE kbd_entry
    ADD COLUMN IF NOT EXISTS latest_proposal_revision_id bigint,
    ADD COLUMN IF NOT EXISTS working_revision_id bigint,
    ADD COLUMN IF NOT EXISTS lock_version integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS kbd_revision (
    id bigserial NOT NULL,
    kbd_entry_id bigint NOT NULL,
    revision_no integer NOT NULL,
    revision_type varchar(16) NOT NULL,
    parent_revision_id bigint,
    payload_json jsonb NOT NULL,
    checksum varchar(64) NOT NULL,
    generation_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    validation_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    actor_id varchar(128),
    actor_type varchar(16) NOT NULL,
    trace_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kbd_revision_pkey PRIMARY KEY (id),
    CONSTRAINT fk_kbd_revision_kbd_entry_id FOREIGN KEY (kbd_entry_id) REFERENCES kbd_entry (id) ON DELETE RESTRICT,
    CONSTRAINT fk_kbd_revision_parent_revision_id FOREIGN KEY (parent_revision_id) REFERENCES kbd_revision (id) ON DELETE RESTRICT,
    CONSTRAINT chk_kbd_revision_revision_no CHECK (revision_no > 0),
    CONSTRAINT chk_kbd_revision_type CHECK (revision_type IN ('proposal', 'expert')),
    CONSTRAINT chk_kbd_revision_actor_type CHECK (actor_type IN ('llm', 'expert', 'migration', 'system')),
    CONSTRAINT uq_kbd_revision_no UNIQUE (kbd_entry_id, revision_no)
);

CREATE INDEX IF NOT EXISTS idx_kbd_revision_entry_created ON kbd_revision (kbd_entry_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kbd_revision_parent ON kbd_revision (parent_revision_id) WHERE parent_revision_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kbd_revision_trace_id ON kbd_revision (trace_id) WHERE trace_id IS NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_kbd_entry_latest_proposal_revision_id'
          AND conrelid = 'kbd_entry'::regclass
    ) THEN
        ALTER TABLE kbd_entry
            ADD CONSTRAINT fk_kbd_entry_latest_proposal_revision_id
            FOREIGN KEY (latest_proposal_revision_id) REFERENCES kbd_revision (id) ON DELETE SET NULL;
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_kbd_entry_working_revision_id'
          AND conrelid = 'kbd_entry'::regclass
    ) THEN
        ALTER TABLE kbd_entry
            ADD CONSTRAINT fk_kbd_entry_working_revision_id
            FOREIGN KEY (working_revision_id) REFERENCES kbd_revision (id) ON DELETE SET NULL;
    END IF;
END $$;

COMMENT ON TABLE kbd_revision IS 'KBD Proposal/Expert append-only 版本；保存工作稿不影响 Agent active，发布继续使用 dynamic_resource_revision';
COMMENT ON COLUMN kbd_entry.latest_proposal_revision_id IS '最新 LLM Proposal 的 kbd_revision.id；只作显式 head 指针，不代表已发布';
COMMENT ON COLUMN kbd_entry.working_revision_id IS '当前专家工作稿的 kbd_revision.id；保存工作稿不得切换 runtime active';
COMMENT ON COLUMN kbd_entry.lock_version IS '专家工作稿乐观锁版本；每次成功保存递增，避免并发覆盖';
