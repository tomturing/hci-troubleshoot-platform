-- ===========================================================================
-- 迁移: 20260605000000_redesign_skill_definition_agent_skills_standard.sql
-- 说明: 按 Agent Skills Open Standard 重建 skill_definition 表
--       旧表 (含 parameters_schema / output_schema) 删除，新建符合标准的表结构
-- 参考: https://agentskills.io/specification
-- ===========================================================================

-- 删除旧表（旧字段 parameters_schema / output_schema 是 Tool 概念，与 Skill 本质不符）
DROP TABLE IF EXISTS skill_definition CASCADE;

-- 重建 skill_definition 表（遵循 Agent Skills Open Standard 字段语义）
CREATE TABLE skill_definition (
    id                  SERIAL NOT NULL,

    -- ===== 标准规范字段（对应 SKILL.md frontmatter 字段）=====

    -- skill_name: 对应标准 name 字段
    -- 规则：kebab-case，1-64字符，小写字母+数字+连字符，不以连字符开头/结尾，无连续连字符
    skill_name          VARCHAR(64) NOT NULL UNIQUE,

    -- description: 供 Agent 发现阶段使用，描述"做什么"和"何时触发"
    -- 最长 1024 字符（与标准一致），必须包含触发条件关键词，Agent 启动时只读此字段
    description         VARCHAR(1024) NOT NULL,

    -- instructions_md: 对应 SKILL.md 正文 Markdown（供 Agent 激活阶段加载）
    -- 内容包含：Step-by-step 指令 + Gotchas + 示例 + 输出模板等，建议 < 500 行
    instructions_md     TEXT NOT NULL DEFAULT '',

    -- compatibility: 对应标准 compatibility 字段，最长 500 字符
    -- 说明环境依赖（OS 版本、工具、网络权限等），多数 Skill 不需要此字段
    compatibility       VARCHAR(500),

    -- license: 对应标准 license 字段，许可证名称或内置文件路径
    license             VARCHAR(100),

    -- allowed_tools: 对应标准 allowed-tools 字段（实验性），空格分隔的预批准工具列表
    allowed_tools       TEXT,

    -- metadata_json: 对应标准 metadata 字段，任意 key-value 扩展元数据
    -- 建议字段：{"author": "hci-team", "category": "storage", "tags": ["disk", "smart"]}
    metadata_json       JSONB NOT NULL DEFAULT '{}',

    -- ===== 平台扩展字段（超出标准规范，满足企业管理平台需求）=====

    -- display_name: 中文展示名（非标准字段，管理控制台展示用）
    display_name        VARCHAR(200),

    -- is_active: 启用开关（非标准字段，平台管理需求）
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    -- assets_json: 轻量资产内联存储，模拟标准 assets/ 目录（非标准字段）
    -- 格式：[{"filename": "template.md", "type": "template", "content": "..."}]
    assets_json         JSONB NOT NULL DEFAULT '[]',

    -- references_json: 参考文档内联存储，模拟标准 references/ 目录（非标准字段）
    -- 格式：[{"filename": "REFERENCE.md", "title": "诊断参考手册", "content": "..."}]
    references_json     JSONB NOT NULL DEFAULT '[]',

    trace_id            VARCHAR(64),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT skill_definition_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE skill_definition IS '技能定义表 — 遵循 Agent Skills Open Standard (agentskills.io)，以 Markdown 知识包形式存储领域专业知识和标准操作流程';
COMMENT ON COLUMN skill_definition.skill_name IS 'Skill 唯一标识，kebab-case，对应标准 name 字段。SOP 变量源引用格式：skill:skill-name';
COMMENT ON COLUMN skill_definition.description IS '供 Agent 发现阶段使用（~100 tokens），描述"做什么"和"何时触发"，最长 1024 字符';
COMMENT ON COLUMN skill_definition.instructions_md IS 'SKILL.md 正文 Markdown，供 Agent 激活阶段加载，建议不超过 500 行 / 5000 tokens';
COMMENT ON COLUMN skill_definition.compatibility IS '环境兼容性说明（可选），描述系统版本、工具依赖、网络权限等';
COMMENT ON COLUMN skill_definition.license IS '许可证（可选）';
COMMENT ON COLUMN skill_definition.allowed_tools IS '预批准工具列表，空格分隔（实验性字段）';
COMMENT ON COLUMN skill_definition.metadata_json IS '扩展元数据 key-value，建议包含 author、category、tags';
COMMENT ON COLUMN skill_definition.display_name IS '中文展示名，管理控制台使用（平台扩展字段）';
COMMENT ON COLUMN skill_definition.is_active IS '启用状态；false 时 Agent 不会激活此 Skill（平台扩展字段）';
COMMENT ON COLUMN skill_definition.assets_json IS '资源文件内联存储，模拟标准 assets/ 目录（平台扩展字段）';
COMMENT ON COLUMN skill_definition.references_json IS '参考文档内联存储，模拟标准 references/ 目录（平台扩展字段）';
COMMENT ON COLUMN skill_definition.created_at IS '创建时间';
COMMENT ON COLUMN skill_definition.updated_at IS '最后更新时间';

-- 索引
CREATE INDEX idx_skill_definition_active   ON skill_definition (is_active);
CREATE INDEX idx_skill_definition_metadata ON skill_definition USING GIN (metadata_json);
