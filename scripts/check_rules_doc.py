"""T0.6 acceptance: every commonsense error column in the official verifier appears in official-rules.md."""

import re
import sys
from pathlib import Path

ROOT = Path("/media/zzq/新加卷/tripmate")
src = (ROOT / "third_party/ChinaTravel/chinatravel/symbol_verification/commonsense_constraint.py").read_text(encoding="utf-8")
doc = (ROOT / "docs/planner/official-rules.md").read_text(encoding="utf-8")
columns = set(re.findall(r"table_statistics\.loc\[0,\s*['\"]([^'\"]+)['\"]\]", src))
for block in re.findall(r"pd\.DataFrame\(columns=\[([^\]]*)\]", src):
    columns.update(re.findall(r"['\"]([^'\"]+)['\"]", block))
columns.discard("data_id")  # aggregate id column, not an error column
missing = sorted(c for c in columns if c not in doc)
print(f"{len(columns)} columns, missing: {missing}")
sys.exit(1 if missing else 0)
