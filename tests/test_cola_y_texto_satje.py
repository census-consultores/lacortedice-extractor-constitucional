"""Cola automática (especializadas) y PDF armado desde el texto de SATJE."""
import fitz

from resolucion.satje import tribunal as T
from worker import job_manager as jm

TEXTO = "VISTOS: " + ("La parte actora demanda el pago de haberes laborales. " * 60) + "\r\nNOTIFIQUESE."


def test_pdf_desde_texto_es_pdf_legible():
    pdf = T.pdf_desde_texto(TEXTO, "09359-2016-03321 — SENTENCIA")
    assert pdf[:4] == b"%PDF"
    doc = fitz.open(stream=pdf, filetype="pdf")
    texto = "".join(p.get_text() for p in doc)
    assert "VISTOS" in texto and "NOTIFIQUESE" in texto
    assert "no conserva el PDF" in texto


def test_fallback_texto_si_uuid_numerico_y_sin_pdf(monkeypatch):
    monkeypatch.setattr(T, "descargar_pdf", lambda uuid: None)
    act = {"ieDocumentoAdjunto": "S", "uuid": "30808724", "actividad": TEXTO,
           "tipo": "SENTENCIA (RESOLUCION) ", "fecha": "2016-12-30T13:59:00"}
    pdf, nombre = T.descargar_doc_actuacion(act, "09359201603321", "sentencia")
    assert pdf[:4] == b"%PDF"
    assert nombre.endswith("_texto_satje.pdf")


def test_sin_fallback_si_uuid_moderno(monkeypatch):
    # Un fallo de descarga de un documento moderno se reintenta, no se
    # reemplaza por el texto.
    monkeypatch.setattr(T, "descargar_pdf", lambda uuid: None)
    act = {"ieDocumentoAdjunto": "S", "uuid": "a1b2-c3d4", "actividad": TEXTO}
    assert T.descargar_doc_actuacion(act, "x", "sentencia") == (None, "")


def test_sin_fallback_si_texto_corto(monkeypatch):
    monkeypatch.setattr(T, "descargar_pdf", lambda uuid: None)
    act = {"ieDocumentoAdjunto": "S", "uuid": "123", "actividad": "Notifíquese."}
    assert T.descargar_doc_actuacion(act, "x", "sentencia") == (None, "")


def test_pdf_real_tiene_prioridad(monkeypatch):
    monkeypatch.setattr(T, "descargar_pdf", lambda uuid: b"%PDF-real")
    act = {"ieDocumentoAdjunto": "S", "uuid": "123", "actividad": TEXTO, "nombreArchivo": "s.pdf"}
    assert T.descargar_doc_actuacion(act, "x", "sentencia")[0] == b"%PDF-real"


def test_tribunales_de_spec():
    # Garantías no tiene unidades propias: "especializadas" queda vacío y todo entra por el índice.
    assert jm._tribunales_de_spec("especializadas") == []
    assert jm._tribunales_de_spec(["9332"]) == ["09332"]
    assert jm._tribunales_de_spec("todas") == sorted(jm.MATERIA.JUDICATURAS)
    assert jm._tribunales_de_spec("otra") == []


def test_rescatar_huerfanos_necesita_dos_revisiones_sin_avance(monkeypatch):
    filas = [(7, 120), (8, 50)]
    actualizados = []

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            if sql.strip().startswith("UPDATE"):
                actualizados.extend(params[0])
        def fetchall(self): return list(filas)

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()
        def commit(self): pass

    monkeypatch.setattr(jm, "_conn", lambda: Conn())
    monkeypatch.setattr(jm, "_active", {})
    jm._huerfanos_vistos.clear()
    assert jm.rescatar_huerfanos() == []          # primera revisión: solo anota
    filas[1] = (8, 60)                            # el 8 avanzó (otro proceso vivo)
    assert jm.rescatar_huerfanos() == [7]         # el 7 no avanzó: huérfano
    assert actualizados == [7]
