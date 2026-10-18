"""Run compiled DSL constraints on one plan with the official executor (no oracle involved)."""

import contextlib
import io
import os
from typing import Any

from tripmate_planner.paths import CT_ROOT


def run_spec_checks(plan: dict[str, Any], dsl: list[str]) -> list[int]:
    """Return indexes of failed constraints."""
    from chinatravel.symbol_verification.hard_constraint import evaluate_constraints_py

    old = os.getcwd()
    os.chdir(CT_ROOT)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            results = evaluate_constraints_py(dsl, plan, verbose=False)
    finally:
        os.chdir(old)
    return [i for i, ok in enumerate(results) if not ok]
