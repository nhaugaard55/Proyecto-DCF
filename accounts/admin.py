from django.contrib import admin

from .models import DailyUsage, UserSubscription


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "created_at", "updated_at")
    list_filter = ("plan",)
    search_fields = ("user__username", "user__email", "user__first_name", "user__last_name")


@admin.register(DailyUsage)
class DailyUsageAdmin(admin.ModelAdmin):
    list_display = ("user", "session_key", "date", "analysis_count")
    list_filter = ("date",)
    search_fields = ("user__username", "user__email", "session_key")
