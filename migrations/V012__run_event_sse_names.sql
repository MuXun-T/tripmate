ALTER TABLE tripmate.agent_run_event
    DROP CONSTRAINT IF EXISTS agent_run_event_event_type_check;

ALTER TABLE tripmate.agent_run_event
    ADD CONSTRAINT agent_run_event_event_type_check
    CHECK (event_type IN (
        'run_started', 'node_started', 'tool_start', 'tool_end', 'citation',
        'approval_required', 'confirmation', 'reservation_updated', 'done', 'completed', 'error'
    ));
