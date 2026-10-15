CREATE TABLE tripmate.knowledge_document (
    document_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    external_document_id text NOT NULL,
    document_type text NOT NULL,
    source text NOT NULL,
    source_id text NOT NULL,
    source_url text NOT NULL,
    source_version text NOT NULL,
    title text NOT NULL,
    city text,
    content_sha256 char(64) NOT NULL,
    retrieved_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('ACTIVE', 'SUPERSEDED', 'DELETED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, document_id),
    UNIQUE (tenant_id, external_document_id, source_version)
);

CREATE TABLE tripmate.knowledge_manifest (
    manifest_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    index_version text NOT NULL,
    index_alias text NOT NULL,
    embedding_model text NOT NULL,
    vector_dimensions integer NOT NULL CHECK (vector_dimensions > 0),
    status text NOT NULL CHECK (status IN ('BUILDING', 'VALIDATED', 'ACTIVE', 'FAILED', 'RETIRED')),
    document_count integer NOT NULL DEFAULT 0 CHECK (document_count >= 0),
    chunk_count integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    validated_at timestamptz,
    UNIQUE (tenant_id, manifest_id),
    UNIQUE (tenant_id, index_version)
);

CREATE TABLE tripmate.knowledge_chunk (
    chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    document_id uuid NOT NULL,
    manifest_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    heading text,
    content text NOT NULL,
    content_sha256 char(64) NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, chunk_id),
    UNIQUE (tenant_id, manifest_id, document_id, ordinal),
    FOREIGN KEY (tenant_id, document_id) REFERENCES tripmate.knowledge_document (tenant_id, document_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, manifest_id) REFERENCES tripmate.knowledge_manifest (tenant_id, manifest_id) ON DELETE RESTRICT
);

CREATE TABLE tripmate.knowledge_active_alias (
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    index_alias text NOT NULL,
    manifest_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, index_alias),
    FOREIGN KEY (tenant_id, manifest_id) REFERENCES tripmate.knowledge_manifest (tenant_id, manifest_id) ON DELETE RESTRICT
);

CREATE TABLE tripmate.private_case_vector (
    vector_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tripmate.tenant(tenant_id) ON DELETE RESTRICT,
    user_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    tombstoned_at timestamptz,
    content_redacted text NOT NULL,
    content_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, user_id) REFERENCES tripmate.user_profile (tenant_id, user_id) ON DELETE RESTRICT
);
CREATE INDEX private_case_vector_scope_idx ON tripmate.private_case_vector
    (tenant_id, user_id, generation) WHERE tombstoned_at IS NULL;
