#!/usr/bin/env bash
# 幂等安装：官方仓库（固定 commit）、.venv-planner、依赖、沙盒、查询数据、模型。
# 用法：source scripts/planner_env.sh && bash scripts/setup_planner.sh
set -euo pipefail
ROOT="/media/zzq/新加卷/tripmate"
cd "$ROOT"
CT_COMMIT="f445e0011f42594fcc29b8c752ece06ded9a8218"
PY=".venv-planner/bin/python"

if [ ! -d third_party/ChinaTravel/.git ]; then
  mkdir -p third_party
  git clone https://github.com/LAMDA-NeSy/ChinaTravel.git third_party/ChinaTravel
fi
git -C third_party/ChinaTravel checkout -q "$CT_COMMIT"
test "$(git -C third_party/ChinaTravel rev-parse HEAD)" = "$CT_COMMIT"

if [ ! -x "$PY" ]; then
  BASE_PY="$(.venv312/bin/python -c "import sys; print(sys._base_executable)")"
  uv venv .venv-planner --python "$BASE_PY" || uv venv .venv-planner --python 3.12 --python-preference only-managed
fi

uv pip install --python "$PY" -r third_party/ChinaTravel/requirements.txt
uv pip install --python "$PY" -r requirements-planner.in \
  --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match
SITE="$($PY -c "import sysconfig; print(sysconfig.get_paths()[\"purelib\"])")"
echo "$ROOT/third_party/ChinaTravel" > "$SITE/chinatravel_thirdparty.pth"
echo "$ROOT" > "$SITE/tripmate_root.pth"
uv pip freeze --python "$PY" > requirements-planner.lock

export HF_HUB_DISABLE_XET=1
R=.cache/models/bge-reranker-base
if [ "$(stat -c %s $R/model.safetensors 2>/dev/null || echo 0)" != "1112206140" ]; then
  bash scripts/fetch_model_curl.sh BAAI/bge-reranker-base 2cfc18c9415c912f9d8155881c133215df768a70 "$R" \
    config.json model.safetensors sentencepiece.bpe.model special_tokens_map.json tokenizer.json tokenizer_config.json
fi
$PY scripts/download_planner_assets.py
