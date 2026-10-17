"""Offline external-dataset RAG evaluation entry point."""

import argparse
import json
from pathlib import Path
from typing import Any

from app.knowledge.index import evaluate_bundle, load_evaluation_dataset


def evaluate_dataset(path: Path, *, top_k: int = 5) -> dict[str, Any]:
    dataset = load_evaluation_dataset(path)
    metrics = evaluate_bundle(dataset.bundle(), dataset.cases, top_k=top_k)
    return {
        "dataset_id": dataset.dataset_id,
        "dataset_revision": dataset.dataset_revision,
        "license": dataset.license,
        "split": dataset.split,
        "source_version": dataset.source_version,
        "metrics": metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the local RAG ranker on an external dataset")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("knowledge/evaluation/travelplanner-validation.json"),
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate_dataset(args.dataset, top_k=args.top_k)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
