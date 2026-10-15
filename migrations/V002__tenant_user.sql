CREATE TABLE tripmate.tenant (
    tenant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'DELETED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tripmate.user_profile (
    user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 120),
    locale text NOT NULL DEFAULT 'zh-CN',
    timezone text NOT NULL DEFAULT 'Asia/Shanghai',
    redacted_contact jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED', 'DELETED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, user_id)
);

CREATE INDEX user_profile_scope_idx ON tripmate.user_profile (tenant_id, status, updated_at);
CREATE TRIGGER tenant_updated_at BEFORE UPDATE ON tripmate.tenant
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
CREATE TRIGGER user_profile_updated_at BEFORE UPDATE ON tripmate.user_profile
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
