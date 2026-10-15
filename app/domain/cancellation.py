from datetime import UTC, datetime
from decimal import Decimal

from app.domain.models import CancellationDecision, CancellationPolicySnapshot


class CancellationPolicyEvaluator:
    """Evaluate the policy snapshot fixed when the reservation was held."""

    def evaluate(self, policy: CancellationPolicySnapshot, now: datetime) -> CancellationDecision:
        if now.tzinfo is None:
            raise ValueError("now must be timezone aware")
        instant = now.astimezone(UTC)
        eligible = sorted(
            (rule for rule in policy.rules if instant < rule.cutoff_at.astimezone(UTC)),
            key=lambda rule: rule.cutoff_at.astimezone(UTC),
        )
        if not eligible:
            return CancellationDecision(
                allowed=False,
                refund_percent=Decimal("0"),
                reason_code="CANCELLATION_CUTOFF_PASSED",
                policy_version=policy.policy_version,
            )
        rule = eligible[0]
        return CancellationDecision(
            allowed=True,
            refund_percent=rule.refund_percent,
            reason_code=rule.reason_code,
            policy_version=policy.policy_version,
        )
