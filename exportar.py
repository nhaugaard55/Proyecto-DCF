
def exportar_resultado(ticker, contenido):
    nombre_archivo = f"{ticker}_analisis.txt"
    with open(nombre_archivo, "w", encoding="utf-8") as archivo:
        archivo.write(contenido)
