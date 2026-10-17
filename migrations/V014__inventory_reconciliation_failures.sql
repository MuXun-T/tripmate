ALTER TABLE tripmate.knowledge_manifest
    ADD COLUMN failure_reason text;

CREATE TABLE tripmate.inventory_reconciliation (
    reconciliation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE CASCADE,
    slot_id uuid NOT NULL,
    reservation_id uuid,
    expected_generation bigint NOT NULL CHECK (expected_generation >= 0),
    expected_version bigint NOT NULL CHECK (expected_version > 0),
    status text NOT NULL CHECK (status IN ('PENDING', 'PROCESSING', 'RESOLVED', 'FAILED')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner text,
    lease_until timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error_code text,
    last_error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, reconciliation_id),
    FOREIGN KEY (tenant_id, slot_id) REFERENCES tripmate.activity_slot (tenant_id, slot_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES tripmate.reservation (tenant_id, reservation_id) ON DELETE CASCADE,
    CHECK ((status = 'PROCESSING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR status <> 'PROCESSING')
);
CREATE UNIQUE INDEX inventory_reconciliation_open_uq
    ON tripmate.inventory_reconciliation (tenant_id, slot_id)
    WHERE status IN ('PENDING', 'PROCESSING', 'FAILED');
CREATE INDEX inventory_reconciliation_due_idx
    ON tripmate.inventory_reconciliation (tenant_id, next_attempt_at, reconciliation_id)
    WHERE status IN ('PENDING', 'FAILED');
CREATE TRIGGER inventory_reconciliation_updated_at BEFORE UPDATE
    ON tripmate.inventory_reconciliation FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
