from django.conf import settings as django_settings

from .subscription import get_usage_summary


def subscription_usage(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"nav_usage_summary": get_usage_summary(request)}


def ga_measurement_id(request):
    return {"GA_MEASUREMENT_ID": getattr(django_settings, "GA_MEASUREMENT_ID", "")}
