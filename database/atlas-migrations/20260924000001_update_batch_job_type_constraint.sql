-- 更新 kbd_batch_job 的 job_type 约束，支持新增的任务类型
ALTER TABLE kbd_batch_job DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_type;
ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_type
    CHECK (job_type::text = ANY (ARRAY[
        'reanalyze_images'::character varying::text,
        'reclassify'::character varying::text,
        'extract_signals'::character varying::text,
        'approve'::character varying::text,
        'reject'::character varying::text,
        'set_review_owner'::character varying::text,
        'set_unpublishable'::character varying::text
    ]));
