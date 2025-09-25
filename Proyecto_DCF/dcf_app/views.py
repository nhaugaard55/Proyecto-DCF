from django.shortcuts import render


def home(request):
    return render(request, 'dcf_app/index.html')
