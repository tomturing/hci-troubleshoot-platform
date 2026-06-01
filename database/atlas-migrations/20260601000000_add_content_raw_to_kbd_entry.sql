-- migrate:up
-- KBD 双通道改造：添加 content_raw 字段用于存储纯文本，专供 LLM 和 RAG 检索/Embedding 使用
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS content_raw text;

COMMENT ON COLUMN kbd_entry.content_raw IS '纯文本内容（剔除 Markdown 格式标记和图片占位符），专供 LLM 和 RAG 检索/Embedding 使用';

-- migrate:down
ALTER TABLE kbd_entry DROP COLUMN IF EXISTS content_raw;
