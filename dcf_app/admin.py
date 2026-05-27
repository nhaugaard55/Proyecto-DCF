from django.contrib import admin

from .models import AnalysisRecord, WatchlistGroup, WatchlistItem


@admin.register(AnalysisRecord)
class AnalysisRecordAdmin(admin.ModelAdmin):
    list_display = ("ticker", "company_name", "user", "metodo", "estado", "fuente_utilizada", "created_at")
    list_filter = ("metodo", "fuente_utilizada", "estado", "user")
    search_fields = ("ticker", "company_name", "sector", "user__username", "user__email")
    ordering = ("-created_at",)


@admin.register(WatchlistGroup)
class WatchlistGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "created_at")
    list_filter = ("user",)
    search_fields = ("name", "user__username", "user__email")
    ordering = ("created_at",)


@admin.register(WatchlistItem)
class WatchlistItemAdmin(admin.ModelAdmin):
    list_display = ("ticker", "company_name", "watchlist", "added_at")
    list_filter = ("watchlist",)
    search_fields = ("ticker", "company_name", "company_exchange", "watchlist__name")
    ordering = ("ticker",)
