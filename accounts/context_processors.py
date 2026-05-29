from .subscription import get_usage_summary


def subscription_usage(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"nav_usage_summary": get_usage_summary(request)}
