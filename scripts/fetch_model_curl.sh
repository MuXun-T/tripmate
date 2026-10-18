#!/usr/bin/env bash
# 用 curl 断点续传下载固定 revision 的 HF 模型文件（hub 下载器在本机网络下会卡住时使用）
# 用法：bash scripts/fetch_model_curl.sh <repo> <revision> <dest_dir> <file...>
set -euo pipefail
repo="$1"; rev="$2"; dest="$3"; shift 3
mkdir -p "$dest"
for f in "$@"; do
  mkdir -p "$dest/$(dirname "$f")"
  for attempt in $(seq 1 30); do
    if curl -sSL --fail -C - --connect-timeout 20 --speed-time 30 --speed-limit 10000 \
        -o "$dest/$f" "https://huggingface.co/$repo/resolve/$rev/$f"; then break; fi
    echo "retry $attempt for $f" >&2; sleep 3
  done
done
