from app.evaluation.replay import compare_case


def test_evaluation_checks_tool_sequence_constraints_citations_and_order() -> None:
    expected = {
        "intent": "planning",
        "expected_tools": ["get_trip_context"],
        "allowed_tools": ["get_trip_context", "get_weather"],
        "constraint_state": "PASS",
        "required_citation_ids": ["citation-1"],
        "reservation_status": None,
    }
    actual = {
        "intent": "planning",
        "tools": ["get_trip_context"],
        "constraint_state": "PASS",
        "citation_ids": ["citation-1", "citation-2"],
        "reservation_status": None,
    }
    assert all(compare_case(expected, actual).values())

    actual["tools"] = ["confirm_reservation"]
    assert not compare_case(expected, actual)["tool_allowlist"]
    assert not compare_case(expected, actual)["tool_sequence"]


def test_evaluation_unknown_is_only_accepted_when_case_allows_it() -> None:
    expected = {"constraint_state": "PASS"}
    assert not compare_case(expected, {"constraint_state": "UNKNOWN"})["constraint_state"]
    assert compare_case(
        {**expected, "allow_unknown": True}, {"constraint_state": "UNKNOWN"}
    )["constraint_state"]


def test_evaluation_case_cannot_pass_without_a_valid_expectation() -> None:
    assert compare_case({}, {}) == {"expectation_schema": False}
    assert compare_case({"allow_unknown": True}, {}) == {"expectation_schema": False}
    assert compare_case({"intent": None}, {"intent": None}) == {"expectation_schema": False}
    assert compare_case(
        {"intent": "planning", "unrecognized_expectation": True}, {"intent": "planning"}
    ) == {"expectation_schema": False}
    assert compare_case({"reservation_status": None}, {"reservation_status": None}) == {
        "expectation_schema": True,
        "reservation_status": True,
    }
