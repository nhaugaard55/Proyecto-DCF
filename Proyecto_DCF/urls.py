from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from dcf_app.views import landing, ticker_strip_view

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', landing, name='landing'),
    path('api/ticker-strip/', ticker_strip_view, name='ticker_strip'),
    path('app/', include('dcf_app.urls')),
    path('app', RedirectView.as_view(url='/app/', permanent=False)),
]
