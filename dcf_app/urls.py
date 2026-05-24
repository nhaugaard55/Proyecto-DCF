from django.urls import path
from . import views

urlpatterns = [
    path('', views.dcf_view, name='home'),
    path('reporte-ejecutivo/<str:ticker>/', views.dcf_executive_report_view, name='dcf_executive_report'),
path('watchlist/', views.watchlist_view, name='watchlist'),
    path('watchlist/toggle/', views.watchlist_toggle, name='watchlist_toggle'),
    path('watchlist/status/', views.watchlist_status, name='watchlist_status'),
    path('api/search_companies/', views.search_companies_view, name='company_search'),
    path('api/business-cycle/', views.business_cycle_view, name='business_cycle'),
]
