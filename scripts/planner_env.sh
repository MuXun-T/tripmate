#!/usr/bin/env bash
# 用法：source scripts/planner_env.sh   （不要用 bash 执行）
export TRIPMATE_ROOT="/media/zzq/新加卷/tripmate"
cd "$TRIPMATE_ROOT" || return 1
export PATH="$TRIPMATE_ROOT/.tools/bin:$PATH"
export UV_CACHE_DIR="$TRIPMATE_ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$TRIPMATE_ROOT/.tools/uv-python"
export PIP_CACHE_DIR="$TRIPMATE_ROOT/.cache/pip"
export HF_HOME="$TRIPMATE_ROOT/.cache/hf"
export HF_DATASETS_CACHE="$HF_HOME/datasets"
export SENTENCE_TRANSFORMERS_HOME="$HF_HOME/sentence-transformers"
export TORCH_HOME="$TRIPMATE_ROOT/.cache/torch"
export XDG_CACHE_HOME="$TRIPMATE_ROOT/.cache/xdg"
export TMPDIR="$TRIPMATE_ROOT/.deps-tmp"
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=8
mkdir -p "$UV_CACHE_DIR" "$HF_HOME" "$TORCH_HOME" "$XDG_CACHE_HOME" "$TMPDIR" "$TRIPMATE_ROOT/.cache/llm" "$TRIPMATE_ROOT/.cache/jieba"
if [ -f "$TRIPMATE_ROOT/.env" ]; then
  LLM_API_KEY="$(grep -E "^LLM_API_KEY=" "$TRIPMATE_ROOT/.env" | tail -n1 | cut -d= -f2- | sed -e "s/^[\"\x27]//" -e "s/[\"\x27]\$//")"
  export LLM_API_KEY
  export DEEPSEEK_API_KEY="$LLM_API_KEY"
fi
if [ -f "$TRIPMATE_ROOT/.venv-planner/bin/activate" ]; then
  source "$TRIPMATE_ROOT/.venv-planner/bin/activate"
fi
