-- 创建发布审核责任人表
CREATE TABLE IF NOT EXISTS kbd_review_owner (
    id bigserial NOT NULL,
    name varchar(100) NOT NULL,
    email varchar(255),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT kbd_review_owner_pkey PRIMARY KEY (id),
    CONSTRAINT uq_kbd_review_owner_email UNIQUE (email)
);

COMMENT ON TABLE kbd_review_owner IS '发布审核责任人列表';
COMMENT ON COLUMN kbd_review_owner.name IS '责任人姓名';
COMMENT ON COLUMN kbd_review_owner.email IS '责任人邮箱（可选，唯一）';

-- kbd_entry 表新增字段
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS review_owner_id bigint;
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS unpublishable_at timestamptz;
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS unpublishable_by integer;
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS unpublishable_reason text;

-- 外键约束
ALTER TABLE kbd_entry ADD CONSTRAINT fk_kbd_entry_review_owner_id
    FOREIGN KEY (review_owner_id) REFERENCES kbd_review_owner(id) ON DELETE SET NULL;

-- 索引
CREATE INDEX IF NOT EXISTS idx_kbd_entry_review_owner_id ON kbd_entry(review_owner_id);

COMMENT ON COLUMN kbd_entry.review_owner_id IS '发布审核责任人 ID';
COMMENT ON COLUMN kbd_entry.unpublishable_at IS '标记为无法发布的时间';
COMMENT ON COLUMN kbd_entry.unpublishable_by IS '标记为无法发布的操作人 ID';
COMMENT ON COLUMN kbd_entry.unpublishable_reason IS '无法发布的原因备注';
