-- hci-sim 独立库：可复用 Bundle 模板（蓝图）持久化。
--
-- 背景（情况 B）：fixture.bundle.compile_input 是"已冻结、已绑定到具体 bundle"的
-- 编译输入快照（内容寻址、不可变）。它不能充当"可复用模板"——模板需要独立生命周期
-- （草稿、编辑、版本化、归属），且与 bundle 的 draft->...->published 状态机不同。
-- 把可变模板塞进 fixture.bundle 会破坏 digest 内容寻址不变量。
--
-- 本迁移新增 fixture.bundle_template：一套参数（模板+默认变量），可反复实例化，
-- 每次实例化填入具体变量 -> 送入现有编译接口 -> 产出 fixture.bundle（带 compile_input 快照）。
--
-- 设计对齐（与 000001 头注释、既有表风格一致）：
--   - 放 fixture schema（与"编译产物"语义一致）；
--   - 跨表只存不可变标识（support_id/kbd_revision），不建跨表外键；
--   - 状态机 CHECK + version 乐观锁 + 必要的去重/查找索引；
--   - 主键 UUID，模板内容本身放对象存储或 jsonb（本表存 jsonb 元数据）。
--   - 模板本身不可变寻址可选：template_digest 仅用于去重相同模板定义，不强制绑定 bundle。

CREATE TABLE IF NOT EXISTS fixture.bundle_template (
    id uuid PRIMARY KEY,
    support_id varchar(20) NOT NULL,
    kbd_revision bigint NOT NULL,
    name varchar(128) NOT NULL,
    -- 模板定义：结构对齐现有 compile_input（route 模板 + 默认变量），由控制面解释并实例化。
    template_json jsonb NOT NULL,
    -- 模板内容指纹（sha384），相同模板定义去重；允许 NULL（未计算时）。
    template_digest varchar(71),
    owner varchar(128) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    status varchar(20) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bundle_template_status CHECK (status IN ('draft', 'published', 'deprecated')),
    -- 同一 support 下模板名唯一（业务可读性约束，不阻止历史版本共存）。
    CONSTRAINT bundle_template_name_unique UNIQUE (support_id, kbd_revision, name)
);

-- 模板内容去重：相同 template_digest 视为同一模板定义，避免重复创建。
CREATE UNIQUE INDEX IF NOT EXISTS bundle_template_digest_idx
    ON fixture.bundle_template (template_digest)
    WHERE template_digest IS NOT NULL;

-- 按 support/kbd 查找可用模板（published 优先）。
CREATE INDEX IF NOT EXISTS bundle_template_support
    ON fixture.bundle_template (support_id, kbd_revision, status);

COMMENT ON TABLE fixture.bundle_template IS
    '可复用 Bundle 模板（蓝图）：一套参数，可反复实例化生成多个 fixture.bundle。'
    '与 fixture.bundle 的关系：template_json 实例化后送入现有编译接口，'
    '产出的 bundle 在其 compile_input 列冻结本次输入快照；两者通过 '
    'support_id + kbd_revision 关联（不建跨表外键，遵循只存不可变标识原则）。'
    '本表对象本身可编辑、可版本化，生命周期独立于不可变 bundle。';

COMMENT ON COLUMN fixture.bundle_template.template_json IS
    '模板定义（jsonb），结构对齐 fixture.bundle.compile_input：含 route 模板与默认变量。'
    '实例化时由控制面填入具体变量值后送入编译。';

COMMENT ON COLUMN fixture.bundle_template.template_digest IS
    '模板内容的 sha384 指纹，用于去重相同模板定义；NULL 表示尚未计算。';

COMMENT ON COLUMN fixture.bundle_template.version IS
    '乐观并发控制版本号，读-改-写时校验，防止并发覆盖。';
