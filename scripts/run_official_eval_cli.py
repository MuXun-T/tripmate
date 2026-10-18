"""Run the official eval_exp.py unchanged, except two loader fixes needed at commit f445e00 + HF a7d893a:
(1) HF CSV string fields are parsed into lists; (2) oracle fields are not stripped (evaluation only).

The pinned HF revision stores hard_logic_py as a string; the official loader requires a list and
raises. We monkeypatch its HF loader (no third-party file is modified, rule R3).
Usage: python scripts/run_official_eval_cli.py --splits easy --method RuleNeSy_rule --lang zh
"""

import ast
import os
import runpy
import sys

CT = "/media/zzq/新加卷/tripmate/third_party/ChinaTravel"
os.chdir(CT)
sys.path.insert(0, CT)
import chinatravel.data.load_datasets as ld  # noqa: E402

_orig = ld._load_huggingface_split


def _patched(split):
    rows = _orig(split)
    for row in rows:
        for key in ("hard_logic_py", "hard_logic", "hard_logic_nl"):
            if isinstance(row.get(key), str):
                row[key] = ast.literal_eval(row[key])
    return rows


ld._load_huggingface_split = _patched
# eval_exp's args lack oracle_translation, so load_query strips the oracle fields it then needs (KeyError).
# Evaluation must see them; keep records intact for this evaluation-only entry point.
ld._strip_oracle_fields = lambda data_i: data_i
sys.argv = ["eval_exp.py", *sys.argv[1:]]
runpy.run_path(os.path.join(CT, "eval_exp.py"), run_name="__main__")
