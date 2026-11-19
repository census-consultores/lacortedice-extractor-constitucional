"""Piloto civil v2: rótulos de normativa y decisión no repetida en la ficha Word."""
import io
from docx import Document
from resolucion.ai._cliente import _agregar_seccion_docx, _perfil_caso_texto


def _texto(contenido):
    doc = Document()
    _agregar_seccion_docx(doc, "II", contenido)
    buf = io.BytesIO(); doc.save(buf)
    return [p.text for p in Document(io.BytesIO(buf.getvalue())).paragraphs]


def test_normativa_se_muestra_como_invocada_por_la_parte():
    t = _texto("14. Argumentos de la Parte Actora:\n• Hecho\nNORMATIVA APLICADA:\nArt. 1 del Código Civil\n"
               "15. Argumentos de la Parte Demandada:\nNORMATIVA APLICADA:\n"
               "No consta normativa aplicada atribuible a esta parte en la sentencia.")
    assert "NORMATIVA INVOCADA POR LA PARTE:" in t and "NORMATIVA APLICADA:" not in t
    assert "No consta en la sentencia normativa invocada por esta parte." in t


def test_frase_canonica_final_no_se_repite_pero_la_decision_si_queda():
    t = _texto("16. Decisión Final:\nSe acepta la demanda y se ordena el pago.\nAcepta la demanda.\n17. Disposiciones Adicionales:\nNada.")
    assert "Acepta la demanda." not in t and "Se acepta la demanda y se ordena el pago." in t


def test_frase_canonica_sola_se_conserva():
    t = _texto("16. Decisión Final:\nAcepta la demanda.\n17. Disposiciones Adicionales:\nNada.")
    assert "Acepta la demanda." in t


def test_puntos_controvertidos_con_tildes():
    txt = _perfil_caso_texto("", "", {"controversias": ["CARGA_PRUEBA", "COBRO_OBLIGACION_DINERARIA", "XYZ_NUEVO"]})
    assert "Carga de la prueba; Cobro de obligación dineraria; Xyz nuevo" in txt
