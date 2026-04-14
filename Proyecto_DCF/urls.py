"""
URL configuration for Proyecto_DCF project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from dcf_app.views import (
    dcf_view, dcf_pdf_view, dcf_excel_view,
    search_companies_view, business_cycle_view,
    watchlist_view, watchlist_toggle, watchlist_status,
    comparar_view,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', dcf_view, name='home'),
    path('dcf/pdf/', dcf_pdf_view, name='dcf_pdf'),
    path('dcf/excel/', dcf_excel_view, name='dcf_excel'),
    path('watchlist/', watchlist_view, name='watchlist'),
    path('watchlist/toggle/', watchlist_toggle, name='watchlist_toggle'),
    path('watchlist/status/', watchlist_status, name='watchlist_status'),
    path('comparar/', comparar_view, name='comparar'),
    path('api/search_companies/', search_companies_view, name='company_search'),
    path('api/business-cycle/', business_cycle_view, name='business_cycle'),
]
