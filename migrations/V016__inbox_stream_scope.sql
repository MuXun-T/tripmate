ALTER TABLE tripmate.inbox_event
    ADD COLUMN stream_name text,
    ADD COLUMN group_name text,
    ADD COLUMN stream_message_id text;

CREATE INDEX inbox_stream_progress_idx
    ON tripmate.inbox_event (tenant_id, stream_name, group_name, stream_message_id)
    WHERE stream_name IS NOT NULL AND group_name IS NOT NULL AND stream_message_id IS NOT NULL;
