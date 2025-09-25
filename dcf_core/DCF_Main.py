import sys
import io
import yfinance as yf
from exportar import exportar_resultado
from finanzas import calcular_crecimientos
from empresa import analizar_empresa

if __name__ == "__main__":
    ticker = input("Ingresá el ticker de la empresa (ej: AAPL): ").upper()

    empresa_temp = yf.Ticker(ticker)
    cashflow_temp = empresa_temp.cashflow
    fcf_temp = cashflow_temp.loc["Free Cash Flow"].dropna().head(5)

    crecimiento, avg_growth_rate = calcular_crecimientos(fcf_temp)

    print(f"\n📈 CAGR calculado: {crecimiento:.2%}")
    print(f"📊 Promedio de crecimiento anual: {avg_growth_rate:.2%}")

    print("\n🔧 ¿Qué tasa de crecimiento querés usar para proyectar el FCF?")
    print("1: CAGR (Compound Annual Growth Rate)")
    print("2: Promedio de crecimiento año a año")
    metodo = input("Elegí 1 o 2 (por defecto 1): ").strip()
    if metodo != "2":
        metodo = "1"

    buffer = io.StringIO()
    sys.stdout = buffer

    resultado = analizar_empresa(ticker, metodo, crecimiento, avg_growth_rate)

    print()
    print("RESULTADOS DEL ANÁLISIS:")
    print("-" * 40)
    print(f"💵 Precio actual: ${resultado['precio_actual']:.4f}")
    print(f"💰 Valor intrínseco estimado: ${resultado['valor_intrinseco']:.4f}")
    print(f"📊 Estado: {resultado['estado']}")
    print(
        f"📈 Diferencia porcentual entre valor intrínseco y precio actual: {resultado['diferencia_pct']:.4f}%")

    sys.stdout = sys.__stdout__
    salida_completa = buffer.getvalue()

    print(salida_completa)

    exportar = input("¿Querés exportar el análisis a un archivo .txt? (s/n): ").strip().lower()
    if exportar in {"s", "si", "y", "yes"}:
        exportar_resultado(ticker, salida_completa)
        print("Archivo exportado correctamente.")
    else:
        print("No se exportó el archivo.")
