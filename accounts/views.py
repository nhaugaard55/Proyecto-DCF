from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from dcf_app.models import AnalysisRecord, WatchlistGroup, WatchlistItem

from .forms import EmailLoginForm, RegisterForm
from .models import UserSubscription
from .subscription import get_usage_summary


@login_required
def account_home(request):
    user = request.user
    latest_analysis = AnalysisRecord.objects.filter(user=user).order_by('-created_at').first()
    watchlist_groups = WatchlistGroup.objects.filter(user=user)
    stats = {
        'analysis_count': AnalysisRecord.objects.filter(user=user).count(),
        'watchlist_group_count': watchlist_groups.count(),
        'watchlist_item_count': WatchlistItem.objects.filter(watchlist__user=user).count(),
        'latest_analysis': latest_analysis,
        'usage_summary': get_usage_summary(request),
    }
    return render(request, 'accounts/account_home.html', {'stats': stats})


def register_view(request):
    if request.user.is_authenticated:
        return redirect('landing')

    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        UserSubscription.objects.get_or_create(user=user, defaults={'plan': UserSubscription.PLAN_FREE})
        login(request, user)
        return redirect('landing')

    return render(request, 'accounts/register.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('landing')

    form = EmailLoginForm(request, data=request.POST or None)
    if request.method == 'POST' and form.is_valid():
        login(request, form.get_user())
        return redirect('landing')

    return render(request, 'accounts/login.html', {'form': form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect('landing')
