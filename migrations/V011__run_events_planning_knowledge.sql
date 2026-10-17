ALTER TABLE tripmate.attraction ADD COLUMN indoor boolean;
ALTER TABLE tripmate.knowledge_chunk ADD COLUMN embedding double precision[];

CREATE TABLE tripmate.agent_run_event (
    event_sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    user_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'run_started', 'tool_start', 'tool_end', 'citation', 'approval_required',
        'confirmation', 'reservation_updated', 'completed', 'error'
    )),
    event_key text,
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE (tenant_id, event_id),
    UNIQUE (tenant_id, run_id, event_key),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES tripmate.agent_run (tenant_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT
);
CREATE INDEX agent_run_event_cursor_idx
    ON tripmate.agent_run_event (tenant_id, user_id, run_id, event_sequence);

CREATE TABLE tripmate.knowledge_evaluation_run (
    evaluation_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    index_version text NOT NULL,
    embedding_model text NOT NULL,
    case_count integer NOT NULL CHECK (case_count > 0),
    metrics jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX knowledge_evaluation_history_idx
    ON tripmate.knowledge_evaluation_run (tenant_id, created_at DESC);
