"""PDF indicado pero no descargado: queda marcado en Observaciones (para la
segunda vuelta manual) y NO entra al reintento automático de errores de red."""
from resolucion import orquestador as orq


def test_marca_al_inicio_y_sin_repetir():
    fila = {"Observaciones": "CNJ: algo"}
    orq._marcar_pdf_fallido(fila, "apelacion")
    orq._marcar_pdf_fallido(fila, "apelacion")
    assert fila["Observaciones"] == "[PDF_FALLIDO:apelacion] CNJ: algo"
    orq._marcar_pdf_fallido(fila, "cnj")
    assert fila["Observaciones"].startswith("[PDF_FALLIDO:cnj] [PDF_FALLIDO:apelacion]")


def test_el_reintento_automatico_no_la_toma():
    src = open("worker/job_manager.py", encoding="utf-8").read()
    i = src.index("def _reintentar_errores_red")
    bloque = src[i:i + 3000]
    assert "[SATJE_ERROR]" in bloque and "PDF_FALLIDO" not in bloque
