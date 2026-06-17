"""Subscription and daily analysis limit helpers for Intrinsic.

This module owns plan and usage decisions so views can stay focused on request
flow. Stripe/billing can later update UserSubscription.plan without changing
the DCF analysis code.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import F
from django.utils import timezone

from .models import DailyUsage, UserSubscription


PLAN_GUEST = "GUEST"
PLAN_ADMIN = UserSubscription.PLAN_ADMIN
PLAN_FREE = UserSubscription.PLAN_FREE
PLAN_PRO = UserSubscription.PLAN_PRO

DAILY_ANALYSIS_LIMITS = {
    PLAN_GUEST: 3,
    PLAN_ADMIN: 15,
    PLAN_FREE: 10,
    PLAN_PRO: None,
}


@dataclass(frozen=True)
class UsageSummary:
    plan: str
    limit: int | None
    used: int
    remaining: int | None
    is_unlimited: bool
    exceeded: bool


def get_user_plan(request) -> str:
    """Return GUEST, ADMIN, FREE, or PRO for the current request."""
    if not request.user.is_authenticated:
        return PLAN_GUEST
    if request.user.is_staff or request.user.is_superuser:
        return PLAN_ADMIN
    subscription, _ = UserSubscription.objects.get_or_create(user=request.user)
    return subscription.plan


def get_daily_limit(request) -> int | None:
    """Return today's analysis limit, or None when the plan is unlimited."""
    return DAILY_ANALYSIS_LIMITS[get_user_plan(request)]


def get_usage_record(request) -> DailyUsage:
    """Return today's DailyUsage row for user or guest session."""
    today = timezone.localdate()
    if request.user.is_authenticated:
        usage, _ = DailyUsage.objects.get_or_create(user=request.user, date=today)
        return usage

    if not request.session.session_key:
        request.session.create()
    usage, _ = DailyUsage.objects.get_or_create(
        user=None,
        session_key=request.session.session_key,
        date=today,
    )
    return usage


def get_remaining_analyses(request) -> int | None:
    """Return remaining analyses for today, or None for unlimited plans."""
    limit = get_daily_limit(request)
    if limit is None:
        return None
    return max(limit - get_usage_record(request).analysis_count, 0)


def can_run_analysis(request) -> bool:
    """Return whether the request can execute one more valid analysis today."""
    if get_user_plan(request) == PLAN_ADMIN:
        return True
    limit = get_daily_limit(request)
    if limit is None:
        return True
    return get_usage_record(request).analysis_count < limit


def record_analysis_run(request) -> DailyUsage:
    """Increment usage after a valid analysis completed successfully."""
    usage = get_usage_record(request)
    if get_user_plan(request) == PLAN_ADMIN:
        limit = DAILY_ANALYSIS_LIMITS[PLAN_ADMIN]
        next_count = 1 if usage.analysis_count >= limit else usage.analysis_count + 1
        usage.analysis_count = next_count
        usage.save(update_fields=("analysis_count",))
        return usage
    DailyUsage.objects.filter(pk=usage.pk).update(analysis_count=F("analysis_count") + 1)
    usage.refresh_from_db(fields=("analysis_count",))
    return usage


def get_usage_summary(request) -> UsageSummary:
    """Return plan, used count, limit and remaining count for UI rendering."""
    plan = get_user_plan(request)
    limit = DAILY_ANALYSIS_LIMITS[plan]
    used = get_usage_record(request).analysis_count
    remaining = None if limit is None else max(limit - used, 0)
    return UsageSummary(
        plan=plan,
        limit=limit,
        used=used,
        remaining=remaining,
        is_unlimited=limit is None,
        exceeded=False if limit is None else used >= limit,
    )
