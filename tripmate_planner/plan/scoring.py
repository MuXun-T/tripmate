"""Feature-based candidate scoring (no learning; weights from config, grid-searched on dev only)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Weights:
    rel: float = 1.0
    transit_per_min: float = 0.02
    price_per_yuan: float = 0.002
    required_bonus: float = 2.0

    @classmethod
    def from_cfg(cls, w: dict[str, float]) -> "Weights":
        return cls(rel=float(w["rel"]), transit_per_min=float(w["transit_per_min"]),
                   price_per_yuan=float(w["price_per_yuan"]), required_bonus=float(w["required_bonus"]))


def candidate_score(w: Weights, *, rel_rank: int, transit_min: float, total_price: float, budget_set: bool,
                    new_required: int, forced: bool) -> float:
    score = w.rel / (1 + rel_rank) - w.transit_per_min * transit_min
    if budget_set:
        score -= w.price_per_yuan * total_price
    score += w.required_bonus * new_required
    if forced:
        score += 2 * w.required_bonus
    return score
