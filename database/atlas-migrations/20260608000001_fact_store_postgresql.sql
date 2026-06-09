-- ============================================================
-- 迁移：创建 fact 和 claim_evidence_link 表，用于 FactStore 持久化 (T4-3)
-- Version : 20260608000001
-- Issue   : T-RELIABILITY-P4
-- ============================================================

-- ── UP ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fact (
    id varchar(36) NOT NULL,
    case_id varchar(20) NOT NULL,
    fact_type varchar(50) NOT NULL,
    key varchar(100) NOT NULL,
    source varchar(50) NOT NULL,
    raw_ref text,
    normalized_value jsonb NOT NULL,
    confidence numeric(4,3) NOT NULL DEFAULT 1.000,
    freshness varchar(30) NOT NULL DEFAULT 'unknown',
    conflict boolean NOT NULL DEFAULT false,
    collected_at timestamptz,
    trace_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fact_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE fact IS '排障事实记录表 — 记录从各个来源收集的结构化诊断事实数据';
COMMENT ON COLUMN fact.id IS '事实主键ID，UUID格式';
COMMENT ON COLUMN fact.case_id IS '关联工单ID';
COMMENT ON COLUMN fact.fact_type IS '事实类型（如vm_status, host_status等）';
COMMENT ON COLUMN fact.key IS '事实键名（如 vm_name, alert_count等）';
COMMENT ON COLUMN fact.source IS '事实来源：env_inject/tool_exec/user_input等';
COMMENT ON COLUMN fact.raw_ref IS '原始数据引用，如关联的工具执行ID或用户消息ID';
COMMENT ON COLUMN fact.normalized_value IS '标准化的事实数据值，JSONB存储';
COMMENT ON COLUMN fact.confidence IS '置信度评分，0.0 到 1.0 之间';
COMMENT ON COLUMN fact.freshness IS '时效性，如 current / stale / unknown';
COMMENT ON COLUMN fact.conflict IS '是否存在来源冲突';
COMMENT ON COLUMN fact.collected_at IS '事实数据实际采集的时间戳';
COMMENT ON COLUMN fact.trace_id IS '链路追踪ID';
COMMENT ON COLUMN fact.created_at IS '记录创建时间';

CREATE INDEX IF NOT EXISTS idx_fact_case_id ON fact (case_id);
CREATE INDEX IF NOT EXISTS idx_fact_type ON fact (fact_type);
CREATE INDEX IF NOT EXISTS idx_fact_case_type_key ON fact (case_id, fact_type, key);

CREATE TABLE IF NOT EXISTS claim_evidence_link (
    id varchar(36) NOT NULL,
    case_id varchar(20) NOT NULL,
    claim_id varchar(50) NOT NULL,
    fact_id varchar(36) NOT NULL,
    relation varchar(30) NOT NULL, -- supporting / contradicting
    confidence numeric(4,3) NOT NULL DEFAULT 1.000,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claim_evidence_link_pkey PRIMARY KEY (id),
    CONSTRAINT fk_claim_evidence_link_fact_id FOREIGN KEY (fact_id) REFERENCES fact (id) ON DELETE CASCADE
);

COMMENT ON TABLE claim_evidence_link IS '断言证据链关联表 — 建立诊断结论/断言与事实证据的关联，形成证据链追踪';
COMMENT ON COLUMN claim_evidence_link.id IS '关联主键ID，UUID格式';
COMMENT ON COLUMN claim_evidence_link.case_id IS '关联工单ID';
COMMENT ON COLUMN claim_evidence_link.claim_id IS 'AI诊断断言的唯一标识ID（如 claim-1）';
COMMENT ON COLUMN claim_evidence_link.fact_id IS '关联事实证据的主键ID';
COMMENT ON COLUMN claim_evidence_link.relation IS '关联关系：supporting(支持) / contradicting(反对)';
COMMENT ON COLUMN claim_evidence_link.confidence IS '该事实支持/反对该断言的置信度/相关度评分';
COMMENT ON COLUMN claim_evidence_link.created_at IS '关联创建时间';

CREATE INDEX IF NOT EXISTS idx_claim_evidence_case ON claim_evidence_link (case_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim ON claim_evidence_link (claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_fact ON claim_evidence_link (fact_id);

-- ── DOWN ─────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS claim_evidence_link;
DROP TABLE IF EXISTS fact;
