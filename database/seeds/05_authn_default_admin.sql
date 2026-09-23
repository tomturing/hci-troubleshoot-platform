-- 默认管理员账号（阶段1.x）：admin / aihci@aclient2025
-- 角色 platform_admin；密码以 argon2id 哈希入库（不可逆，安全入仓）。
-- 用于首次登录 admin-ui；上线后请尽快改密 / 轮换凭证。
-- 幂等：按 user_id 主键、credential (type,identifier) 唯一键 DO NOTHING。

INSERT INTO "user" (user_id, realm, status, roles, token_version, created_at)
VALUES ('00000000-0000-0000-0000-000000000001'::uuid, 'admin', 'active', '["platform_admin"]'::jsonb, 1, now())
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO user_credential (user_id, credential_type, identifier, secret, status, created_at)
VALUES (
    '00000000-0000-0000-0000-000000000001'::uuid,
    'password',
    'admin',
    '$argon2id$v=19$m=65536,t=3,p=4$y+5rLAyhhKDw60HvCItqLw$mKmEbbwC4vjGCYxL++zOEzXaSLZ8PlXBhz+D4dHW4no',
    'active',
    now()
)
ON CONFLICT (credential_type, identifier) DO NOTHING;
