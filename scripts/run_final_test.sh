#!/usr/bin/env bash
# T5.2 / T5.3: one-shot test-split runs (rule R6). Each step writes its own lock in reports/final/ and refuses to rerun.
# Usage: bash scripts/run_final_test.sh baselines|ours
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source scripts/planner_env.sh
HASH=$(python -c "import json; print(json.load(open('reports/final/FREEZE.json'))['config_hash'])")
LOGS=runs/final_logs
mkdir -p "$LOGS"
case "${1:-}" in
  baselines)
    python -m tripmate_planner.eval.baselines.official_react0 --split test --final --run-id p5-react0-test \
      2>&1 | grep -E '^\[[0-9]+/[0-9]+\]|budget|Traceback|Error' > "$LOGS/react0.log" || true
    python -m tripmate_planner.eval.baselines.score --run-id p5-react0-test > "$LOGS/react0_score.log" 2>&1
    python -m tripmate_planner.eval.baselines.official_rulenesy --uids-file data/splits/test.txt --final \
      --run-id p5-rulenesy-test 2>&1 | grep -E '^\[[0-9]+/[0-9]+\]|method|Traceback' > "$LOGS/rulenesy.log" || true
    python -m tripmate_planner.eval.baselines.score --run-id p5-rulenesy-test > "$LOGS/rulenesy_score.log" 2>&1
    ;;
  ours)
    python -m tripmate_planner.eval.run_benchmark --config configs/planner/full.json --split test --final \
      --config-hash "$HASH" --scope p5_ours --run-id p5-full-test > "$LOGS/full.log" 2>&1
    python -m tripmate_planner.eval.mt_eval --config configs/planner/full.json --split test --final \
      --config-hash "$HASH" --scope p5_ours --run-id p5-mt-full-test > "$LOGS/mt.log" 2>&1
    python -m tripmate_planner.eval.extraction_eval --config configs/planner/full.json --split test --final \
      --config-hash "$HASH" --scope p5_ours --run-id p5-extract-test > "$LOGS/extract.log" 2>&1
    python -m tripmate_planner.eval.retrieval_eval --split test --final --specs runs/p5-extract-test/specs.jsonl \
      --run-id p5-retrieval-test > "$LOGS/retrieval.log" 2>&1
    ;;
  *) echo "usage: $0 baselines|ours" >&2; exit 2 ;;
esac
echo "done $1"
