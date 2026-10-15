CREATE TABLE tripmate.agent_run (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    user_id uuid NOT NULL,
    trip_id uuid,
    thread_id text NOT NULL,
    intent text NOT NULL,
    status text NOT NULL CHECK (status IN ('RUNNING', 'WAITING_CONFIRMATION', 'COMPLETED', 'FAILED', 'CANCELLED')),
    checkpoint_ref text,
    awaiting_action_id uuid,
    resume_command jsonb,
    result_json jsonb,
    lease_owner text,
    lease_until timestamptz,
    fencing_version bigint NOT NULL DEFAULT 0 CHECK (fencing_version >= 0),
    requested_event_id uuid,
    last_error_code text,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, run_id),
    UNIQUE (tenant_id, requested_event_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, trip_id) REFERENCES tripmate.trip (tenant_id, trip_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX agent_run_active_thread_uq ON tripmate.agent_run (tenant_id, thread_id)
    WHERE status IN ('RUNNING', 'WAITING_CONFIRMATION');
CREATE INDEX agent_run_recovery_idx ON tripmate.agent_run (status, lease_until, created_at)
    WHERE status IN ('RUNNING', 'WAITING_CONFIRMATION');

CREATE TABLE tripmate.agent_action (
    action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    run_id uuid NOT NULL,
    action_type text NOT NULL,
    parameters jsonb NOT NULL,
    parameters_hash char(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('PROPOSED', 'CONFIRMED', 'REJECTED', 'EXECUTED', 'FAILED')),
    action_fingerprint char(64) NOT NULL,
    result_json jsonb,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, run_id, action_id),
    UNIQUE (tenant_id, run_id, action_fingerprint),
    UNIQUE (tenant_id, action_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES tripmate.agent_run (tenant_id, run_id) ON DELETE RESTRICT
);

CREATE TABLE tripmate.action_confirmation (
    confirmation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
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
    UNIQUE (tenant_id, action_id),
    FOREIGN KEY (tenant_id, action_id) REFERENCES tripmate.agent_action (tenant_id, action_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    CHECK (expires_at > issued_at),
    CHECK ((status = 'ISSUED' AND consumed_at IS NULL) OR (status <> 'ISSUED' AND consumed_at IS NOT NULL))
);

ALTER TABLE tripmate.agent_run ADD CONSTRAINT agent_run_awaiting_action_fk
    FOREIGN KEY (tenant_id, run_id, awaiting_action_id)
    REFERENCES tripmate.agent_action (tenant_id, run_id, action_id) ON DELETE RESTRICT;
ALTER TABLE tripmate.reservation ADD CONSTRAINT reservation_action_scope_fk
    FOREIGN KEY (tenant_id, approved_action_id)
    REFERENCES tripmate.agent_action (tenant_id, action_id) ON DELETE RESTRICT;
ALTER TABLE tripmate.reservation_confirmation ADD CONSTRAINT reservation_confirmation_action_fk
    FOREIGN KEY (tenant_id, action_id)
    REFERENCES tripmate.agent_action (tenant_id, action_id) ON DELETE RESTRICT;

CREATE TABLE tripmate.user_preference (
    preference_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    user_id uuid NOT NULL,
    scope text NOT NULL,
    preference_key text NOT NULL,
    value_json jsonb NOT NULL,
    source text NOT NULL CHECK (source IN ('USER_EXPLICIT', 'CONFIRMED_TRIP', 'OPERATOR')),
    effective_at timestamptz NOT NULL,
    deleted_at timestamptz,
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, preference_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX user_preference_active_uq ON tripmate.user_preference (tenant_id, user_id, scope, preference_key)
    WHERE deleted_at IS NULL;
CREATE INDEX user_preference_scope_idx ON tripmate.user_preference (tenant_id, user_id, updated_at DESC);

CREATE TABLE tripmate.conversation_message (
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    conversation_id uuid NOT NULL,
    message_id uuid NOT NULL,
    user_id uuid NOT NULL,
    sequence_no bigint NOT NULL,
    content_redacted text NOT NULL CHECK (length(content_redacted) <= 4000),
    content_hash char(64) NOT NULL,
    run_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, conversation_id, message_id),
    UNIQUE (tenant_id, conversation_id, sequence_no),
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, run_id) REFERENCES tripmate.agent_run (tenant_id, run_id) ON DELETE RESTRICT
);

CREATE INDEX action_confirmation_expiry_idx ON tripmate.action_confirmation (tenant_id, status, expires_at)
    WHERE status = 'ISSUED';
CREATE TRIGGER agent_run_updated_at BEFORE UPDATE ON tripmate.agent_run
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
CREATE TRIGGER agent_action_updated_at BEFORE UPDATE ON tripmate.agent_action
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
CREATE TRIGGER user_preference_updated_at BEFORE UPDATE ON tripmate.user_preference
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
