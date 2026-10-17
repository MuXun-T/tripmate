ALTER TABLE tripmate.private_case_vector
    ADD COLUMN embedding double precision[];
ALTER TABLE tripmate.private_case_vector
    ADD COLUMN source_preference_id uuid;
ALTER TABLE tripmate.private_case_vector
    ADD CONSTRAINT private_case_vector_preference_fk
    FOREIGN KEY (tenant_id, source_preference_id)
    REFERENCES tripmate.user_preference (tenant_id, preference_id) ON DELETE RESTRICT;
ALTER TABLE tripmate.private_case_vector
    ADD CONSTRAINT private_case_vector_embedding_dimensions_ck
    CHECK (embedding IS NULL OR array_length(embedding, 1) = 384);
CREATE UNIQUE INDEX private_case_vector_preference_uq
    ON tripmate.private_case_vector (tenant_id, source_preference_id)
    WHERE source_preference_id IS NOT NULL AND tombstoned_at IS NULL;

CREATE TABLE tripmate.reminder (
    reminder_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    trip_id uuid NOT NULL,
    user_id uuid NOT NULL,
    action_id uuid NOT NULL,
    notification_id uuid NOT NULL,
    dedup_key char(64) NOT NULL,
    reminder_type text NOT NULL CHECK (reminder_type IN ('departure', 'reservation', 'weather_check')),
    scheduled_at timestamptz NOT NULL,
    timezone text NOT NULL,
    status text NOT NULL CHECK (status IN ('SCHEDULED', 'SENDING', 'RETRYING', 'SENT', 'FAILED', 'CANCELLED')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner text,
    lease_until timestamptz,
    fencing_version bigint NOT NULL DEFAULT 0 CHECK (fencing_version >= 0),
    last_error_code text,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, reminder_id),
    UNIQUE (tenant_id, user_id, dedup_key),
    UNIQUE (notification_id),
    FOREIGN KEY (tenant_id, trip_id) REFERENCES tripmate.trip (tenant_id, trip_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, action_id) REFERENCES tripmate.agent_action (tenant_id, action_id) ON DELETE RESTRICT,
    CHECK ((status = 'SENDING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR status <> 'SENDING')
);
CREATE INDEX reminder_due_idx ON tripmate.reminder (tenant_id, scheduled_at, reminder_id)
    WHERE status IN ('SCHEDULED', 'RETRYING');
CREATE INDEX reminder_user_idx ON tripmate.reminder (tenant_id, user_id, created_at DESC);
CREATE TRIGGER reminder_updated_at BEFORE UPDATE ON tripmate.reminder
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();

CREATE TABLE tripmate.notification_inbox (
    notification_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    reminder_id uuid NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    status text NOT NULL DEFAULT 'UNREAD' CHECK (status IN ('UNREAD', 'READ')),
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at timestamptz,
    UNIQUE (tenant_id, notification_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, reminder_id) REFERENCES tripmate.reminder (tenant_id, reminder_id) ON DELETE RESTRICT,
    CHECK ((status = 'UNREAD' AND read_at IS NULL) OR (status = 'READ' AND read_at IS NOT NULL))
);
CREATE INDEX notification_inbox_user_idx
    ON tripmate.notification_inbox (tenant_id, user_id, created_at DESC);
