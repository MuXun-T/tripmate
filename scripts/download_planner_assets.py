"""Download pinned ChinaTravel sandbox, query splits and local models (idempotent).

Run inside .venv-planner after `source scripts/planner_env.sh`.
"""

import json
import platform
import shutil
import zipfile
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

ROOT = Path("/media/zzq/新加卷/tripmate")
CT = ROOT / "third_party" / "ChinaTravel"
VERSIONS = ROOT / "configs" / "planner" / "versions.json"

PINS = {
    "chinatravel_git": "f445e0011f42594fcc29b8c752ece06ded9a8218",
    "query_dataset": {"repo": "LAMDA-NeSy/ChinaTravel", "revision": "a7d893afb628af6ca181625b6c7cca2d60a6bccb"},
    "sandbox_dataset": {"repo": "LAMDA-NeSy/ChinaTravel-Sandbox", "revision": "885c7dad143bf040238ddca263f4788d582ee0f4"},
    "dense_model": {"repo": "BAAI/bge-small-zh-v1.5", "revision": "7999e1d3359715c523056ef9478215996d62a620"},
    "rerank_model": {"repo": "BAAI/bge-reranker-base", "revision": "2cfc18c9415c912f9d8155881c133215df768a70"},
}


def install_sandbox() -> None:
    target = CT / "chinatravel" / "environment" / "database"
    if target.is_dir() and any(target.iterdir()):
        return
    sandbox = PINS["sandbox_dataset"]
    archive = hf_hub_download(sandbox["repo"], "raw/ChinaTravel_sandbox_zh.zip", repo_type="dataset",
                              revision=sandbox["revision"])
    staging = ROOT / ".deps-tmp" / "sandbox_zh"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(staging)
    candidates = [p for p in staging.rglob("*") if p.is_dir() and (p / "attractions").is_dir()]
    if not candidates:
        raise SystemExit(f"sandbox archive layout not recognised under {staging}")
    shutil.copytree(candidates[0], target)
    shutil.rmtree(staging, ignore_errors=True)


def download_queries() -> None:
    from datasets import load_dataset

    q = PINS["query_dataset"]
    ds = load_dataset(q["repo"], name="default", revision=q["revision"])
    for split, expected in (("easy", 300), ("medium", 150), ("human", 154)):
        if len(ds[split]) != expected:
            raise SystemExit(f"{split} has {len(ds[split])} rows, expected {expected}")


# 本机网络下 hub 下载器对大文件会卡住；reranker 由 scripts/fetch_model_curl.sh 断点续传下载，按 HF 公布大小校验
CURL_MODELS = {
    "rerank_model": (ROOT / ".cache" / "models" / "bge-reranker-base",
                     {"config.json": 799, "model.safetensors": 1112206140, "sentencepiece.bpe.model": 5069051,
                      "special_tokens_map.json": 279, "tokenizer.json": 17098107, "tokenizer_config.json": 443}),
}


def _curl_model_ready(key: str) -> str | None:
    if key not in CURL_MODELS:
        return None
    folder, sizes = CURL_MODELS[key]
    if all((folder / name).is_file() and (folder / name).stat().st_size == size for name, size in sizes.items()):
        return str(folder)
    return None


def download_models() -> dict[str, str]:
    paths = {}
    for key in ("dense_model", "rerank_model"):
        m = PINS[key]
        ready = _curl_model_ready(key)
        if ready is not None:
            paths[key] = ready
            continue
        paths[key] = snapshot_download(m["repo"], revision=m["revision"],
                                       allow_patterns=["*.json", "*.txt", "*.safetensors", "*.model",
                                                       "sentencepiece*", "1_Pooling/*", "modules.json"])
    return paths


def main() -> None:
    install_sandbox()
    download_queries()
    paths = download_models()
    import torch  # noqa: PLC0415

    versions = {**PINS, "model_paths": paths, "python": platform.python_version(),
                "torch": torch.__version__}
    VERSIONS.parent.mkdir(parents=True, exist_ok=True)
    VERSIONS.write_text(json.dumps(versions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("assets ready")


if __name__ == "__main__":
    main()
