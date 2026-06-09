"""Utilidades compartidas entre los módulos de dcf_core."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Detección de tipo de empresa
# ---------------------------------------------------------------------------

_SECTORES_FINANCIEROS: frozenset[str] = frozenset({
    "Financial Services",
    "Financials",
    "Finance",
})

_INDUSTRIAS_FINANCIERAS_KW: tuple[str, ...] = (
    "bank", "insurance", "capital markets", "asset management",
    "credit services", "financial", "savings", "thrift", "mortgage",
    "brokerage", "investment", "diversified financial",
)


def es_sector_financiero(sector: str, industria: str = "") -> bool:
    """True si la empresa pertenece al sector financiero (banco, aseguradora, etc.)."""
    if (sector or "").strip() in _SECTORES_FINANCIEROS:
        return True
    industria_lower = (industria or "").lower()
    return any(kw in industria_lower for kw in _INDUSTRIAS_FINANCIERAS_KW)


def parse_datetime_epoch(epoch_seconds: Optional[int]) -> Optional[datetime]:
    """Convierte un timestamp Unix (segundos) a datetime con zona UTC."""
    if not epoch_seconds:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def parse_datetime_iso(value: Optional[str]) -> Optional[datetime]:
    """Convierte una cadena ISO 8601 a datetime con zona UTC."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None

    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
