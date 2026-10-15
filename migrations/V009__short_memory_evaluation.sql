CREATE TABLE tripmate.evaluation_case (
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    case_id uuid NOT NULL,
    case_version integer NOT NULL CHECK (case_version > 0),
    user_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    trip_id uuid,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    input_json jsonb NOT NULL CHECK (jsonb_typeof(input_json) = 'object'),
    expected_json jsonb NOT NULL CHECK (jsonb_typeof(expected_json) = 'object'),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, case_id, case_version),
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, trip_id) REFERENCES tripmate.trip (tenant_id, trip_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX evaluation_case_one_active_uq ON tripmate.evaluation_case (tenant_id, case_id)
    WHERE active;

CREATE TABLE tripmate.evaluation_result (
    evaluation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    case_id uuid NOT NULL,
    case_version integer NOT NULL,
    run_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('PASSED', 'FAILED', 'WAITING_CONFIRMATION', 'TIMED_OUT', 'RUN_FAILED')),
    actual_json jsonb NOT NULL CHECK (jsonb_typeof(actual_json) = 'object'),
    assertions_json jsonb NOT NULL CHECK (jsonb_typeof(assertions_json) = 'object'),
    result_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, case_id, case_version, run_id),
    FOREIGN KEY (tenant_id, case_id, case_version)
        REFERENCES tripmate.evaluation_case (tenant_id, case_id, case_version) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, run_id) REFERENCES tripmate.agent_run (tenant_id, run_id) ON DELETE RESTRICT
);
CREATE INDEX evaluation_result_case_idx ON tripmate.evaluation_result
    (tenant_id, case_id, case_version, created_at DESC);
