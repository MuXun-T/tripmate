"""Rule R4: oracle field names may appear only in four evaluation modules."""

from pathlib import Path

ALLOWED = {
    "tripmate_planner/eval/oracle.py",
    "tripmate_planner/eval/labels.py",
    "tripmate_planner/eval/official_metrics.py",
    "tripmate_planner/eval/poi_ret_build.py",
}
ROOT = Path(__file__).resolve().parents[1]


def test_oracle_strings_only_in_allowed_files() -> None:
    offenders = []
    for path in (ROOT / "tripmate_planner").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "hard_logic" in path.read_text(encoding="utf-8") and rel not in ALLOWED:
            offenders.append(rel)
    assert offenders == []
