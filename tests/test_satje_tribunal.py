from config.config import ID_CNJ
from config import materia as MATERIA
"""
Tests de resolucion/satje/tribunal.py — clasificación COGEP, detección de
tribunal de primera instancia, duración del caso y búsqueda de resolución.

Todo lo de este módulo es lógica pura o llama a api.obtener_actuaciones
(mockeado) — nada toca la red real.
"""
from datetime import datetime, timezone

from resolucion.satje.tribunal import (
    clasificar_cogep,
    es_tribunal_primera_instancia,
    es_incidente_primera_instancia,
    determinar_estado,
    calcular_duracion,
    buscar_actuacion_resolucion,
    obtener_escritos_pdf,
)
from config.config import RESULTADO_EN_TRAMITE, RESULTADO_SIN_ACTUACIONES


# ── clasificar_cogep ────────────────────────────────────────────────────────

def test_clasificar_cogep_sentencia():
    assert clasificar_cogep("SENTENCIA (RESOLUCION)", "") == "Con sentencia."


def test_clasificar_cogep_prescripcion_por_tipo():
    resultado = clasificar_cogep("PRESCRIPCION", "")
    assert "153 # 6" in resultado


def test_clasificar_cogep_caducidad_por_actividad():
    # El texto de la actividad cuenta cuando la actuación es una RESOLUCION.
    resultado = clasificar_cogep("AUTO RESOLUTORIO (RESOLUCION)", "SE DECLARA LA CADUCIDAD DEL PROCESO")
    assert "153 # 7" in resultado


def test_texto_de_relleno_no_cierra_la_causa():
    # Medido en SATJE: actas, oficios y convocatorias traen las palabras de
    # relleno (formulario de excepciones, principios constitucionales).
    assert clasificar_cogep("ACTA DE AUDIENCIA UNICA (COGEP) (ACTA)",
                            "Falta de legitimación en la causa NO NO NO") == ""
    assert clasificar_cogep("OFICIO A ENTIDADES PUBLICAS (OFICIO)",
                            "mediación, celeridad y economía procesal") == ""
    assert clasificar_cogep("CONVOCATORIA AUDIENCIA DE CONCILIACION (AUTO DE SUSTANCIACION)", "") == ""
    assert clasificar_cogep("ACTA DE SORTEO", "por motivo: Inhibición Por Competencia Territorial") == ""


def test_sentencia_sin_la_palabra_sentencia():
    assert clasificar_cogep("ACEPTAR DEMANDA (RESOLUCION)", "") == "Con sentencia."
    assert clasificar_cogep("NEGAR DEMANDA (RESOLUCION)", "") == "Con sentencia."
    # Comparación exacta: otra resolución cualquiera no es sentencia.
    assert clasificar_cogep("ACEPTAR RECURSO DE APELACION (RESOLUCION)", "") != "Con sentencia."


def test_clasificar_cogep_sin_match_devuelve_vacio():
    assert clasificar_cogep("PROVIDENCIA", "TRASLADO A LA CONTRAPARTE") == ""


def test_clasificar_cogep_tipos_procesales_no_matchean_por_actividad():
    # "AUTO DE SUSTANCIACION" es un tipo procesal genérico — no debe
    # dispararse por texto libre en la actividad, solo por su propio tipo.
    resultado = clasificar_cogep("AUTO DE SUSTANCIACION", "SE ORDENA NOTIFICAR CADUCIDAD")
    assert resultado == ""


def test_clasificar_cogep_ignora_acentos_y_mayusculas():
    resultado = clasificar_cogep("prescripción", "")
    assert "153 # 6" in resultado


# ── es_tribunal_primera_instancia ───────────────────────────────────────────

def test_es_tribunal_primera_instancia_true():
    assert es_tribunal_primera_instancia("UNIDAD JUDICIAL CIVIL CON SEDE EN EL CANTÓN GUAYAQUIL")


def test_es_tribunal_primera_instancia_false_para_cnj():
    assert not es_tribunal_primera_instancia("Corte Nacional de Justicia - Sala Especializada")


def test_tribunal_de_garantias_penales_es_primera_instancia_en_garantias():
    # En garantías constitucionales cualquier juez, también el Tribunal de Garantías Penales, es primera instancia.
    assert es_tribunal_primera_instancia("Tribunal de Garantías Penales")


def test_incidente_primera_instancia_por_codigo_aunque_el_nombre_no_coincida():
    # 06102 Alausí: SATJE la nombra sin "Multicompetente" y antes se descartaba
    assert es_incidente_primera_instancia({"idJudicatura": "06102", "nombreJudicatura": "JUZGADO DE ALAUSI"})
    assert es_incidente_primera_instancia({"idJudicatura": "99999", "nombreJudicatura": "UNIDAD JUDICIAL CIVIL DE X"})
    assert not es_incidente_primera_instancia({"idJudicatura": ID_CNJ, "nombreJudicatura": "Corte Nacional de Justicia"})


# ── determinar_estado ───────────────────────────────────────────────────────

def test_determinar_estado_en_corte_nacional():
    incidentes = [{"idJudicatura": ID_CNJ}]  # ID_CNJ
    assert determinar_estado(incidentes, "") == MATERIA.ESTADO_EN_CNJ


def test_determinar_estado_en_tramite_sin_resultado():
    assert determinar_estado([], "") == MATERIA.ESTADO_EN_PRIMERA


def test_determinar_estado_en_tramite_explicito():
    assert determinar_estado([], RESULTADO_EN_TRAMITE) == MATERIA.ESTADO_EN_PRIMERA


def test_determinar_estado_sin_actuaciones():
    assert determinar_estado([], RESULTADO_SIN_ACTUACIONES) == MATERIA.ESTADO_EN_PRIMERA


def test_determinar_estado_archivado():
    assert determinar_estado([], "Con sentencia.") == "Archivado"


# ── calcular_duracion ────────────────────────────────────────────────────────

def test_calcular_duracion_sin_actuacion_resolucion():
    assert calcular_duracion(None, []) == {}


def test_calcular_duracion_caso_normal():
    act_resolucion = {"fecha": "2023-06-15T10:00:00-05:00"}
    actuaciones = [
        {"fecha": "2023-06-15T10:00:00-05:00"},
        {"fecha": "2021-01-10T09:00:00-05:00"},  # la más antigua (última de la lista DESC)
    ]
    resultado = calcular_duracion(act_resolucion, actuaciones)
    assert resultado["_fecha_inicio_caso"] == "2021-01-10"
    assert resultado["_fecha_sentencia_caso"] == "2023-06-15"
    assert resultado["_duracion_dias"] == (
        datetime(2023, 6, 15, tzinfo=timezone.utc).date()
        - datetime(2021, 1, 10, tzinfo=timezone.utc).date()
    ).days


def test_calcular_duracion_sin_fechas_validas():
    act_resolucion = {"fecha": ""}
    actuaciones = [{"fecha": ""}]
    assert calcular_duracion(act_resolucion, actuaciones) == {}


# ── buscar_actuacion_resolucion (mockea api.obtener_actuaciones) ───────────

def test_buscar_actuacion_resolucion_sin_incidentes_primera_instancia():
    resultado, act, actuaciones = buscar_actuacion_resolucion(
        "09501202300010", incidentes=[{"idJudicatura": ID_CNJ, "nombreJudicatura": "CNJ"}],
        tribunal_id="09501",
    )
    assert resultado == RESULTADO_SIN_ACTUACIONES
    assert act is None
    assert actuaciones == []


def test_buscar_actuacion_resolucion_encuentra_sentencia(monkeypatch):
    incidentes = [{"idJudicatura": "09501", "nombreJudicatura": "Unidad Judicial Civil con sede en el cantón Guayaquil"}]

    actuaciones_fake = [
        {"tipo": "SENTENCIA (RESOLUCION)", "actividad": "", "fecha": "2023-01-01T00:00:00-05:00"},
    ]

    monkeypatch.setattr(
        "resolucion.satje.tribunal.obtener_actuaciones",
        lambda id_juicio, inc: actuaciones_fake,
    )
    monkeypatch.setattr(
        "resolucion.satje.tribunal._es_razon_notificacion",
        lambda tipo: False,
    )

    resultado, act, actuaciones = buscar_actuacion_resolucion(
        "09501202300010", incidentes, tribunal_id="09501",
    )
    assert resultado == "Con sentencia."
    assert act == actuaciones_fake[0]
    assert actuaciones == actuaciones_fake


def test_buscar_actuacion_resolucion_en_tramite_sin_resolucion(monkeypatch):
    incidentes = [{"idJudicatura": "09501", "nombreJudicatura": "Unidad Judicial Civil con sede en el cantón Guayaquil"}]
    actuaciones_fake = [
        {"tipo": "PROVIDENCIA", "actividad": "TRASLADO", "fecha": "2023-01-01T00:00:00-05:00"},
    ]
    monkeypatch.setattr(
        "resolucion.satje.tribunal.obtener_actuaciones",
        lambda id_juicio, inc: actuaciones_fake,
    )
    monkeypatch.setattr(
        "resolucion.satje.tribunal._es_razon_notificacion",
        lambda tipo: False,
    )

    resultado, act, actuaciones = buscar_actuacion_resolucion(
        "09501202300010", incidentes, tribunal_id="09501",
    )
    assert resultado == RESULTADO_EN_TRAMITE
    assert act is None


# ── obtener_escritos_pdf ─────────────────────────────────────────────────────

def test_obtener_escritos_pdf_ordena_por_fecha_y_limita(monkeypatch):
    incidentes = [{"idJudicatura": "09501"}]
    actuaciones = [
        {"tipo": "ESCRITO", "ieTablaReferencia": "EscritosPrimera", "ieDocumentoAdjunto": "S",
         "fecha": "2023-01-01", "uuid": "u-nuevo"},
        {"tipo": "ESCRITO", "ieTablaReferencia": "EscritosPrimera", "ieDocumentoAdjunto": "S",
         "fecha": "2019-01-01", "uuid": "u-viejo"},
        {"tipo": "OFICIO", "ieTablaReferencia": "EscritosPrimera", "ieDocumentoAdjunto": "S",
         "fecha": "2018-01-01", "uuid": "u-oficio"},  # no es ESCRITO, se ignora
        {"tipo": "SENTENCIA (RESOLUCION)", "ieTablaReferencia": "ProvPrimera", "ieDocumentoAdjunto": "S",
         "fecha": "2024-01-01", "uuid": "u-sentencia"},  # no es EscritosPrimera, se ignora
    ]
    monkeypatch.setattr("resolucion.satje.tribunal.obtener_actuaciones", lambda id_juicio, inc: actuaciones)
    monkeypatch.setattr(
        "resolucion.satje.tribunal.obtener_anexos",
        lambda act: [{"UUID": act["uuid"] + "-anexo"}],
    )
    descargados = []
    def _fake_descargar(uuid):
        descargados.append(uuid)
        return b"pdf-bytes-" + uuid.encode()
    monkeypatch.setattr("resolucion.satje.tribunal.descargar_pdf", _fake_descargar)

    pdfs = obtener_escritos_pdf("09501202300010", incidentes, max_escritos=1)

    assert len(pdfs) == 1
    assert descargados == ["u-viejo-anexo"]  # el mas antiguo de los ESCRITO, no el OFICIO ni la SENTENCIA


def test_obtener_escritos_pdf_sin_escritos(monkeypatch):
    monkeypatch.setattr("resolucion.satje.tribunal.obtener_actuaciones", lambda id_juicio, inc: [])
    pdfs = obtener_escritos_pdf("09501202300010", [{"idJudicatura": "09501"}])
    assert pdfs == []


def test_obtener_escritos_pdf_omite_los_que_no_bajan(monkeypatch):
    actuaciones = [
        {"tipo": "ESCRITO", "ieTablaReferencia": "EscritosPrimera", "ieDocumentoAdjunto": "S",
         "fecha": "2020-01-01", "uuid": "u1"},
    ]
    monkeypatch.setattr("resolucion.satje.tribunal.obtener_actuaciones", lambda id_juicio, inc: actuaciones)
    monkeypatch.setattr("resolucion.satje.tribunal.obtener_anexos", lambda act: [{"UUID": "u1-anexo"}])
    monkeypatch.setattr("resolucion.satje.tribunal.descargar_pdf", lambda uuid: None)

    pdfs = obtener_escritos_pdf("09501202300010", [{"idJudicatura": "09501"}])
    assert pdfs == []


def _acts(*filas):
    return [{"tipo": t, "fecha": f, "actividad": a} for t, f, a in filas]


def _buscar(por_juzgado, tribunal_id):
    from resolucion.satje import tribunal as tr
    incs = [{"idJudicatura": j, "nombreJudicatura": j} for j in por_juzgado]
    orig = tr.obtener_actuaciones, tr.es_tribunal_primera_instancia
    tr.obtener_actuaciones = lambda idj, inc: por_juzgado[inc["idJudicatura"]]
    tr.es_tribunal_primera_instancia = lambda nombre: True
    try:
        return tr.buscar_actuacion_resolucion("X", incs, tribunal_id)
    finally:
        tr.obtener_actuaciones, tr.es_tribunal_primera_instancia = orig


def test_traslado_de_juzgado_la_causa_sigue_viva():
    # 08304-2025-00136: el primero se inhibe, la causa sigue en 08257.
    res, act, acts = _buscar({
        "08304": _acts(("INCOMPETENCIA DEL TRIBUNAL (AUTO INTERLOCUTORIO)", "2025-11-13", "")),
        "08257": _acts(("ESCRITO", "2026-10-22", ""), ("ACTA DE SORTEO", "2025-11-24", "Inhibición")),
    }, "08304")
    assert res.startswith("En tramite") and act is None
    assert acts[0]["fecha"] == "2026-10-22"   # las del juzgado donde está hoy


def test_traslado_con_sentencia_en_el_segundo_juzgado():
    # 07333-2022-01307: la sentencia está en el juzgado al que llegó.
    res, act, _ = _buscar({
        "07333": _acts(("INCOMPETENCIA DEL TRIBUNAL (AUTO INTERLOCUTORIO)", "2024-01-20", "")),
        "07307": _acts(("SENTENCIA ACEPTANDO DEMANDA (RESOLUCION)", "2025-05-01", "")),
    }, "07333")
    assert res == "Con sentencia." and act["fecha"] == "2025-05-01"


def test_incompetencia_sin_otro_juzgado_cierra_la_causa():
    res, act, _ = _buscar({
        "17230": _acts(("INCOMPETENCIA DEL TRIBUNAL (AUTO INTERLOCUTORIO)", "2023-07-13", "")),
    }, "17230")
    assert "incompetencia" in res


def test_sentencia_gana_a_actuacion_posterior_por_texto():
    # 03334-2017-00019: un oficio posterior a la sentencia hablaba de caducidad.
    res, act, _ = _buscar({
        "03334": _acts(("CADUCIDAD (AUTO INTERLOCUTORIO)", "2017-11-24", ""),
                       ("SENTENCIA (RESOLUCION)", "2017-09-04", "")),
    }, "03334")
    assert res == "Con sentencia."


def test_cobro_cerrado_por_pago():
    assert "pago" in clasificar_cogep("SOLUCION O PAGO Y/O EXTINCION DE LA OBLIGACION (AUTO INTERLOCUTORIO)", "")


def test_archivos_de_civil():
    assert clasificar_cogep("ARCHIVO POR NO COMPLETAR DEMANDA (AUTO INTERLOCUTORIO)", "") == "Art. 146, demanda incompleta."
    assert clasificar_cogep("ARCHIVO DEL PROCESO (AUTO INTERLOCUTORIO)", "") == "Archivo del proceso."
    # El motivo con nombre propio sigue ganando.
    assert "incompetencia" in clasificar_cogep("ARCHIVO POR FALTA DE COMPETENCIA (AUTO INTERLOCUTORIO)", "")


def test_inadmitir_demanda():
    assert "inadmisión" in clasificar_cogep("INADMITIR DEMANDA Y/O SOLICITUD (AUTO INTERLOCUTORIO)", "")
    assert clasificar_cogep("INADMITIR RECURSO DE APELACION (AUTO INTERLOCUTORIO)", "") == ""


def test_archivo_generico_cede_al_motivo_real_anterior():
    # 04332-2021-00059: conciliación total en 2022, archivo administrativo en 2025.
    res, act, _ = _buscar({
        "04332": _acts(("ARCHIVO DEL PROCESO (AUTO INTERLOCUTORIO)", "2025-02-26", ""),
                       ("CONCILIACION TOTAL (RESOLUCION)", "2022-03-30", "")),
    }, "04332")
    assert res == "Art. 234, conciliación." and act["fecha"] == "2022-03-30"


def test_archivo_generico_solo():
    res, _, _ = _buscar({"14305": _acts(("ARCHIVO DEL PROCESO (AUTO INTERLOCUTORIO)", "2017-10-05", ""))}, "14305")
    assert res == "Archivo del proceso."


def test_nombre_de_la_actuacion_manda_sobre_el_texto():
    assert clasificar_cogep("CONCILIACION TOTAL (RESOLUCION)",
                            "por el principio de inmediación asumo la competencia… transacción") == "Art. 234, conciliación."


def test_palabras_completas_en_el_texto():
    assert clasificar_cogep("AUTO RESOLUTORIO (RESOLUCION)", "principio de inmediación") == ""
    assert "mediación" in clasificar_cogep("AUTO RESOLUTORIO (RESOLUCION)", "se aprueba el acuerdo de mediación")


def test_derivacion_a_mediacion_no_cierra():
    assert clasificar_cogep("DERIVACION A MEDIACION (AUTO INTERLOCUTORIO)", "") == ""


def test_archivo_tras_excepcion_de_incompetencia():
    res, _, _ = _buscar({"17230": _acts(("ARCHIVO DEL PROCESO (AUTO INTERLOCUTORIO)", "2023-08-23", ""),
                                        ("EXCEPCION POR INCOMPETENCIA DEL JUZGADOR (AUTO INTERLOCUTORIO)", "2023-07-13", ""))}, "17230")
    assert "incompetencia" in res
