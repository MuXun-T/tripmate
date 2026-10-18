import pytest

from tripmate_planner.eval.stats import bootstrap_ci, mcnemar_exact


def test_mcnemar_known_value() -> None:
    a = [False] * 6 + [True] * 4
    b = [True] * 6 + [True] * 4
    b01, b10, p = mcnemar_exact(a, b)
    assert (b01, b10) == (6, 0)
    assert p == pytest.approx(0.03125)
    assert mcnemar_exact([True], [True])[2] == 1.0


def test_bootstrap_constant() -> None:
    assert bootstrap_ci([True] * 20) == (1.0, 1.0, 1.0)
    mean, lo, hi = bootstrap_ci([1, 0] * 50)
    assert mean == 0.5 and lo < 0.5 < hi
