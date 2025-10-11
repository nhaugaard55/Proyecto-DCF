from django.contrib import admin

from .models import AnalysisRecord


@admin.register(AnalysisRecord)
class AnalysisRecordAdmin(admin.ModelAdmin):
    list_display = ("ticker", "company_name", "metodo", "estado", "fuente_utilizada", "created_at")
    list_filter = ("metodo", "fuente_utilizada", "estado")
    search_fields = ("ticker", "company_name", "sector")
    ordering = ("-created_at",)
