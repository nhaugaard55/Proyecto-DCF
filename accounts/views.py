from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .forms import EmailLoginForm, RegisterForm


@login_required
def account_home(request):
    return render(request, 'accounts/account_home.html')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('landing')

    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
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
