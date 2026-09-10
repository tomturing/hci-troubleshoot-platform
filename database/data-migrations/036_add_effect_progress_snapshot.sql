-- qkv_effect 跨次进展观察：保留受控观测输出的指纹与状态，不保存额外原始内容。
ALTER TABLE effect_verification_check
    ADD COLUMN IF NOT EXISTS observation_fingerprint varchar(64),
    ADD COLUMN IF NOT EXISTS progress_state varchar(32);

ALTER TABLE effect_verification_check
    DROP CONSTRAINT IF EXISTS ck_effect_check_progress_state;

ALTER TABLE effect_verification_check
    ADD CONSTRAINT ck_effect_check_progress_state CHECK (
        progress_state IS NULL
        OR (progress_state)::text = ANY ((ARRAY[
            'baseline'::varchar,
            'in_progress'::varchar,
            'stalled'::varchar,
            'inconclusive'::varchar,
            'achieved'::varchar
        ])::text[])
    );

COMMENT ON COLUMN effect_verification_check.observation_fingerprint IS '受控观测输出 SHA-256；用于跨次进展比较';
COMMENT ON COLUMN effect_verification_check.progress_state IS 'baseline/in_progress/stalled/inconclusive/achieved';
