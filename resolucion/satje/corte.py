"""
Scraping de SATJE para sentencias de CASACIÓN (Corte Nacional de Justicia).

detectar_sentencia_cnj(actuaciones) → (descripcion, actuacion)

Detector dedicado para la Corte Nacional que ignora providencias, oficios
y AEPs (Acciones Extraordinarias de Protección) y solo reporta resoluciones
sustantivas de casación.
"""

from api import obtener_actuaciones, _es_razon_notificacion
from worker import logger as log
from config.config import ID_CNJ

# ── Tipos excluidos del análisis (no son resoluciones de casación) ─────────────
_EXCLUYE = [
    "ACTA DE SORTEO",
    "AUTO DE SUSTANCIACION",
    "COMPLETAR O ACLARAR",
    "ACCION EXTRAORDINARIA",   # AEP a Corte Constitucional, no es casación
    "ESCRITO",
    "OFICIO",
]

# ── Mapa tipo de actuación → descripción oficial ──────────────────────────────
_RESOLUCIONES = [
    ("NO CASAR SENTENCIA Y/O AUTO INTERLOCUTORIO (RESOLUCION)", "No casa la sentencia."),
    ("CASAR PARCIALMENTE LA SENTENCIA (RESOLUCION)",             "Casa parcialmente la sentencia."),
    ("SENTENCIA CASANDO SENTENCIA Y/O AUTO INTERLOCUTORIO (RESOLUCION)", "Casa la sentencia."),
    ("INADMISIBILIDAD DE RECURSO DE CASACION",                  "Art. 270, inadmisión."),
    ("IMPROCEDENTE",  "Art. 266, improcedente por extemporáneo (Tribunal)."),
    ("IMPROCEDENCIA", "Art. 266, improcedente por extemporáneo (Tribunal)."),
    ("DESISTIMIENTO DEL RECURSO",  "Art. 238, desistimiento."),
    ("ABANDONO",                   "Art. 245, abandono."),
    ("ADMISIBILIDAD DE RECURSO DE CASACION", "Art. 270, admisión."),
]

# Descripciones que indican resolución con PDF (casa / no casa / parcialmente)
DESCRIPCIONES_SUBSTANTIVAS = {
    "No casa la sentencia.",
    "Casa parcialmente la sentencia.",
    "Casa la sentencia.",
}


# Resoluciones de la CNJ que TERMINAN el recurso sin pronunciarse sobre el
# fondo (el conjuez inadmite, el recurso es improcedente, o la parte lo
# desiste o abandona). No son "sustantivas" (no hay sentencia que analizar con
# el prompt 5), pero sí cierran la casación: se guarda la ficha de casación con
# el PDF del auto y una decisión propia del catálogo, en vez de dejar la causa
# como si siguiera "En Corte Nacional". Clave = descripción de _RESOLUCIONES,
# valor = nombre de la decisión en el catálogo decisiones-finales-casacion.
DESCRIPCIONES_TERMINALES_SIN_FONDO = {
    "Art. 270, inadmisión.":                              "Inadmite el recurso de casación.",
    "Art. 266, improcedente por extemporáneo (Tribunal).": "Improcedente el recurso de casación.",
    "Art. 238, desistimiento.":                           "Desistimiento del recurso de casación.",
    "Art. 245, abandono.":                                "Abandono del recurso de casación.",
}


def _norm(s: str) -> str:
    return (s or "").upper()\
        .replace("Á","A").replace("É","E").replace("Í","I")\
        .replace("Ó","O").replace("Ú","U").replace("Ñ","N")


def detectar_sentencia_cnj(actuaciones: list) -> tuple:
    """
    Recorre las actuaciones de un incidente CNJ y devuelve la primera resolución.
    Las actuaciones vienen DESC por fecha, así que la primera que matchea
    es la resolución vigente.

    Retorna:
      (descripcion, actuacion) o ("", None) si no hay resolución.
    """
    for act in actuaciones:
        tipo = (act.get("tipo") or "").strip()
        if _es_razon_notificacion(tipo):
            continue
        t = _norm(tipo)
        if any(ex in t for ex in _EXCLUYE):
            continue
        for kw, desc in _RESOLUCIONES:
            if kw in t:
                return desc, act
    return "", None


def obtener_actuaciones_cnj(id_juicio: str, inc_cnj: dict) -> list:
    """Descarga las actuaciones del incidente CNJ dado."""
    return obtener_actuaciones(id_juicio, inc_cnj)


def encontrar_incidente_cnj(incidentes: list) -> dict | None:
    """Retorna el incidente de la Corte Nacional de la lista, o None."""
    return next(
        (i for i in incidentes if i.get("idJudicatura") == ID_CNJ),
        None,
    )
