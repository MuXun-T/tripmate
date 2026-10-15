ALTER TABLE tripmate.evaluation_case
    ADD CONSTRAINT evaluation_case_expectation_nonempty_ck
    CHECK (expected_json ?| ARRAY[
        'intent',
        'expected_tools',
        'allowed_tools',
        'constraint_state',
        'required_citation_ids',
        'reservation_status'
    ]);
