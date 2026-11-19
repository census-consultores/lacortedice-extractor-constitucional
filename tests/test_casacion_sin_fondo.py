"""
Casación terminada sin sentencia de fondo (inadmisión, improcedencia,
desistimiento, abandono): se guarda ficha de casación con el PDF del auto y la
decisión propia del catálogo, y la causa queda "Concluido en Corte Nacional".
Sin red ni IA.
"""
import pytest

import resolucion.orquestador as orq
from config import materia as MATERIA
from config.config import ID_CNJ, COMPLETITUD_ARCHIVADO
from resolucion.satje import corte
from tests.test_orquestador import _mock_causa_base, _ctx


def _cnj(monkeypatch, desc, adjunto="S"):
    inc_cnj = {"idJudicatura": ID_CNJ}
    monkeypatch.setattr(orq, "encontrar_incidente_cnj", lambda incidentes: inc_cnj)
    monkeypatch.setattr(orq, "obtener_actuaciones_cnj", lambda id_juicio, inc: [{"tipo": "x"}])
    monkeypatch.setattr(orq, "detectar_sentencia_cnj",
                        lambda acts: (desc, {"ieDocumentoAdjunto": adjunto, "fecha": "2025-08-27T12:43:00"}))
    llamadas = []
    def _descargar(act, id_juicio, prefijo):
        llamadas.append(prefijo)
        return (b"pdf-auto", "x.pdf") if prefijo == "auto_cnj" else (None, "")
    monkeypatch.setattr(orq, "descargar_doc_actuacion", _descargar)
    monkeypatch.setattr(orq, "extraer_corte", lambda **kw: pytest.fail("no debe llamar a la IA"))
    return llamadas


@pytest.mark.parametrize("desc", list(corte.DESCRIPCIONES_TERMINALES_SIN_FONDO))
def test_terminal_sin_fondo_baja_auto_y_marca_ficha(monkeypatch, desc):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})
    llamadas = _cnj(monkeypatch, desc)

    fila = orq.procesar_causa("09359-2023-00001", _ctx())

    assert llamadas[-1] == "auto_cnj"
    assert fila["_cnj_sin_fondo"] is True
    assert fila["_pdf_bytes_cnj"] == b"pdf-auto"
    assert fila["_pdf_nombre_cnj"] == "09359-2023-00001_auto_cnj.pdf"
    assert fila["Resultado en Corte"] == desc
    assert fila["Completitud CNJ"] == COMPLETITUD_ARCHIVADO
    assert fila["Estado de Juicio"] == MATERIA.ESTADO_TERMINADO_CNJ


def test_terminal_sin_adjunto_igual_marca_ficha(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})
    _cnj(monkeypatch, "Art. 270, inadmisión.", adjunto="N")
    fila = orq.procesar_causa("09359-2023-00001", _ctx())
    assert fila["_cnj_sin_fondo"] is True
    assert "_pdf_bytes_cnj" not in fila


def test_admision_no_es_terminal(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})
    _cnj(monkeypatch, "Art. 270, admisión.")
    fila = orq.procesar_causa("09359-2023-00001", _ctx())
    assert not fila.get("_cnj_sin_fondo")
    assert fila["Estado de Juicio"] != MATERIA.ESTADO_TERMINADO_CNJ


def test_detecta_inadmision_real_de_satje():
    # Orden real (DESC) de 09359-2023-00001 en la Sala Laboral de la CNJ.
    acts = [
        {"tipo": "ENVIO DEL PROCESO A CORTE PROVINCIAL (RAZON)"},
        {"tipo": "RECHAZAR REVOCATORIA Y/O REFORMA (AUTO INTERLOCUTORIO)"},
        {"tipo": "INADMISIBILIDAD DE RECURSO DE CASACIÓN (RAZON DE NOTIFICACION)"},
        {"tipo": "INADMISIBILIDAD DE RECURSO DE CASACIÓN (AUTO INTERLOCUTORIO)"},
    ]
    desc, act = corte.detectar_sentencia_cnj(acts)
    assert desc == "Art. 270, inadmisión."
    assert desc in corte.DESCRIPCIONES_TERMINALES_SIN_FONDO


def test_runner_crea_casacion_sin_fondo():
    from pathlib import Path
    from worker import runner
    assert 'or fila.get("_cnj_sin_fondo")' in Path(runner.__file__).read_text(encoding="utf-8")
