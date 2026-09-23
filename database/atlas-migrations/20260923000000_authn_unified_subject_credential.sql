-- 认证系统（auth-service）：统一主体 + 通用凭证
-- 设计文档: docs/solution/events/2026-09-23-统一认证系统设计方案.md
-- 说明: customer 与 admin 均为 user，差异仅在 realm / 凭证 / 角色。
--       全部语句幂等（IF NOT EXISTS），可重复执行。

-- 1) 扩展 user 表：主体域 / 状态 / 角色 / 令牌版本号
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS realm         varchar(16) NOT NULL DEFAULT 'customer';
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS status        varchar(16) NOT NULL DEFAULT 'active';
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS nickname      varchar(128);
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS avatar_url    text;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS roles         jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS token_version int NOT NULL DEFAULT 1;
-- admin 无端侧 client_id，放宽为可空（UNIQUE 允许多个 NULL）
ALTER TABLE "user" ALTER COLUMN client_id DROP NOT NULL;

COMMENT ON COLUMN "user".realm IS '主体域：customer（客户）/ admin（管理员）；两套认证按 realm + aud 强隔离';
COMMENT ON COLUMN "user".status IS '账号状态：active / disabled；停用须同时递增 token_version 使已签发令牌失效';
COMMENT ON COLUMN "user".roles IS '角色数组 jsonb，如 ["platform_admin"]；DB 层无 CHECK，写入须经应用层白名单校验';
COMMENT ON COLUMN "user".token_version IS '令牌版本号；改角色/停用/改密时 +1，使已签发 JWT 立即失效';

CREATE INDEX IF NOT EXISTS idx_user_roles ON "user" USING gin (roles);
CREATE INDEX IF NOT EXISTS idx_user_realm_status ON "user" (realm, status);

-- 2) 通用凭证表（1:N）
CREATE TABLE IF NOT EXISTS user_credential (
    credential_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES "user"(user_id) ON DELETE CASCADE,
    credential_type varchar(32)  NOT NULL,
    identifier      varchar(255) NOT NULL,
    secret          text,
    extra           jsonb NOT NULL DEFAULT '{}'::jsonb,
    failed_attempts int NOT NULL DEFAULT 0,
    locked_until    timestamptz,
    status          varchar(16) NOT NULL DEFAULT 'active',
    last_used_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    trace_id        varchar(64),
    CONSTRAINT uq_user_credential_type_identifier UNIQUE (credential_type, identifier)
);

COMMENT ON TABLE user_credential IS '通用凭证表 — 一个用户多种凭证（密码/微信/手机/OIDC），1:N；唯一性按 (类型, 标识) 隔离';
COMMENT ON COLUMN user_credential.secret IS '凭证密钥：password 存 argon2id 哈希，wechat 存 openid；禁止存明文口令';
COMMENT ON COLUMN user_credential.failed_attempts IS '连续失败次数，达阈值锁定；成功登录后清零';
COMMENT ON COLUMN user_credential.trace_id IS '创建该凭证的请求追踪 ID（W3C traceparent）';

CREATE INDEX IF NOT EXISTS idx_user_credential_user ON user_credential (user_id);
CREATE INDEX IF NOT EXISTS idx_user_credential_status ON user_credential (credential_type, status);

-- 3) 登录会话表（认证运行时域，与 SSE 凭证表 session 语义不同）
CREATE TABLE IF NOT EXISTS auth_session (
    session_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES "user"(user_id) ON DELETE CASCADE,
    realm          varchar(16) NOT NULL,
    token_version  int NOT NULL DEFAULT 1,
    refresh_hash   varchar(128),
    ip             varchar(64),
    user_agent     text,
    expires_at     timestamptz NOT NULL,
    revoked_at     timestamptz,
    last_active_at timestamptz NOT NULL DEFAULT now(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    trace_id       varchar(64)
);

COMMENT ON TABLE auth_session IS '登录会话表 — 认证运行时域；与 SSE 凭证表 session（绑工单）语义不同，勿混用';
COMMENT ON COLUMN auth_session.token_version IS '签发时的令牌版本号，与 user.token_version 比对实现即时失效';
COMMENT ON COLUMN auth_session.revoked_at IS '吊销时刻，非 NULL 表示已登出或被强制下线';

CREATE INDEX IF NOT EXISTS idx_auth_session_user ON auth_session (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_session_active ON auth_session (revoked_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_session_trace_id ON auth_session (trace_id);

-- 4) 认证安全审计
CREATE TABLE IF NOT EXISTS auth_audit (
    audit_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid REFERENCES "user"(user_id) ON DELETE SET NULL,
    realm       varchar(16),
    action      varchar(64)  NOT NULL,
    result      varchar(16)  NOT NULL DEFAULT 'success',
    identifier  varchar(255),
    ip          varchar(64),
    user_agent  text,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace_id    varchar(64)  NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE auth_audit IS '认证安全审计 — 登录/登出/改密/凭证变更/账号启停，append-only';
COMMENT ON COLUMN auth_audit.action IS '动作：login / logout / register / change_password / reset_credential / enable / disable';
COMMENT ON COLUMN auth_audit.result IS '结果：success / denied / failed';

CREATE INDEX IF NOT EXISTS idx_auth_audit_user_time ON auth_audit (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_audit_action_result ON auth_audit (action, result, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_audit_trace_id ON auth_audit (trace_id);

-- 5) 通用操作审计（跨模块，字段范式对齐 diagnosis_management_audit）
CREATE TABLE IF NOT EXISTS operation_audit (
    audit_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id uuid REFERENCES "user"(user_id) ON DELETE SET NULL,
    actor_display varchar(128),
    actor_realm   varchar(16),
    actor_roles   jsonb NOT NULL DEFAULT '[]'::jsonb,
    action        varchar(64)  NOT NULL,
    resource_type varchar(64)  NOT NULL,
    resource_id   varchar(128),
    result        varchar(16)  NOT NULL DEFAULT 'success',
    details       jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace_id      varchar(64)  NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE operation_audit IS '通用操作审计 — 跨模块账号级追溯，append-only；字段范式对齐 diagnosis_management_audit';
COMMENT ON COLUMN operation_audit.actor_user_id IS '操作者账号（FK，可 join）；服务/系统操作为 NULL';
COMMENT ON COLUMN operation_audit.actor_display IS '操作者显示快照（服务名/系统/已删除账号仍可读）';

CREATE INDEX IF NOT EXISTS idx_operation_audit_actor_time ON operation_audit (actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_audit_resource ON operation_audit (resource_type, resource_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_audit_trace_id ON operation_audit (trace_id);

-- 6) 现有审计表补齐 actor_user_id（可空，兼容服务/系统操作）
ALTER TABLE diagnosis_management_audit ADD COLUMN IF NOT EXISTS actor_user_id uuid REFERENCES "user"(user_id) ON DELETE SET NULL;
ALTER TABLE diagnosis_legal_hold_audit ADD COLUMN IF NOT EXISTS actor_user_id uuid REFERENCES "user"(user_id) ON DELETE SET NULL;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS actor_user_id uuid REFERENCES "user"(user_id) ON DELETE SET NULL;
ALTER TABLE dynamic_resource_usage_audit ADD COLUMN IF NOT EXISTS actor_user_id uuid REFERENCES "user"(user_id) ON DELETE SET NULL;
ALTER TABLE vm_console_audit_event ADD COLUMN IF NOT EXISTS actor_user_id uuid REFERENCES "user"(user_id) ON DELETE SET NULL;

COMMENT ON COLUMN diagnosis_management_audit.actor_user_id IS '操作者账号（FK）；与 actor_id 字符串快照并存，用于 join 追溯';
COMMENT ON COLUMN vm_console_audit_event.actor_user_id IS '操作者账号（FK）；与 actor 字符串快照并存';

CREATE INDEX IF NOT EXISTS idx_diagnosis_management_audit_actor_user ON diagnosis_management_audit (actor_user_id);
CREATE INDEX IF NOT EXISTS idx_vm_console_audit_event_actor_user ON vm_console_audit_event (actor_user_id);
