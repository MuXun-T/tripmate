"""Run compiled DSL constraints on one plan with the official executor (no oracle involved).

The official executor does not depend on the working directory, so no chdir (thread-safe).
"""

import contextlib
import io
from typing import Any


def run_spec_checks(plan: dict[str, Any], dsl: list[str]) -> list[int]:
    """Return indexes of failed constraints."""
    from chinatravel.symbol_verification.hard_constraint import evaluate_constraints_py

    with contextlib.redirect_stdout(io.StringIO()):
        results = evaluate_constraints_py(dsl, plan, verbose=False)
    return [i for i, ok in enumerate(results) if not ok]
