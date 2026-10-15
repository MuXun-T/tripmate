CREATE TABLE tripmate.idempotency_record (
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    user_id uuid NOT NULL,
    route text NOT NULL,
    operation text NOT NULL,
    key_hash char(64) NOT NULL,
    request_hash char(64) NOT NULL,
    http_status smallint NOT NULL CHECK (http_status BETWEEN 100 AND 599),
    result_json jsonb NOT NULL,
    resource_type text,
    resource_id uuid,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, route, operation, key_hash),
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT
);
CREATE INDEX idempotency_expiry_idx ON tripmate.idempotency_record (expires_at);

CREATE TABLE tripmate.outbox_event (
    event_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_version bigint NOT NULL CHECK (aggregate_version > 0),
    event_type text NOT NULL,
    event_version integer NOT NULL CHECK (event_version > 0),
    payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('PENDING', 'PUBLISHED', 'FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    replay_until timestamptz,
    published_stream text,
    published_stream_id text,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, event_id)
);
CREATE INDEX outbox_dispatch_idx ON tripmate.outbox_event (status, next_attempt_at, created_at);
CREATE INDEX outbox_replay_idx ON tripmate.outbox_event (tenant_id, created_at, event_id)
    WHERE status = 'PUBLISHED';
ALTER TABLE tripmate.agent_run ADD CONSTRAINT agent_run_requested_event_fk
    FOREIGN KEY (tenant_id, requested_event_id) REFERENCES tripmate.outbox_event (tenant_id, event_id)
    ON DELETE RESTRICT;

CREATE TABLE tripmate.outbox_replay_cursor (
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    stream_name text NOT NULL,
    cursor_created_at timestamptz NOT NULL,
    cursor_event_id uuid NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, stream_name)
);

CREATE TABLE tripmate.inbox_event (
    event_id uuid NOT NULL,
    consumer text NOT NULL,
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    status text NOT NULL CHECK (status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'DLQ')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner text,
    lease_until timestamptz,
    claim_token uuid,
    fencing_version bigint NOT NULL DEFAULT 0 CHECK (fencing_version >= 0),
    payload_hash char(64) NOT NULL,
    error_code text,
    processed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, consumer),
    CHECK ((status = 'PROCESSING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL AND claim_token IS NOT NULL)
        OR status <> 'PROCESSING')
);
CREATE INDEX inbox_expired_lease_idx ON tripmate.inbox_event (lease_until)
    WHERE status = 'PROCESSING';

CREATE TABLE tripmate.stream_group_watermark (
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    stream_name text NOT NULL,
    group_name text NOT NULL,
    safe_message_id text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, stream_name, group_name)
);

CREATE TABLE tripmate.dead_letter_event (
    dlq_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    event_id uuid NOT NULL,
    consumer text NOT NULL,
    stream_name text NOT NULL,
    group_name text NOT NULL,
    stream_message_id text NOT NULL,
    event_payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    error_code text NOT NULL,
    stack_hash char(64),
    attempt_count integer NOT NULL,
    status text NOT NULL CHECK (status IN ('OPEN', 'REPLAYED', 'RESOLVED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE (tenant_id, event_id, consumer)
);

CREATE TABLE tripmate.audit_log (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    actor_user_id uuid,
    run_id uuid,
    action_id uuid,
    reservation_id uuid,
    request_id text NOT NULL,
    trace_id text NOT NULL,
    action text NOT NULL,
    parameters_hash char(64),
    result_code text NOT NULL,
    details_redacted jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, actor_user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, run_id) REFERENCES tripmate.agent_run (tenant_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, action_id) REFERENCES tripmate.agent_action (tenant_id, action_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES tripmate.reservation (tenant_id, reservation_id) ON DELETE RESTRICT
);
CREATE INDEX audit_scope_created_idx ON tripmate.audit_log (tenant_id, created_at DESC);

CREATE TABLE tripmate.agent_tool_call (
    tool_call_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    run_id uuid,
    user_id uuid,
    tool_name text NOT NULL,
    arguments_hash char(64) NOT NULL,
    result_hash char(64),
    result_code text NOT NULL,
    duration_ms integer CHECK (duration_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, run_id) REFERENCES tripmate.agent_run (tenant_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT
);

CREATE TABLE tripmate.worker_heartbeat (
    worker_name text PRIMARY KEY,
    instance_id text NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TRIGGER idempotency_updated_at BEFORE UPDATE ON tripmate.idempotency_record
    FOR EACH ROW EXECUTE FUNCTION tripmate.touch_updated_at();
