"""
Scraping de SATJE para sentencias de PRIMERA INSTANCIA.

Funciones que consultan la API del SATJE, clasifican actuaciones según el
COGEP y detectan resoluciones de primera instancia de la materia configurada.

buscar_actuacion_resolucion(id_juicio, incidentes, tribunal_id)
    → (resultado_tribunal, actuacion, actuaciones_lista)

Tablas COGEP y lógica de clasificación extraídas de extractor.py.
"""
import re

from api import (
    obtener_actuaciones, obtener_anexos, descargar_pdf,
    limpiar_html, _fecha_iso, _es_razon_notificacion,
)
from worker import logger as log

# ── Constante de Corte Nacional (para identificar incidentes CNJ) ─────────────
from config.config import ID_CNJ
from config import materia as MATERIA


# ── Catálogo COGEP ────────────────────────────────────────────────────────────
# Orden importa: se evalúa de más específico a más general.
COGEP = [
    ("Art. 153 # 1, incompetencia de la o del juzgador.",
     ["INCOMPETENCIA DEL JUZGADOR", "INHIBICION POR TERRITORIO"],
     ["INCOMPETENCIA DEL JUZGADOR", "INHIBICION POR TERRITORIO"]),
    ("Art. 153 # 2, la incapacidad o falta de personería de la parte actora o su representante.",
     ["FALTA DE PERSONERIA", "PERSONERÍA"],
     ["FALTA DE PERSONERIA", "INCAPACIDAD O FALTA DE PERSONERIA"]),
    ("Art. 153 # 3, falta de legitimación en la causa o incompleta conformación de litis consorcio.",
     ["FALTA DE LEGITIMACION", "LITIS CONSORCIO"],
     ["FALTA DE LEGITIMACION", "LITIS CONSORCIO", "LEGITIMACION EN LA CAUSA"]),
    ("Art. 153 # 4, error en la forma de proponer la demanda, inadecuación del procedimiento o indebida acumulación de pretensiones.",
     ["INADECUACION DEL PROCEDIMIENTO"],
     ["INADECUACION DEL PROCEDIMIENTO", "ERROR EN LA FORMA DE PROPONER"]),
    ("Art. 153 # 5, litispendencia.", ["LITISPENDENCIA"], ["LITISPENDENCIA"]),
    ("Art. 153 # 6, prescripción.", ["PRESCRIPCION"],
     ["PRESCRIPCION DE LA ACCION", "PRESCRIPCION DEL DERECHO"]),
    ("Art. 153 # 7, caducidad.", ["CADUCIDAD"], ["CADUCIDAD"]),
    ("Art. 153 # 8, cosa juzgada.", ["COSA JUZGADA"], ["COSA JUZGADA"]),
    ("Art. 153 # 9, transacción.", ["TRANSACCION"], ["ART. 153", "TRANSACCION"]),
    ("Art. 153 # 10, existencia de convenio, compromiso arbitral o convenio de mediación.",
     ["CONVENIO ARBITRAL"], ["CONVENIO ARBITRAL", "CONVENIO DE MEDIACION"]),
    # "ARCHIVO POR NO COMPLETAR DEMANDA (AUTO INTERLOCUTORIO)": así lo registran
    # los juzgados civiles (07310-2022-00004, 12312-2019-00074).
    ("Art. 146, demanda incompleta.", ["DEMANDA INCOMPLETA", "NO COMPLETAR DEMANDA"], ["DEMANDA INCOMPLETA"]),
    ("Art. 147, incompetencia del tribunal.",
     ["INCOMPETENCIA DEL TRIBUNAL", "ARCHIVO POR FALTA DE COMPETENCIA", "INHIBICION POR COMPETENCIA"],
     ["INCOMPETENCIA DEL TRIBUNAL", "FALTA DE COMPETENCIA", "INHIBICION POR COMPETENCIA"]),
    ("Art. 147, indebida acumulación de pretensiones.",
     ["ACUMULACION DE PRETENSIONES"], ["INDEBIDA ACUMULACION DE PRETENSIONES"]),
    ("Art. 16, acumulación de procesos.",
     ["ACUMULACION DE PROCESOS"], ["ACUMULACION DE PROCESOS"]),
    ("Art. 236, retiro de la demanda.",
     ["RETIRO DE LA DEMANDA"], ["RETIRO DE LA DEMANDA"]),
    ("Art. 238, desistimiento.", ["DESISTIMIENTO"], ["DESISTIMIENTO"]),
    ("Art. 102, mediación.", ["MEDIACION", "ACUERDO DE MEDIACION"],
     ["MEDIACION", "ACUERDO DE MEDIACION"]),
    ("Art. 235, transacción.", ["TRANSACCION"], ["TRANSACCION"]),
    ("Art. 234, conciliación.", ["CONCILIACION"], ["CONCILIACION"]),
    ("Art. 245, abandono.", ["ABANDONO"], ["ABANDONO", "SE DECLARA EL ABANDONO"]),
    ("Art. 87, abandono por falta de comparecencia.",
     ["FALTA DE COMPARECENCIA"],
     ["FALTA DE COMPARECENCIA", "ABANDONO POR FALTA DE COMPARECENCIA"]),
    ("Art. 307, prescripción tributaria.", [], ["ART. 307", "PRESCRIPCION TRIBUTARIA"]),
    ("Art. 147, inadmisión de la demanda.",
     # "INADMITIR DEMANDA Y/O SOLICITUD" (14305-2016-00497). No "INADMITIR"
     # a secas: "INADMITIR RECURSO DE APELACION" no cierra la causa.
     ["INADMISION DE LA DEMANDA", "INADMISION DE DEMANDA", "INADMISION", "INADMITIR DEMANDA"],
     ["INADMISION DE LA DEMANDA", "INADMISION"]),
    ("Art. 361, pago de la deuda.",
     # "SOLUCION O PAGO Y/O EXTINCION DE LA OBLIGACION (AUTO INTERLOCUTORIO)":
     # así cierra civil el cobro pagado (07308-2023-00425).
     ["PAGO DE LA DEUDA", "PAGO PREVIO", "SOLUCION O PAGO"],
     ["PAGO DE LA DEUDA", "PAGO PREVIO", "ART. 361"]),
    # Archivo sin motivo en el nombre (11337-2024-00002, 12312-2019-00097). Al
    # final: un motivo con nombre propio gana, y una sentencia también (va antes).
    ("Archivo del proceso.", ["ARCHIVO DEL PROCESO"], []),
    ("Con sentencia.", ["SENTENCIA (RESOLUCION)"], []),
]


# Tipos con los que algunos juzgados registran la sentencia sin la palabra
# "SENTENCIA" (portado de tributaria, verificado allí contra SATJE).
# Comparación EXACTA, nunca "contiene (RESOLUCION)": aceptar cualquier
# resolución metía autos como si fueran sentencias.
#   "ACEPTAR DEMANDA" — civil 13333-2016-00201: es la sentencia del cobro de
#       la letra de cambio, y la causa quedaba "en trámite".
#   "(RESOLUCION)" a secas — también se usa para aprobar una conciliación, así
#       que no cuenta como sentencia explícita (ver _es_sentencia_explicita).
TIPOS_SENTENCIA_EXACTOS = {
    "(RESOLUCION)",
    "ACEPTAR DEMANDA (RESOLUCION)",
    "NEGAR DEMANDA (RESOLUCION)",
    "ALLANAMIENTO TOTAL (RESOLUCION)",
}

# Motivos que no cierran la causa sino que la mandan a OTRO juzgado: si en
# SATJE aparece otro incidente de primera instancia con actividad posterior,
# la causa sigue viva allí (08304-2025-00136: inhibición, sigue en 08257).
MOTIVOS_TRASLADO = {
    "Art. 147, incompetencia del tribunal.",
    "Art. 153 # 1, incompetencia de la o del juzgador.",
}


def _norm(s: str) -> str:
    return (s or "").upper()\
        .replace("Á","A").replace("É","E").replace("Í","I")\
        .replace("Ó","O").replace("Ú","U").replace("Ñ","N")


MOTIVO_ARCHIVO_GENERICO = "Archivo del proceso."


def _motivo_especifico_anterior(acts: list, desde: dict):
    """El primer motivo con nombre propio en las actuaciones más viejas que
    `desde` (la lista viene de la más nueva a la más vieja)."""
    vio = False
    for a in acts:
        if a is desde:
            vio = True
            continue
        if not vio or _es_razon_notificacion(a.get("tipo", "")):
            continue
        m = clasificar_cogep(a.get("tipo", "").strip(), limpiar_html(a.get("actividad", "")))
        if m and m != MOTIVO_ARCHIVO_GENERICO:
            return m, a
    return None


def _es_sentencia_explicita(tipo: str) -> bool:
    """El juzgado la registró como sentencia, con nombre propio."""
    t = " ".join(_norm(tipo).split())
    if not t.endswith("(RESOLUCION)") or _es_razon_notificacion(tipo):
        return False
    return "SENTENCIA" in t or (t in TIPOS_SENTENCIA_EXACTOS and t != "(RESOLUCION)")


# Actuaciones que nunca cierran una causa, digan lo que digan:
#   ACTA DE SORTEO — registra que la causa LLEGA a un juzgado; repite el motivo
#       del traslado ("Inhibición por competencia") o el asunto ("Ejecución de
#       acta de transacción": 05333-2025-03781).
#   OFICIO — comunica; cita de memoria principios como "mediación, celeridad"
#       (24331-2024-01230, 13337-2023-01218).
#   CONVOCATORIA — "CONVOCATORIA AUDIENCIA DE CONCILIACION" no es una
#       conciliación (12331-2022-01212).
#   DERIVACION — "DERIVACION A MEDIACION" manda la causa a mediación; si hay
#       acuerdo, se registra aparte (04332-2021-00067).
TIPOS_NO_RESOLUTORIOS = ("ACTA DE SORTEO", "OFICIO", "CONVOCATORIA", "NUEVA CONVOCATORIA", "DERIVACION")


def _no_es_resolutoria(tipo: str) -> bool:
    return " ".join(_norm(tipo).split()).startswith(TIPOS_NO_RESOLUTORIOS)


def clasificar_cogep(tipo: str, actividad: str) -> str:
    """
    Devuelve la descripción COGEP del motivo de terminación o '' si no aplica.
    """
    t = " ".join(_norm(tipo).split())
    a = _norm(actividad)

    if _no_es_resolutoria(tipo):
        return ""
    if "SENTENCIA" in t and t.endswith("(RESOLUCION)"):
        return "Con sentencia."
    if t in TIPOS_SENTENCIA_EXACTOS:
        return "Con sentencia."

    # El texto libre solo cuenta en una RESOLUCION. En actas, oficios y autos
    # aparece de relleno: el acta de audiencia es un formulario que lista
    # todas las excepciones con "NO NO NO" (13336-2024-00100 quedaba
    # "falta de legitimación"; 09315-2023-00056, "conciliación").
    solo_tipo = not t.endswith("(RESOLUCION)")

    # El nombre de la actuación manda: en "CONCILIACION TOTAL (RESOLUCION)" el
    # texto menciona "transacción" o "inmediación", y antes ganaba el motivo
    # del catálogo que apareciera primero en la lista (04332-2021-00008/00059).
    for descripcion, kw_tipo, kw_act in COGEP:
        if any(k in t for k in kw_tipo):
            return descripcion
    if solo_tipo:
        return ""
    # En el texto, palabras completas: "inmediación" no es "mediación".
    for descripcion, kw_tipo, kw_act in COGEP:
        if any(re.search(r"\b" + re.escape(k) + r"\b", a) for k in kw_act):
            return descripcion
    return ""


def es_tribunal_primera_instancia(nombre: str) -> bool:
    """True si la judicatura es un órgano de primera instancia de la materia
    (ver PRIMERA_INSTANCIA_CLAVES en config/materia.py)."""
    return MATERIA.es_primera_instancia(nombre)


def es_incidente_primera_instancia(inc: dict) -> bool:
    """True si el incidente es de una judicatura de primera instancia de la materia.
    Manda el código de judicatura: SATJE no siempre nombra igual a la unidad
    (06102 Alausí llegaba sin "Multicompetente" y se descartaban sus causas)."""
    return (str(inc.get("idJudicatura", "")) in MATERIA.JUDICATURAS
            or es_tribunal_primera_instancia(inc.get("nombreJudicatura", "")))


# Largo mínimo del texto de una actuación para tratarlo como el documento
# (una sentencia o auto completo, no una razón o un decreto de una línea).
MIN_TEXTO_ACTUACION = 1500


def pdf_desde_texto(texto: str, titulo: str = "") -> bytes:
    """Arma un PDF A4 con el texto de una actuación de SATJE.

    En las causas del sistema anterior (en la práctica, 2016–2018) SATJE ya no
    conserva el PDF: el servicio de documentos responde vacío, pero el texto
    íntegro de la providencia sigue en el campo `actividad` de la actuación.
    """
    import html as _html
    import io
    import fitz  # pymupdf

    parrafos = [p.strip() for p in texto.replace("\r\n", "\n").split("\n")]
    cuerpo = "".join(f"<p>{_html.escape(p)}</p>" for p in parrafos if p)
    nota = ("Texto de la actuación tomado de SATJE: el sistema no conserva el PDF "
            "original de esta providencia.")
    cabecera = f"<h4>{_html.escape(titulo)}</h4>" if titulo else ""
    doc_html = f"{cabecera}<p class='nota'>{_html.escape(nota)}</p>{cuerpo}"
    css = ("* {font-family: serif; font-size: 11pt;} p {text-align: justify; margin: 0 0 6pt 0;} "
           "h4 {font-size: 12pt;} .nota {font-size: 8pt; color: #555;}")
    buf = io.BytesIO()
    story = fitz.Story(html=doc_html, user_css=css)
    writer = fitz.DocumentWriter(buf)
    pagina = fitz.paper_rect("a4")
    area = pagina + (56, 56, -56, -56)
    mas = 1
    while mas:
        dev = writer.begin_page(pagina)
        mas, _ = story.place(area)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def _texto_de_actuacion(act: dict) -> str:
    texto = (act.get("actividad") or "").strip()
    return texto if len(texto) >= MIN_TEXTO_ACTUACION else ""


def descargar_doc_actuacion(act: dict, id_juicio: str, prefijo: str) -> tuple:
    """
    Descarga el PDF de una actuación del SATJE.
    Retorna (bytes, nombre_archivo) o (None, '').

    Si la actuación es del sistema anterior (uuid numérico) y SATJE ya no tiene
    el PDF, arma el PDF con el texto íntegro de la actuación.
    """
    pdf, nombre = _descargar_doc_actuacion_pdf(act, id_juicio, prefijo)
    if pdf:
        return pdf, nombre
    uuid = str(act.get("uuid") or "")
    texto = _texto_de_actuacion(act)
    if texto and uuid.isdigit():
        titulo = f"{id_juicio} — {(act.get('tipo') or '').strip()} — {(act.get('fecha') or '')[:10]}"
        try:
            return pdf_desde_texto(texto, titulo), f"{id_juicio}_{prefijo}_texto_satje.pdf"
        except Exception as e:
            log.warn(f"No se pudo armar el PDF desde el texto de SATJE: {e}", causa=id_juicio)
    return None, ""


def _descargar_doc_actuacion_pdf(act: dict, id_juicio: str, prefijo: str) -> tuple:
    if act.get("ieDocumentoAdjunto") != "S":
        return None, ""

    if act.get("ieTablaReferencia", "") == "EscritosPrimera":
        for anexo in obtener_anexos(act):
            uuid = anexo.get("UUID") or anexo.get("uuid", "")
            if not uuid:
                continue
            nombre = anexo.get("nombreArchivo") or anexo.get("descripcion") or prefijo
            pdf = descargar_pdf(uuid)
            if pdf:
                nombre_final = f"{id_juicio}_{nombre}"
                if not nombre_final.lower().endswith(".pdf"):
                    nombre_final += ".pdf"
                return pdf, nombre_final
    else:
        uuid = act.get("uuid", "")
        if not uuid:
            return None, ""
        nombre = act.get("nombreArchivo", prefijo) or prefijo
        pdf = descargar_pdf(uuid)
        if pdf:
            nombre_final = f"{id_juicio}_{nombre}"
            if not nombre_final.lower().endswith(".pdf"):
                nombre_final += ".pdf"
            return pdf, nombre_final

    return None, ""


def determinar_estado(incidentes: list, resultado_tribunal: str) -> str:
    """
    Determina el estado de juicio general (etiquetas en config/materia.py):
    - ESTADO_EN_CNJ        si hay incidente en la Corte Nacional
    - ESTADO_EN_APELACION  si hay incidente en la Corte Provincial (materias con apelación)
    - 'Archivado'          si hay resolución terminal
    - ESTADO_EN_PRIMERA    si aún está en trámite
    """
    from config.config import RESULTADO_EN_TRAMITE, RESULTADO_SIN_ACTUACIONES

    tiene_cnj = any(i.get("idJudicatura", "") == ID_CNJ for i in incidentes)
    if tiene_cnj:
        return MATERIA.ESTADO_EN_CNJ
    if MATERIA.tiene_apelacion() and any(
        MATERIA.es_apelacion(i.get("nombreJudicatura", "")) for i in incidentes
    ):
        return MATERIA.ESTADO_EN_APELACION
    if not resultado_tribunal:
        return MATERIA.ESTADO_EN_PRIMERA
    if resultado_tribunal in (RESULTADO_EN_TRAMITE, RESULTADO_SIN_ACTUACIONES):
        return MATERIA.ESTADO_EN_PRIMERA
    return "Archivado"


def buscar_actuacion_resolucion(
    id_juicio: str,
    incidentes: list,
    tribunal_id: str,
    job_id: int = None,
    causa: str = None,
) -> tuple:
    """
    Recorre los incidentes de primera instancia del SATJE y devuelve la
    primera actuación que representa una resolución terminal (COGEP).

    Retorna:
      (resultado_tribunal: str, actuacion: dict|None, actuaciones: list)
      - resultado_tribunal: descripción COGEP o RESULTADO_EN_TRAMITE si no hay resolución
      - actuacion:          la actuación que generó la resolución, o None
      - actuaciones:        lista completa de actuaciones del incidente principal
    """
    from config.config import RESULTADO_EN_TRAMITE, RESULTADO_SIN_ACTUACIONES

    incs_primera = [
        i for i in incidentes
        if es_incidente_primera_instancia(i)
    ]
    incs_primera.sort(key=lambda i: 0 if i.get("idJudicatura") == tribunal_id else 1)

    if not incs_primera:
        nombres = "; ".join(f'{i.get("idJudicatura", "?")} {i.get("nombreJudicatura", "")}' for i in incidentes)
        log.info(f"Sin incidente de primera instancia de la materia (incidentes: {nombres or 'ninguno'})",
                 job_id=job_id, causa=causa)
        return RESULTADO_SIN_ACTUACIONES, None, []

    acts_por_inc = [(inc, obtener_actuaciones(id_juicio, inc)) for inc in incs_primera]

    # 1) Una sentencia registrada como tal, en CUALQUIER juzgado de la causa,
    # manda sobre coincidencias por texto (un oficio o archivo posterior que
    # menciona "cosa juzgada" o "incompetencia" le ganaba a la sentencia).
    for inc, acts in acts_por_inc:
        for act in acts:
            if _es_sentencia_explicita(act.get("tipo", "")):
                return "Con sentencia.", act, acts

    def _ultima_fecha(acts):
        return max((a.get("fecha") or "" for a in acts), default="")

    # 2) Motivo COGEP por juzgado. Si el motivo es un traslado (incompetencia,
    # inhibición) y otro juzgado de primera instancia tiene actividad posterior,
    # la causa siguió allí: ese motivo no la cierra.
    for inc, acts in acts_por_inc:
        for act in acts:
            if _es_razon_notificacion(act.get("tipo", "")):
                continue
            tipo      = act.get("tipo", "").strip()
            actividad = limpiar_html(act.get("actividad", ""))
            motivo    = clasificar_cogep(tipo, actividad)
            if not motivo:
                continue
            if motivo == MOTIVO_ARCHIVO_GENERICO:
                # El archivo sin motivo suele ser el cierre administrativo
                # DESPUÉS del motivo real (04332-2021-00059: conciliación total
                # en 2022, archivo en 2025; 17230-2022-03344: excepción de
                # incompetencia y luego archivo). Si hay un motivo con nombre
                # propio más atrás en el mismo juzgado, ese es el que cuenta.
                previo = _motivo_especifico_anterior(acts, act)
                if previo:
                    motivo, act = previo
            if motivo in MOTIVOS_TRASLADO and any(
                otro is not inc and _ultima_fecha(otros) > (act.get("fecha") or "")
                for otro, otros in acts_por_inc
            ):
                log.info(
                    f"Traslado de juzgado ({motivo}) en {inc.get('idJudicatura', '')}: la causa sigue en otro",
                    job_id=job_id, causa=causa,
                )
                break
            if inc.get("idJudicatura") != tribunal_id:
                log.info(
                    f"Resolución encontrada en: {inc.get('nombreJudicatura', inc.get('idJudicatura', ''))}",
                    job_id=job_id, causa=causa,
                )
            return motivo, act, acts

    # En trámite: las actuaciones del juzgado donde está la causa hoy (el de
    # actividad más reciente), no las del que la remitió.
    actuaciones = max((acts for _, acts in acts_por_inc), key=_ultima_fecha, default=[])
    resultado_tribunal = RESULTADO_EN_TRAMITE if actuaciones else RESULTADO_SIN_ACTUACIONES
    act_resolucion = None

    return resultado_tribunal, act_resolucion, actuaciones


def calcular_duracion(act_resolucion: dict, actuaciones: list) -> dict:
    """
    Calcula la duración del caso en días, de la actuación más antigua a la sentencia.
    Retorna dict con _fecha_inicio_caso, _fecha_sentencia_caso, _duracion_dias (o vacío).
    """
    if not act_resolucion or not actuaciones:
        return {}

    fecha_sentencia = _fecha_iso(act_resolucion.get("fecha", ""))
    actas_validas = [a for a in actuaciones if _fecha_iso(a.get("fecha", ""))]
    if not actas_validas or not fecha_sentencia:
        return {}

    fecha_inicio = _fecha_iso(actas_validas[-1].get("fecha", ""))
    if not fecha_inicio:
        return {}

    return {
        "_fecha_inicio_caso":   actas_validas[-1]["fecha"][:10],
        "_fecha_sentencia_caso": act_resolucion["fecha"][:10],
        "_duracion_dias":        max(0, (fecha_sentencia.date() - fecha_inicio.date()).days),
    }


def obtener_escritos_pdf(
    id_juicio: str,
    incidentes: list,
    max_escritos: int = 3,
    job_id: int = None,
    causa: str = None,
) -> list:
    """
    Descarga hasta max_escritos PDFs de escritos (ieTablaReferencia
    'EscritosPrimera'), del más antiguo al más reciente — candidatos a
    ser la demanda/contestación, donde suele constar la firma del
    abogado patrocinador (usado como fallback cuando la sentencia no
    lo trae). La mayoría son PDFs escaneados sin capa de texto — eso lo
    maneja el caller (modo visión), acá solo se bajan los bytes.
    """
    candidatos = []
    for inc in incidentes:
        for a in obtener_actuaciones(id_juicio, inc):
            if (
                a.get("ieTablaReferencia") == "EscritosPrimera"
                and "ESCRITO" in a.get("tipo", "").upper()
                and a.get("ieDocumentoAdjunto") == "S"
            ):
                candidatos.append(a)
    candidatos.sort(key=lambda a: a.get("fecha", ""))

    pdfs = []
    for a in candidatos[:max_escritos]:
        pdf = None
        for anexo in obtener_anexos(a):
            uuid = anexo.get("UUID") or anexo.get("uuid", "")
            if not uuid:
                continue
            pdf = descargar_pdf(uuid)
            if pdf:
                break
        if pdf:
            pdfs.append(pdf)
        else:
            log.info("Escrito sin PDF descargable, se omite", job_id=job_id, causa=causa)
    return pdfs
