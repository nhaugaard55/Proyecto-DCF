from django.conf import settings
from django.db import models
from django.db.models import Q


class UserSubscription(models.Model):
    """Plan actual de un usuario.

    Billing externo se agregará más adelante sobre este modelo sin cambiar la
    relación principal usuario -> plan.
    """

    PLAN_FREE = "FREE"
    PLAN_ADMIN = "ADMIN"
    PLAN_PRO = "PRO"
    PLAN_CHOICES = [
        (PLAN_FREE, "Free"),
        (PLAN_ADMIN, "Admin"),
        (PLAN_PRO, "Pro"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.CharField(max_length=16, choices=PLAN_CHOICES, default=PLAN_FREE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user_id",)

    def __str__(self) -> str:
        return f"{self.user} - {self.plan}"


class DailyUsage(models.Model):
    """Contador diario de análisis por usuario autenticado o sesión guest."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="daily_usage",
    )
    session_key = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    date = models.DateField()
    analysis_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-date",)
        constraints = [
            models.UniqueConstraint(
                fields=("user", "date"),
                condition=Q(user__isnull=False),
                name="unique_daily_usage_per_user",
            ),
            models.UniqueConstraint(
                fields=("session_key", "date"),
                condition=Q(user__isnull=True, session_key__isnull=False),
                name="unique_daily_usage_per_session",
            ),
        ]

    def __str__(self) -> str:
        owner = self.user_id or self.session_key or "anonymous"
        return f"{owner} - {self.date}: {self.analysis_count}"
