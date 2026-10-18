"""Official commonsense checks (8 Is_* functions) on one plan; symbolic input built from TripSpec only."""

import contextlib
import io
from typing import Any

from tripmate_planner.spec.tripspec import TripSpec
from tripmate_planner.verify.result import VerifyResult
from tripmate_planner.verify.spec_checks import run_spec_checks

_FUNCS: list[Any] = []


def _funcs() -> list[Any]:
    if not _FUNCS:  # official verifiers do not depend on the working directory
        from chinatravel.symbol_verification import commonsense_constraint as cc

        _FUNCS.extend([cc.Is_activity_grounded, cc.Is_intercity_transport_correct, cc.Is_attractions_correct,
                       cc.Is_hotels_correct, cc.Is_restaurants_correct, cc.Is_transport_correct,
                       cc.Is_time_correct, cc.Is_space_correct])
    return _FUNCS


def run_commonsense(plan: dict[str, Any], spec: TripSpec) -> tuple[list[str], list[str]]:
    symbolic = {"start_city": spec.start_city, "target_city": spec.target_city,
                "people_number": spec.people_number or 1, "days": spec.days}
    failed: list[str] = []
    messages: list[str] = []
    for func in _funcs():
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                table, info = func(symbolic, plan, verbose=False)
            for col in table.columns:
                if int(table[col].loc[0]):
                    failed.append(str(col))
            if any(int(table[c].loc[0]) for c in table.columns):
                messages.extend(str(x)[:300] for x in info)
        except Exception as exc:
            failed.append("Commonsense Evaluator Exception")
            messages.append(f"{func.__name__}: {type(exc).__name__}: {str(exc)[:200]}")
    return failed, messages


def verify_plan(plan: dict[str, Any] | None, spec: TripSpec, dsl: list[str], *, commonsense: bool = True,
                spec_checks: bool = True) -> VerifyResult:
    if not plan:
        return VerifyResult(passed=False, commonsense_failed=["no plan"])
    failed, messages = run_commonsense(plan, spec) if commonsense else ([], [])
    spec_failed = run_spec_checks(plan, dsl) if spec_checks else []
    return VerifyResult(passed=not failed and not spec_failed, commonsense_failed=failed,
                        commonsense_messages=messages, spec_failed=spec_failed)
