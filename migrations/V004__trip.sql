CREATE TABLE tripmate.trip (
    trip_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    owner_user_id uuid NOT NULL,
    destination text NOT NULL CHECK (length(destination) BETWEEN 1 AND 120),
    start_date date NOT NULL,
    end_date date NOT NULL,
    timezone text NOT NULL,
    party_size integer NOT NULL CHECK (party_size BETWEEN 1 AND 100),
    budget_limit numeric(12, 2) CHECK (budget_limit >= 0),
    currency char(3),
    companion_type text CHECK (companion_type IN ('solo', 'couple', 'family', 'elderly', 'mixed', 'other')),
    status text NOT NULL CHECK (status IN ('DRAFT', 'PLANNING', 'READY', 'INFEASIBLE', 'CONFIRMED', 'COMPLETED', 'CANCELLED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (end_date >= start_date),
    UNIQUE (tenant_id, trip_id),
    FOREIGN KEY (tenant_id, owner_user_id)
        REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT
);

CREATE TABLE tripmate.trip_member (
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    trip_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('OWNER', 'EDITOR', 'VIEWER')),
    status text NOT NULL CHECK (status IN ('INVITED', 'ACTIVE', 'REMOVED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, trip_id, user_id),
    FOREIGN KEY (tenant_id, trip_id) REFERENCES tripmate.trip (tenant_id, trip_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT
);

CREATE TABLE tripmate.trip_item (
    trip_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    trip_id uuid NOT NULL,
    sequence_no integer NOT NULL CHECK (sequence_no > 0),
    attraction_id uuid,
    slot_id uuid,
    start_at timestamptz NOT NULL,
    end_at timestamptz NOT NULL,
    cost numeric(12, 2) NOT NULL DEFAULT 0 CHECK (cost >= 0),
    currency char(3),
    constraint_status text NOT NULL CHECK (constraint_status IN ('PASS', 'FAIL', 'UNKNOWN', 'NOT_EVALUATED')),
    reservation_id uuid,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (end_at > start_at),
    CHECK (attraction_id IS NOT NULL OR slot_id IS NOT NULL),
    UNIQUE (tenant_id, trip_id, sequence_no),
    FOREIGN KEY (tenant_id, trip_id) REFERENCES tripmate.trip (tenant_id, trip_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, attraction_id) REFERENCES tripmate.attraction (tenant_id, attraction_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, slot_id) REFERENCES tripmate.activity_slot (tenant_id, slot_id) ON DELETE RESTRICT
);

CREATE INDEX trip_owner_status_idx ON tripmate.trip (tenant_id, owner_user_id, status, updated_at DESC);
CREATE TRIGGER trip_updated_at BEFORE UPDATE ON tripmate.trip
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
CREATE TRIGGER trip_item_updated_at BEFORE UPDATE ON tripmate.trip_item
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
