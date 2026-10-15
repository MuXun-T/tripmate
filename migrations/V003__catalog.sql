CREATE TABLE tripmate.attraction (
    attraction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    external_source text NOT NULL,
    external_id text NOT NULL,
    city text NOT NULL,
    name text NOT NULL,
    category text NOT NULL,
    latitude numeric(9, 6),
    longitude numeric(9, 6),
    description text,
    source_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('ACTIVE', 'HIDDEN', 'DELETED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, attraction_id),
    UNIQUE (tenant_id, external_source, external_id),
    CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180)
);

CREATE TABLE tripmate.activity_slot (
    slot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    attraction_id uuid NOT NULL,
    external_id text,
    start_at timestamptz NOT NULL,
    end_at timestamptz NOT NULL,
    capacity integer NOT NULL CHECK (capacity > 0),
    price numeric(12, 2) NOT NULL CHECK (price >= 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    cancellation_policy jsonb NOT NULL,
    policy_version text NOT NULL,
    activity_version bigint NOT NULL DEFAULT 1 CHECK (activity_version > 0),
    status text NOT NULL CHECK (status IN ('ACTIVE', 'SOLD_OUT', 'CLOSED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (end_at > start_at),
    UNIQUE (tenant_id, slot_id),
    UNIQUE (tenant_id, external_id, start_at),
    FOREIGN KEY (tenant_id, attraction_id)
        REFERENCES tripmate.attraction (tenant_id, attraction_id) ON DELETE RESTRICT
);

CREATE INDEX attraction_city_status_idx ON tripmate.attraction (tenant_id, city, status);
CREATE INDEX activity_slot_window_idx ON tripmate.activity_slot (tenant_id, attraction_id, start_at);
CREATE TRIGGER attraction_updated_at BEFORE UPDATE ON tripmate.attraction
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
CREATE TRIGGER activity_slot_updated_at BEFORE UPDATE ON tripmate.activity_slot
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
