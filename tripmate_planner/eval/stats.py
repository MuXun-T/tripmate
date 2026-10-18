"""Bootstrap confidence intervals and exact McNemar test."""

import random
from collections.abc import Sequence

SEED = 20261018


def bootstrap_ci(values: Sequence[float | bool], n: int = 1000, seed: int = SEED,
                 alpha: float = 0.05) -> tuple[float, float, float]:
    data = [float(v) for v in values]
    if not data:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    k = len(data)
    means = sorted(sum(data[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    lo = means[int((alpha / 2) * n)]
    hi = means[min(n - 1, int((1 - alpha / 2) * n))]
    return (sum(data) / k, lo, hi)


def mcnemar_exact(a: Sequence[bool], b: Sequence[bool]) -> tuple[int, int, float]:
    """Return (b01, b10, two-sided exact p). b01 = a fails & b passes."""
    from scipy.stats import binomtest

    if len(a) != len(b):
        raise ValueError("paired sequences must have equal length")
    b01 = sum(1 for x, y in zip(a, b, strict=True) if not x and y)
    b10 = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
    if b01 + b10 == 0:
        return b01, b10, 1.0
    return b01, b10, float(binomtest(min(b01, b10), b01 + b10, 0.5).pvalue)
