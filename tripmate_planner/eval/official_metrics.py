"""Official ChinaTravel metrics for any uid subset (same functions as eval_exp.py)."""

import contextlib
import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

from tripmate_planner.eval.oracle import load_oracle_records
from tripmate_planner.paths import CT_ROOT


@contextlib.contextmanager
def _in_ct_root() -> Iterator[None]:
    old = os.getcwd()
    os.chdir(CT_ROOT)
    try:
        yield
    finally:
        os.chdir(old)


@dataclass
class OfficialMetrics:
    n: int
    DR: float
    EPR_micro: float
    EPR_macro: float
    LPR_micro: float
    LPR_macro: float
    C_LPR: float
    FPR: float
    per_uid: dict[str, dict[str, bool]] = field(default_factory=dict)
    commonsense_fail_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_official_metrics(uids: list[str], plans: dict[str, dict[str, Any]]) -> OfficialMetrics:
    from chinatravel.evaluation.commonsense_constraint import evaluate_commonsense_constraints
    from chinatravel.evaluation.hard_constraint import evaluate_hard_constraints_v2
    from chinatravel.evaluation.schema_constraint import evaluate_schema_constraints

    records = load_oracle_records(uids)
    plan_dict = {u: plans.get(u) or {} for u in uids}
    schema = json.loads((CT_ROOT / "chinatravel" / "evaluation" / "output_schema.json").read_text(encoding="utf-8"))
    with _in_ct_root():
        schema_rate, _schema_agg, schema_pass = evaluate_schema_constraints(uids, plan_dict, schema=schema)
        macro_comm, micro_comm, comm_agg, comm_pass = evaluate_commonsense_constraints(
            uids, records, plan_dict, verbose=False, lang="zh")
        (macro_logi, micro_logi, _c_macro, c_micro, _logi_agg,
         logi_pass) = evaluate_hard_constraints_v2(uids, records, plan_dict, env_pass_id=comm_pass,
                                                   verbose=False, lang="zh")
    schema_set, comm_set, logi_set = set(schema_pass), set(comm_pass), set(logi_pass)
    final = schema_set & comm_set & logi_set
    per_uid = {u: {"schema": u in schema_set, "commonsense": u in comm_set, "logic": u in logi_set,
                   "final": u in final} for u in uids}
    fail_counts = {str(c): int(comm_agg[c].sum()) for c in comm_agg.columns if c != "data_id"}
    return OfficialMetrics(n=len(uids), DR=float(schema_rate), EPR_micro=float(micro_comm),
                           EPR_macro=float(macro_comm), LPR_micro=float(micro_logi),
                           LPR_macro=float(macro_logi), C_LPR=float(c_micro),
                           FPR=100.0 * len(final) / len(uids), per_uid=per_uid,
                           commonsense_fail_counts=fail_counts)
