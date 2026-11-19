"""
Tests de resolucion/orquestador.py::procesar_causa — el punto de entrada
único (SATJE → IA → fila dict) que worker/runner.py debe empezar a llamar
en vez del monolito viejo resolucion/extractor.py.

Se mockean SATJE (api.py), los extractores (IA) y las funciones satje/* —
ningún test acá toca la red real ni gasta tokens de IA.
"""
import pytest
from config.config import ID_CNJ

import resolucion.orquestador as orq
from config import materia as _MAT
_ID_MATERIA = next(iter(_MAT.IDS_MATERIA_SATJE), 36)
from config.config import (
    COMPLETITUD_NO_APLICABLE, COMPLETITUD_SIN_ACTUS, COMPLETITUD_PDF_SIN_GPT,
    COMPLETITUD_PENDIENTE, COMPLETITUD_ARCHIVADO,
    RESULTADO_EN_TRAMITE, RESULTADO_CNJ_EN_TRAMITE,
)


def _ctx(**overrides):
    base = dict(
        tribunal="09501",
        nombre_tribunal="Unidad Judicial Civil con sede en el cantón Guayaquil",
        ano="2023",
        corrida_id="test-corrida",
        carpeta_local=None,
        carpeta_docx=None,
        gpt_ok=True,
        gpt_cnj_ok=True,
        skip_pdf=False,
        modelo="gpt-4o",
        job_id=None,
    )
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _no_op_normalizar(monkeypatch):
    # normalizar_numero es pura, no hace falta mockearla — pero evitamos que
    # ningún test dependa por accidente de logging con DB real.
    monkeypatch.setattr(orq.log, "info", lambda *a, **kw: None)
    monkeypatch.setattr(orq.log, "warn", lambda *a, **kw: None)
    monkeypatch.setattr(orq.log, "error", lambda *a, **kw: None)


def test_causa_no_encontrada(monkeypatch):
    monkeypatch.setattr(orq, "buscar_causa", lambda numero: None)

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Completitud"] == COMPLETITUD_SIN_ACTUS
    assert fila["Número de juicio"] == "09501-2023-00010"


def test_causa_de_otra_materia(monkeypatch):
    # Sin ids SATJE verificados, la materia se decide por la judicatura de los
    # incidentes: una Unidad Judicial Penal no es de esta materia.
    monkeypatch.setattr(orq, "buscar_causa", lambda numero: {"idMateria": 5, "nombreDelito": "Penal"})
    monkeypatch.setattr(orq, "obtener_incidentes", lambda id_juicio: [
        {"idJudicatura": "09281", "nombreJudicatura": "UNIDAD JUDICIAL PENAL CON SEDE EN GUAYAQUIL",
         "lstIncidenteJudicatura": [{}]}
    ])

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Completitud"] == COMPLETITUD_NO_APLICABLE
    from config import materia as MATERIA
    assert fila["Estado de Juicio"] == MATERIA.ETIQUETA_OTRA_MATERIA


def test_sin_incidentes(monkeypatch):
    monkeypatch.setattr(orq, "buscar_causa", lambda numero: {"idMateria": _ID_MATERIA, "fechaIngreso": ""})
    monkeypatch.setattr(orq, "obtener_incidentes", lambda id_juicio: [])

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Completitud"] == COMPLETITUD_SIN_ACTUS
    assert fila["Estado de Juicio"] == "Sin resultados"


def _mock_causa_base(monkeypatch, resultado_tribunal, act_resolucion, actuaciones=None,
                      actor="Juan Perez", demandado="SRI"):
    monkeypatch.setattr(orq, "buscar_causa", lambda numero: {
        "idMateria": _ID_MATERIA, "fechaIngreso": "2022-01-01T00:00:00+00:00",
    })
    incidente = {
        "idJudicatura": "09501",
        "nombreJudicatura": "Unidad Judicial Civil con sede en el cantón Guayaquil",
        "lstIncidenteJudicatura": [{
            "lstLitiganteActor":     [{"nombresLitigante": actor}],
            "lstLitiganteDemandado": [{"nombresLitigante": demandado}],
        }],
    }
    monkeypatch.setattr(orq, "obtener_incidentes", lambda id_juicio: [incidente])
    monkeypatch.setattr("resolucion.satje.tribunal.es_tribunal_primera_instancia", lambda nombre: True)
    monkeypatch.setattr(
        orq, "buscar_actuacion_resolucion",
        lambda *a, **kw: (resultado_tribunal, act_resolucion, actuaciones or []),
    )
    monkeypatch.setattr(orq, "calcular_duracion", lambda *a, **kw: {})
    monkeypatch.setattr(orq, "determinar_estado", lambda *a, **kw: "Archivado")
    monkeypatch.setattr(orq, "encontrar_incidente_cnj", lambda incidentes: None)


def test_extrae_actor_y_demandado(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (None, ""))

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Actor"] == "Juan Perez"
    assert fila["Demandado"] == "SRI"


def test_con_sentencia_gpt_ok_llama_extractor_y_propaga_campos(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "S"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (b"pdf-bytes", "x.pdf"))

    llamado = {}
    def _fake_extraer_tribunal(**kwargs):
        llamado.update(kwargs)
        return {
            "Cuantía": "500.00", "Tipo de Acción": "Impugnación",
            "Completitud": "Completo", "_pdf_bytes_tribunal": b"pdf-bytes",
            "_pdf_nombre_tribunal": "x.pdf",
            "Abogado del Actor": "Ab. Juan Pérez",
            "Abogado del Demandado": "Dra. María López",
        }
    monkeypatch.setattr(orq, "extraer_tribunal", _fake_extraer_tribunal)

    fila = orq.procesar_causa("09501-2023-00010", _ctx(modelo="gpt-4o"))

    assert llamado["modelo"] == "gpt-4o"
    assert llamado["demandado"] == "SRI"
    assert fila["Cuantía"] == "500.00"
    assert fila["Tipo de Acción"] == "Impugnación"
    assert fila["Completitud"] == "Completo"
    assert fila["Abogado del Actor"] == "Ab. Juan Pérez"
    assert fila["Abogado del Demandado"] == "Dra. María López"


def test_fallback_abogados_se_activa_cuando_sentencia_no_los_trae(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "S"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (b"pdf-bytes", "x.pdf"))
    monkeypatch.setattr(orq, "extraer_tribunal", lambda **kw: {
        "Completitud": "Completo", "Abogado del Actor": "", "Abogado del Demandado": "",
        "_auditoria": {"abogados": False},
    })

    llamado_escritos = {}
    monkeypatch.setattr(orq, "obtener_escritos_pdf", lambda *a, **kw: (llamado_escritos.__setitem__("ok", True) or [b"pdf1", b"pdf2"]))
    monkeypatch.setattr(
        orq, "completar_abogados_con_escritos",
        lambda escritos, **kw: {"abogado_actor": "Ab. Juan Pérez", "abogado_demandado": "Dra. María López"},
    )

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert llamado_escritos.get("ok") is True
    assert fila["Abogado del Actor"] == "Ab. Juan Pérez"
    assert fila["Abogado del Demandado"] == "Dra. María López"
    assert fila["_auditoria"]["abogados"] is True


def test_fallback_abogados_no_se_activa_si_sentencia_ya_los_trajo(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "S"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (b"pdf-bytes", "x.pdf"))
    monkeypatch.setattr(orq, "extraer_tribunal", lambda **kw: {
        "Completitud": "Completo", "Abogado del Actor": "Ab. X", "Abogado del Demandado": "Ab. Y",
        "_auditoria": {"abogados": True},
    })
    monkeypatch.setattr(
        orq, "obtener_escritos_pdf",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no deberia llamarse")),
    )

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Abogado del Actor"] == "Ab. X"
    assert fila["Abogado del Demandado"] == "Ab. Y"


def test_fallback_abogados_sin_escritos_disponibles_no_rompe(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "S"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (b"pdf-bytes", "x.pdf"))
    monkeypatch.setattr(orq, "extraer_tribunal", lambda **kw: {
        "Completitud": "Completo", "Abogado del Actor": "", "Abogado del Demandado": "",
        "_auditoria": {"abogados": False},
    })
    monkeypatch.setattr(orq, "obtener_escritos_pdf", lambda *a, **kw: [])

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Abogado del Actor"] == ""
    assert fila["Abogado del Demandado"] == ""


def test_con_sentencia_gpt_desactivado_no_llama_extractor(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "S"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (b"pdf-bytes", "x.pdf"))

    llamado = {"veces": 0}
    monkeypatch.setattr(orq, "extraer_tribunal", lambda **kw: llamado.__setitem__("veces", 1))

    fila = orq.procesar_causa("09501-2023-00010", _ctx(gpt_ok=False))

    assert llamado["veces"] == 0
    assert fila["Completitud"] == COMPLETITUD_PDF_SIN_GPT
    assert fila["_pdf_bytes_tribunal"] == b"pdf-bytes"


def test_skip_pdf_no_descarga(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "S"})

    llamado = {"veces": 0}
    monkeypatch.setattr(
        orq, "descargar_doc_actuacion",
        lambda *a, **kw: llamado.__setitem__("veces", llamado["veces"] + 1) or (b"x", "x.pdf"),
    )

    fila = orq.procesar_causa("09501-2023-00010", _ctx(skip_pdf=True))

    assert llamado["veces"] == 0
    assert fila["Completitud"] == COMPLETITUD_PDF_SIN_GPT


def test_pdf_no_disponible_en_satje(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (None, ""))

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    # No se pudo bajar PDF: cae al fallback de completitud (no hay "Con
    # sentencia" en rt porque _mock_causa_base pasa resultado literal).
    assert fila["Completitud"] == COMPLETITUD_PENDIENTE
    assert "_pdf_bytes_tribunal" not in fila


def test_motivo_cogep_archivado_sin_pdf(monkeypatch):
    # act_resolucion presente pero NO es "Con sentencia" → archivado, sin PDF.
    _mock_causa_base(monkeypatch, "Art. 153 # 6, prescripción.", {"ieDocumentoAdjunto": "N"})

    llamado = {"veces": 0}
    monkeypatch.setattr(
        orq, "descargar_doc_actuacion",
        lambda *a, **kw: llamado.__setitem__("veces", llamado["veces"] + 1) or (None, ""),
    )

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert llamado["veces"] == 0  # nunca se intenta bajar PDF si no es sentencia
    assert fila["Completitud"] == COMPLETITUD_ARCHIVADO
    assert fila["Resultado tribunal"] == "Art. 153 # 6, prescripción."


def test_sin_cnj_no_agrega_campos_cnj(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (None, ""))

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Resultado en Corte"] == ""
    assert fila["Completitud CNJ"] == ""


def test_con_cnj_sentencia_sustantiva_llama_extractor_corte(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})

    inc_cnj = {"idJudicatura": ID_CNJ}
    monkeypatch.setattr(orq, "encontrar_incidente_cnj", lambda incidentes: inc_cnj)
    monkeypatch.setattr(orq, "obtener_actuaciones_cnj", lambda id_juicio, inc: [{"tipo": "x"}])
    monkeypatch.setattr(
        orq, "detectar_sentencia_cnj",
        lambda acts: ("Casa la sentencia.", {"ieDocumentoAdjunto": "S"}),
    )

    def _descargar(act, id_juicio, prefijo):
        if prefijo == "sentencia_cnj":
            return b"pdf-cnj", "x_cnj.pdf"
        return None, ""
    monkeypatch.setattr(orq, "descargar_doc_actuacion", _descargar)

    llamado = {}
    def _fake_extraer_corte(**kwargs):
        llamado.update(kwargs)
        return {"Completitud CNJ": "Completo", "_resumen_cnj": "resumen"}
    monkeypatch.setattr(orq, "extraer_corte", _fake_extraer_corte)

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert llamado["numero"] == "09501-2023-00010"
    assert fila["Resultado en Corte"] == "Casa la sentencia."
    assert fila["Completitud CNJ"] == "Completo"
    assert fila["_resumen_cnj"] == "resumen"
    assert fila["Sentencia Corte Nacional"] == "09501-2023-00010_sentencia_cnj.pdf"


def test_duracion_cnj_se_calcula_y_no_pisa_la_del_tribunal(monkeypatch):
    """
    calcular_duracion() se llama una vez para tribunal (con `actuaciones`,
    vacío en este mock) y una vez para CNJ (con `acts_cnj`) — deben quedar
    en claves separadas de `fila` (_fecha_inicio_cnj / _duracion_dias_cnj)
    sin pisar las del tribunal (_fecha_inicio_caso / _duracion_dias).
    """
    acts_cnj = [{"tipo": "x"}]
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"}, actuaciones=[])

    def _calcular_duracion(act_resolucion, actuaciones):
        if actuaciones is acts_cnj:
            return {
                "_fecha_inicio_caso": "2020-06-09",
                "_fecha_sentencia_caso": "2023-10-10",
                "_duracion_dias": 1219,
            }
        return {}
    monkeypatch.setattr(orq, "calcular_duracion", _calcular_duracion)

    inc_cnj = {"idJudicatura": ID_CNJ}
    monkeypatch.setattr(orq, "encontrar_incidente_cnj", lambda incidentes: inc_cnj)
    monkeypatch.setattr(orq, "obtener_actuaciones_cnj", lambda id_juicio, inc: acts_cnj)
    monkeypatch.setattr(
        orq, "detectar_sentencia_cnj",
        lambda acts: ("Casa la sentencia.", {"ieDocumentoAdjunto": "N"}),
    )
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (None, ""))

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["_fecha_inicio_cnj"] == "2020-06-09"
    assert fila["_fecha_sentencia_cnj"] == "2023-10-10"
    assert fila["_duracion_dias_cnj"] == 1219
    # La del tribunal no debe quedar contaminada con el valor de CNJ.
    assert fila.get("_fecha_inicio_caso") in (None, "")
    assert fila.get("_duracion_dias") is None


def test_con_cnj_no_sustantiva_archiva(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})
    monkeypatch.setattr(orq, "descargar_doc_actuacion", lambda *a, **kw: (None, ""))

    inc_cnj = {"idJudicatura": ID_CNJ}
    monkeypatch.setattr(orq, "encontrar_incidente_cnj", lambda incidentes: inc_cnj)
    monkeypatch.setattr(orq, "obtener_actuaciones_cnj", lambda id_juicio, inc: [{"tipo": "x"}])
    monkeypatch.setattr(
        orq, "detectar_sentencia_cnj",
        lambda acts: ("Art. 238, desistimiento.", {"ieDocumentoAdjunto": "N"}),
    )

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Completitud CNJ"] == COMPLETITUD_ARCHIVADO


def test_con_cnj_actuaciones_sin_resolucion_aun(monkeypatch):
    # Hay actividad en el incidente CNJ pero todavía ninguna resolución
    # sustantiva → "en trámite", no "sin actuaciones".
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})

    inc_cnj = {"idJudicatura": ID_CNJ}
    monkeypatch.setattr(orq, "encontrar_incidente_cnj", lambda incidentes: inc_cnj)
    monkeypatch.setattr(orq, "obtener_actuaciones_cnj", lambda id_juicio, inc: [{"tipo": "ACTA DE SORTEO"}])
    monkeypatch.setattr(orq, "detectar_sentencia_cnj", lambda acts: ("", None))

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Resultado en Corte"] == RESULTADO_CNJ_EN_TRAMITE
    assert fila["Completitud CNJ"] == COMPLETITUD_PENDIENTE


def test_con_cnj_sin_actuaciones(monkeypatch):
    _mock_causa_base(monkeypatch, "Con sentencia.", {"ieDocumentoAdjunto": "N"})

    inc_cnj = {"idJudicatura": ID_CNJ}
    monkeypatch.setattr(orq, "encontrar_incidente_cnj", lambda incidentes: inc_cnj)
    monkeypatch.setattr(orq, "obtener_actuaciones_cnj", lambda id_juicio, inc: [])
    monkeypatch.setattr(orq, "detectar_sentencia_cnj", lambda acts: ("", None))

    fila = orq.procesar_causa("09501-2023-00010", _ctx())

    assert fila["Resultado en Corte"] == "Sin actuaciones CNJ"
    assert fila["Completitud CNJ"] == COMPLETITUD_PENDIENTE
