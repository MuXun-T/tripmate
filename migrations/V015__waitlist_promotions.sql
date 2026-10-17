CREATE TABLE tripmate.waitlist_promotion (
    promotion_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    reservation_id uuid NOT NULL,
    user_id uuid NOT NULL,
    action_id uuid NOT NULL,
    price_snapshot numeric(12, 2) NOT NULL CHECK (price_snapshot >= 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    activity_version bigint NOT NULL CHECK (activity_version > 0),
    inventory_version bigint NOT NULL CHECK (inventory_version > 0),
    reservation_version bigint NOT NULL CHECK (reservation_version > 0),
    parameters_hash char(64) NOT NULL,
    token_hash char(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('PROPOSED', 'CONFIRMED', 'EXPIRED', 'REJECTED', 'STALE')),
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, promotion_id),
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES tripmate.reservation (tenant_id, reservation_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, action_id) REFERENCES tripmate.agent_action (tenant_id, action_id)
        ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK ((status = 'PROPOSED' AND consumed_at IS NULL) OR (status <> 'PROPOSED' AND consumed_at IS NOT NULL))
);
CREATE UNIQUE INDEX waitlist_promotion_open_uq
    ON tripmate.waitlist_promotion (tenant_id, reservation_id)
    WHERE status = 'PROPOSED';
CREATE INDEX waitlist_promotion_user_idx
    ON tripmate.waitlist_promotion (tenant_id, user_id, created_at DESC);
CREATE TRIGGER waitlist_promotion_updated_at BEFORE UPDATE ON tripmate.waitlist_promotion
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();

ALTER TABLE tripmate.notification_inbox
    ALTER COLUMN reminder_id DROP NOT NULL;
ALTER TABLE tripmate.notification_inbox
    ADD COLUMN promotion_id uuid;
ALTER TABLE tripmate.notification_inbox
    ADD CONSTRAINT notification_inbox_promotion_fk
    FOREIGN KEY (tenant_id, promotion_id) REFERENCES tripmate.waitlist_promotion (tenant_id, promotion_id)
    ON DELETE RESTRICT;
ALTER TABLE tripmate.notification_inbox
    ADD CONSTRAINT notification_inbox_reminder_or_promotion_ck
    CHECK ((reminder_id IS NOT NULL) <> (promotion_id IS NOT NULL));
CREATE UNIQUE INDEX notification_inbox_promotion_uq
    ON tripmate.notification_inbox (tenant_id, promotion_id)
    WHERE promotion_id IS NOT NULL;
