CREATE TABLE tripmate.inventory (
    inventory_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    slot_id uuid NOT NULL,
    total_quantity integer NOT NULL CHECK (total_quantity >= 0),
    available_quantity integer NOT NULL CHECK (available_quantity >= 0),
    held_quantity integer NOT NULL DEFAULT 0 CHECK (held_quantity >= 0),
    confirmed_quantity integer NOT NULL DEFAULT 0 CHECK (confirmed_quantity >= 0),
    status text NOT NULL CHECK (status IN ('ACTIVE', 'PAUSED', 'RECONCILING')),
    reconciliation_generation bigint NOT NULL DEFAULT 0 CHECK (reconciliation_generation >= 0),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (available_quantity + held_quantity + confirmed_quantity = total_quantity),
    UNIQUE (tenant_id, inventory_id),
    UNIQUE (tenant_id, slot_id),
    FOREIGN KEY (tenant_id, slot_id) REFERENCES tripmate.activity_slot (tenant_id, slot_id) ON DELETE RESTRICT
);

CREATE TABLE tripmate.reservation (
    reservation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    trip_id uuid NOT NULL,
    user_id uuid NOT NULL,
    slot_id uuid NOT NULL,
    quantity integer NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    price_snapshot numeric(12, 2) NOT NULL CHECK (price_snapshot >= 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    status text NOT NULL CHECK (status IN ('HELD', 'CONFIRMED', 'COMPLETED', 'CANCELLED', 'EXPIRED', 'WAITLISTED')),
    hold_token_hash char(64),
    hold_expires_at timestamptz,
    route text NOT NULL,
    operation text NOT NULL,
    idempotency_key_hash char(64) NOT NULL,
    request_hash char(64) NOT NULL,
    parameters_hash char(64) NOT NULL,
    approved_action_id uuid NOT NULL,
    activity_version bigint NOT NULL CHECK (activity_version > 0),
    inventory_version bigint NOT NULL CHECK (inventory_version > 0),
    policy_version text NOT NULL,
    policy_snapshot jsonb NOT NULL,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'HELD' AND hold_expires_at IS NOT NULL AND hold_token_hash IS NOT NULL) OR status <> 'HELD'),
    UNIQUE (tenant_id, reservation_id),
    UNIQUE (tenant_id, user_id, route, operation, idempotency_key_hash),
    FOREIGN KEY (tenant_id, trip_id) REFERENCES tripmate.trip (tenant_id, trip_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, slot_id) REFERENCES tripmate.activity_slot (tenant_id, slot_id) ON DELETE RESTRICT
);

CREATE TABLE tripmate.reservation_confirmation (
    confirmation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    reservation_id uuid NOT NULL,
    action_id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash char(64) NOT NULL,
    parameters_hash char(64) NOT NULL,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    status text NOT NULL CHECK (status IN ('ISSUED', 'CONSUMED', 'REJECTED', 'EXPIRED', 'REVOKED')),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    UNIQUE (tenant_id, confirmation_id),
    UNIQUE (tenant_id, reservation_id),
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES tripmate.reservation (tenant_id, reservation_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK ((status = 'ISSUED' AND consumed_at IS NULL) OR (status <> 'ISSUED' AND consumed_at IS NOT NULL))
);

ALTER TABLE tripmate.trip_item ADD CONSTRAINT trip_item_reservation_scope_fk
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES tripmate.reservation (tenant_id, reservation_id) ON DELETE RESTRICT;

CREATE INDEX inventory_slot_status_idx ON tripmate.inventory (tenant_id, slot_id, status);
CREATE INDEX reservation_expiry_idx ON tripmate.reservation (tenant_id, status, hold_expires_at) WHERE status = 'HELD';
CREATE INDEX reservation_slot_status_idx ON tripmate.reservation (tenant_id, slot_id, status);
CREATE TRIGGER inventory_updated_at BEFORE UPDATE ON tripmate.inventory
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
CREATE TRIGGER reservation_updated_at BEFORE UPDATE ON tripmate.reservation
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
