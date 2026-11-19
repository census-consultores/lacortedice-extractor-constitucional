"""
Tests del formato del Word generado (resolucion/ai/_cliente.py):
  - Los títulos de sub-sección (numerados o tipo "Etiqueta:") deben salir
    en negrita, distintos del cuerpo — antes se veían exactamente igual,
    lo que hacía difícil auditar visualmente dónde termina una sub-sección
    y empieza la siguiente.
"""
import io
from docx import Document

from resolucion.ai._cliente import crear_docx_tribunal, crear_docx_corte


def _negritas(doc):
    return {
        p.text.strip(): [r.text for r in p.runs if r.bold]
        for p in doc.paragraphs if p.text.strip()
    }


def test_titulo_numerado_sale_en_negrita():
    extraccion = "14. Argumentos de la Parte Actora\nContenido del argumento."
    data = crear_docx_tribunal("09501-2019-00010", "Resumen.", extraccion, "Jur.", "Clas.")
    doc = Document(io.BytesIO(data))
    negritas = _negritas(doc)
    assert negritas["14. Argumentos de la Parte Actora"] == ["14. Argumentos de la Parte Actora"]
    # El cuerpo NO debe llevar negrita.
    assert negritas["Contenido del argumento."] == []


def test_etiqueta_con_valor_en_la_misma_linea_solo_negrita_la_etiqueta():
    extraccion = "Fecha: 12 de febrero del 2020"
    data = crear_docx_tribunal("09501-2019-00010", "Resumen.", extraccion, "Jur.", "Clas.")
    doc = Document(io.BytesIO(data))
    doc2 = Document(io.BytesIO(data))
    parrafo = next(p for p in doc2.paragraphs if p.text.strip().startswith("Fecha:"))
    bold_runs = [r.text for r in parrafo.runs if r.bold]
    plain_runs = [r.text for r in parrafo.runs if not r.bold]
    assert bold_runs == ["Fecha:"]
    assert plain_runs == [" 12 de febrero del 2020"]


def test_etiqueta_sola_sin_contenido_en_negrita():
    extraccion = "18. Voto Salvado\nVoto salvado:\nNo consta en la sentencia."
    data = crear_docx_tribunal("09501-2019-00010", "Resumen.", extraccion, "Jur.", "Clas.")
    doc = Document(io.BytesIO(data))
    negritas = _negritas(doc)
    assert negritas["Voto salvado:"] == ["Voto salvado:"]
    assert negritas["No consta en la sentencia."] == []


def test_linea_normal_sin_dos_puntos_no_lleva_negrita():
    extraccion = "El tribunal considera que los gastos fueron correctamente valorados."
    data = crear_docx_tribunal("09501-2019-00010", "Resumen.", extraccion, "Jur.", "Clas.")
    doc = Document(io.BytesIO(data))
    negritas = _negritas(doc)
    assert negritas[extraccion] == []


def test_docx_corte_tambien_aplica_negrita():
    extraccion = "8. Causal o causales invocadas:\nCAUSAL 1 - Aplicación indebida."
    data = crear_docx_corte("09501-2019-00010", "Resumen.", extraccion, "Jur.")
    doc = Document(io.BytesIO(data))
    negritas = _negritas(doc)
    assert negritas["8. Causal o causales invocadas:"] == ["8. Causal o causales invocadas:"]
