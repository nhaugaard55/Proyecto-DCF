from django.db import models


class AnalysisRecord(models.Model):
    """Persisted snapshot of a DCF analysis run on the platform."""

    METODO_CAGR = "1"
    METODO_PROMEDIO = "2"
    METODO_CHOICES = [
        (METODO_CAGR, "CAGR (compuesto)"),
        (METODO_PROMEDIO, "Promedio año a año"),
    ]

    ticker = models.CharField(max_length=16)
    company_name = models.CharField(max_length=255)
    company_exchange = models.CharField(max_length=64, blank=True)
    sector = models.CharField(max_length=128, blank=True)
    metodo = models.CharField(max_length=2, choices=METODO_CHOICES)
    fuente_solicitada = models.CharField(max_length=16, blank=True)
    fuente_utilizada = models.CharField(max_length=16, blank=True)
    valor_intrinseco = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    precio_actual = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    diferencia_pct = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    estado = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("ticker", "created_at")),
        ]

    def __str__(self) -> str:
        return f"{self.ticker} - {self.company_name}"

    @property
    def fuente_display(self) -> str:
        etiquetas = {
            "fmp": "Financial Modeling Prep",
            "yfinance": "YFinance",
            "auto": "Automático",
        }
        return etiquetas.get(self.fuente_utilizada or "", self.fuente_utilizada or "N/D")
