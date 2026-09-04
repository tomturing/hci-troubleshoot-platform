-- 20260904000000_add_signal_modeling_assets.sql
-- 关键信号多 Agent 分工分层建模：模板库、最佳实践库、异常抽取复盘表

-- 1. 信号类型建模标准模板库 (Schema & Constraint Definition)
CREATE TABLE IF NOT EXISTS signal_modeling_template (
    id SERIAL PRIMARY KEY,
    tool_name VARCHAR(32) NOT NULL UNIQUE,
    category VARCHAR(16) NOT NULL,
    description TEXT NOT NULL,
    acquire_schema JSONB NOT NULL,
    allowed_matcher_types VARCHAR(32)[] NOT NULL,
    variable_protocol JSONB NOT NULL,
    anti_patterns TEXT[] NOT NULL DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    trace_id VARCHAR(64) NOT NULL DEFAULT 'migration:20260904000000',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
COMMENT ON TABLE signal_modeling_template IS '信号类型建模标准模板库：定义 13 类信号的输入 Schema 与参数契约';

-- 2. 信号建模最佳实践库 (Golden Few-Shot Dataset)
CREATE TABLE IF NOT EXISTS signal_best_practice (
    id SERIAL PRIMARY KEY,
    template_id INT REFERENCES signal_modeling_template(id) ON DELETE CASCADE,
    tool_name VARCHAR(32) NOT NULL,
    pattern_category VARCHAR(64) NOT NULL,
    source_kbd_id BIGINT REFERENCES kbd_entry(id) ON DELETE SET NULL,
    support_id VARCHAR(32),
    raw_evidence TEXT NOT NULL,
    signal_json JSONB NOT NULL,
    design_notes TEXT NOT NULL,
    completeness_score INT DEFAULT 10,
    is_active BOOLEAN DEFAULT TRUE,
    trace_id VARCHAR(64) NOT NULL DEFAULT 'migration:20260904000000',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signal_best_practice_tool ON signal_best_practice(tool_name) WHERE is_active = TRUE;
COMMENT ON TABLE signal_best_practice IS '信号建模最佳实践库：沉淀已发布 KBD 中专家最终审核通过的黄金实例';

-- 3. 信号抽取异常复盘日志表 (Failure Feedback Loop)
CREATE TABLE IF NOT EXISTS signal_failure_extraction (
    id BIGSERIAL PRIMARY KEY,
    kbd_id BIGINT REFERENCES kbd_entry(id) ON DELETE CASCADE,
    stage VARCHAR(32) NOT NULL,
    raw_content TEXT NOT NULL,
    reason VARCHAR(64) NOT NULL,
    detail_payload JSONB DEFAULT '{}'::jsonb,
    trace_id VARCHAR(64) NOT NULL,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_by VARCHAR(64),
    resolved_notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signal_failure_stage ON signal_failure_extraction(stage, resolved);
COMMENT ON TABLE signal_failure_extraction IS '信号抽取异常复盘日志表：沉淀计数、分类、建模、验证各阶段未通过的异常案例';

-- 兼容早期已创建但缺少链路字段的表结构。
ALTER TABLE signal_modeling_template
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64) NOT NULL DEFAULT 'migration:20260904000000';
ALTER TABLE signal_best_practice
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64) NOT NULL DEFAULT 'migration:20260904000000';
ALTER TABLE signal_failure_extraction
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64) NOT NULL DEFAULT 'migration:20260904000000';
CREATE INDEX IF NOT EXISTS idx_signal_failure_trace_id ON signal_failure_extraction(trace_id);

-- 4. 初始化 13 类标准采集工具的基础模板
INSERT INTO signal_modeling_template (tool_name, category, description, acquire_schema, allowed_matcher_types, variable_protocol, anti_patterns, trace_id)
VALUES
  ('qkv_task', 'frontend', '前端任务查询：acli task get，产出 status/host/vm/errcode_tracing/request_id 等', 
   '{"type":"object","required":["keyword"],"properties":{"keyword":{"type":"string"},"limit":{"type":"integer","default":100},"is_failed":{"type":"boolean","default":true},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY[]::varchar[], 
   '{"produces":["HOST","VM","REQUEST_ID","STATUS","ERRCODE_TRACING","TARGET","END","DATE","DESCRIPTION"],"requires":[]}'::jsonb, 
   ARRAY['禁止使用非失败任务关键词', '禁止配置 match 字段'], 'migration:20260904000000'),

  ('qkv_alert', 'frontend', '前端信号-告警查询：acli alert get，产出 host/vm/target/alert_type/end 等', 
   '{"type":"object","required":["keyword"],"properties":{"keyword":{"type":"string"},"limit":{"type":"integer","default":100},"alert_type":{"type":"string"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY[]::varchar[], 
   '{"produces":["HOST","VM","TARGET","END","DATE","ALERT_TYPE","STATUS"],"requires":[]}'::jsonb, 
   ARRAY['无 is_failed 字段', '禁止将普通任务失败当做告警'], 'migration:20260904000000'),

  ('qkv_dialog', 'frontend', '弹框复合取值：在当前主控 today 与 today/vt 日志检索弹框文本，产出 END/REQUEST_ID/HOST', 
   '{"type":"object","required":["keyword"],"properties":{"keyword":{"type":"string"},"paths":{"type":"array","items":{"type":"string"},"default":["/sf/log/today","/sf/log/today/vt"]},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY[]::varchar[], 
   '{"produces":["HOST","REQUEST_ID","END"],"requires":[]}'::jsonb, 
   ARRAY['仅适用于明确的前端界面弹窗提示'], 'migration:20260904000000'),

  ('qkv_vm_console', 'frontend', '条件型实时视觉生产者：采集虚拟机控制台截图产出 VM_CONSOLE_* 变量', 
   '{"type":"object","required":["host","vm_id"],"properties":{"host":{"type":"string"},"vm_id":{"type":"string"},"capture_mode":{"type":"string","default":"vnc"},"timeout":{"type":"integer","default":60}}}'::jsonb, 
   ARRAY[]::varchar[], 
   '{"produces":["VM_CONSOLE_TEXT","VM_CONSOLE_STATUS"],"requires":["HOST","VM_ID"]}'::jsonb, 
   ARRAY['不得并入 FRONTEND_TOOLS', '必须满足 HOST+VM_ID 先决条件'], 'migration:20260904000000'),

  ('qkv_effect', 'frontend', '条件型效果验证生产者：排障处置后状态复查', 
   '{"type":"object","properties":{"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY[]::varchar[], 
   '{"produces":["EFFECT_VERIFIED"],"requires":[]}'::jsonb, 
   ARRAY['不得作为 KBD 的唯一生产者'], 'migration:20260904000000'),

  ('qfk_log', 'backend', '统一日志判定：whitebox/blackbox/pod 均由 acli log get 获取', 
   '{"type":"object","required":["file"],"properties":{"file":{"type":"string"},"path":{"type":"string","default":"/sf/log"},"host":{"type":"string","default":"{{HOST}}"},"time_window":{"type":"string"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY['keyword','regex','state','threshold','delta','trend','exists']::varchar[], 
   '{"produces":[],"requires":["HOST"]}'::jsonb, 
   ARRAY['file 必须是纯 basename 文件名，严禁包含目录', '禁止在路径中包含 <日期> 等人工占位符'], 'migration:20260904000000'),

  ('qfk_system', 'backend', '后端信号-系统检查和操作：acli system <command>（如 lsof/ps/lsblk/iostat/df/cat）', 
   '{"type":"object","required":["command"],"properties":{"command":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"host":{"type":"string","default":"{{HOST}}"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY['keyword','regex','state','threshold','exists']::varchar[], 
   '{"produces":[],"requires":["HOST"]}'::jsonb, 
   ARRAY['禁止硬编码特定环境存储卷UUID或主机IP', '禁止使用 date/uptime+exists 恒真伪断言'], 'migration:20260904000000'),

  ('qfk_vm', 'backend', '后端信号-虚拟机相关操作：acli vm <command>', 
   '{"type":"object","required":["command"],"properties":{"command":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"host":{"type":"string","default":"{{HOST}}"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY['keyword','regex','state','threshold','exists']::varchar[], 
   '{"produces":[],"requires":["HOST","VM"]}'::jsonb, 
   ARRAY['仅限只读检查命令，禁止写操作'], 'migration:20260904000000'),

  ('qfk_service', 'backend', '服务状态：asv/anet/asan/host 服务状态探测', 
   '{"type":"object","required":["resource_keyword"],"properties":{"resource_keyword":{"type":"string"},"container":{"type":"string","default":"asv"},"command":{"type":"string","default":"status"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY['state','keyword','exists']::varchar[], 
   '{"produces":[],"requires":["HOST"]}'::jsonb, 
   ARRAY['严禁使用 restart 等处置操作'], 'migration:20260904000000'),

  ('qfk_network', 'backend', '后端信号-网络相关操作：acli network <command>', 
   '{"type":"object","required":["command"],"properties":{"command":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"host":{"type":"string","default":"{{HOST}}"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY['keyword','regex','state','exists']::varchar[], 
   '{"produces":[],"requires":["HOST"]}'::jsonb, 
   ARRAY['仅限只读网络检查'], 'migration:20260904000000'),

  ('qfk_storage', 'backend', '后端信号-存储相关操作：acli storage <command>（如 asan disk list）', 
   '{"type":"object","required":["command"],"properties":{"command":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"host":{"type":"string","default":"{{HOST}}"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY['keyword','regex','state','threshold','exists']::varchar[], 
   '{"produces":[],"requires":["HOST"]}'::jsonb, 
   ARRAY['仅限只读存储检查'], 'migration:20260904000000'),

  ('qfk_hardware', 'backend', '后端信号-硬件相关操作：acli hardware <command>', 
   '{"type":"object","required":["command"],"properties":{"command":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"host":{"type":"string","default":"{{HOST}}"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY['keyword','regex','state','exists']::varchar[], 
   '{"produces":[],"requires":["HOST"]}'::jsonb, 
   ARRAY['仅限只读硬件检查'], 'migration:20260904000000'),

  ('qfk_platform', 'backend', '后端信号-平台相关操作：acli platform <command>', 
   '{"type":"object","required":["command"],"properties":{"command":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"host":{"type":"string","default":"{{HOST}}"},"timeout":{"type":"integer","default":60},"instruction":{"type":"string"}}}'::jsonb, 
   ARRAY['keyword','regex','state','exists']::varchar[], 
   '{"produces":[],"requires":["HOST"]}'::jsonb, 
   ARRAY['仅限只读平台检查'], 'migration:20260904000000')
ON CONFLICT (tool_name) DO UPDATE SET
  category = EXCLUDED.category,
  description = EXCLUDED.description,
  acquire_schema = EXCLUDED.acquire_schema,
  allowed_matcher_types = EXCLUDED.allowed_matcher_types,
  variable_protocol = EXCLUDED.variable_protocol,
  anti_patterns = EXCLUDED.anti_patterns,
  updated_at = NOW();
