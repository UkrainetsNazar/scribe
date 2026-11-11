import math
from datetime import datetime

from scribe.domain.errors import QuotaExceededError
from scribe.domain.users import Plan


def usage_period(now: datetime) -> str:
    return now.strftime("%Y-%m")


def billable_minutes(duration_sec: float) -> int:
    return max(1, math.ceil(duration_sec / 60))


def ensure_within_quota(plan: Plan, used_minutes: int, duration_sec: float) -> int:
    """Returns the minutes to charge, or raises QuotaExceededError."""
    minutes = billable_minutes(duration_sec)
    if used_minutes + minutes > plan.monthly_minutes:
        raise QuotaExceededError(
            f"Monthly limit of {plan.monthly_minutes} minutes would be exceeded"
        )
    return minutes
