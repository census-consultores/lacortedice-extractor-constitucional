"""
Scraping de SATJE para sentencias de APELACIÓN (Corte Provincial de Justicia).

Solo aplica a materias con tres niveles (laboral, civil…): primera instancia
→ Sala de la Corte Provincial → casación en la Corte Nacional. En tributario
no existe este nivel y config/materia.py deja APELACION_CLAVES vacío, con lo
que encontrar_incidente_apelacion() siempre devuelve None.

encontrar_incidente_apelacion(incidentes) → dict | None
detectar_sentencia_apelacion(actuaciones) → (descripcion, actuacion)
"""

from api import obtener_actuaciones, _es_razon_notificacion
from config import materia as MATERIA

# ── Tipos excluidos (no son la resolución de la apelación) ────────────────────
_EXCLUYE = [
    "ACTA DE SORTEO",
    "AUTO DE SUSTANCIACION",
    "COMPLETAR O ACLARAR",
    "ACLARACION",
    "AMPLIACION",
    "ACCION EXTRAORDINARIA",   # AEP a la Corte Constitucional
    "ESCRITO",
    "OFICIO",
    "RAZON",
    "CONVOCATORIA",
    "AUDIENCIA",
    "CALIFICACION DE RECURSO",
    "CONCESION DE RECURSO",
    "RECURSO DE CASACION",     # la providencia que concede/califica la casación
    "CONFLICTO DE COMPETENCIA",  # la Sala dirime qué juzgado conoce; no es la apelación (14305-2016-00198)
]

# ── Tipos de actuación que SÍ son la resolución de segunda instancia ─────────
# El orden importa: de más específico a más general. La descripción final
# (Confirma / Revoca / Reforma / Nulidad) la fija la IA (prompt 11, sección
# 11) porque SATJE no siempre lo refleja en el tipo de actuación; aquí solo
# se distingue "hay sentencia de apelación" de "terminó por otra vía".
CON_SENTENCIA = "Con sentencia de apelación."

# Tipos reales vistos en SATJE (Sala Laboral de Guayas, causas 2022-2023,
# verificado 2026-10-14): "ACEPTAR RECURSO DE APELACION (RESOLUCION)",
# "RECHAZAR RECURSO DE APELACION (RESOLUCION)", "SENTENCIA (RESOLUCION)",
# "SENTENCIA Y/O RESOLUCION (RESOLUCION)", "RESOLUCION DE LA APELACION
# (ART. 147 COGEP) (RESOLUCION)", "CONCILIACION TOTAL (RESOLUCION)".
ACEPTA_APELACION  = "Acepta el recurso de apelación."
RECHAZA_APELACION = "Rechaza el recurso de apelación."

_RESOLUCIONES = [
    ("ACEPTAR RECURSO DE APELACION",           ACEPTA_APELACION),
    ("RECHAZAR RECURSO DE APELACION",          RECHAZA_APELACION),
    ("RESOLUCION DE LA APELACION",             CON_SENTENCIA),
    ("SENTENCIA Y/O RESOLUCION",               CON_SENTENCIA),
    # "RESOLUCIÓN (RESOLUCION)" a secas: así cuelgan algunas Salas la sentencia
    # de apelación ("VISTOS: El Tribunal de la Sala…"; 06336-2023-00097,
    # 02335-2021-00134 con voto salvado y luego casación). Va después de
    # "SENTENCIA Y/O RESOLUCION", que también la contiene.
    ("RESOLUCION (RESOLUCION)",                CON_SENTENCIA),
    ("CONCILIACION",                           "Conciliación en apelación."),
    ("SENTENCIA (RESOLUCION)",                 CON_SENTENCIA),
    ("SENTENCIA",                              CON_SENTENCIA),
    ("AUTO RESOLUTORIO",                       "Auto resolutorio en apelación."),
    ("AUTO INTERLOCUTORIO (RESOLUCION)",       "Auto resolutorio en apelación."),
    ("DESISTIMIENTO",                          "Art. 238, desistimiento del recurso."),
    ("ABANDONO",                               "Art. 245, abandono."),
    ("INADMISI",                               "Inadmisión del recurso de apelación."),
    ("DESERCION",                              "Deserción del recurso."),
    ("NULIDAD",                                "Declara la nulidad."),
]

# Descripciones que indican resolución sustantiva con PDF analizable
DESCRIPCIONES_SUBSTANTIVAS = {
    "Con sentencia de apelación.",
    ACEPTA_APELACION,
    RECHAZA_APELACION,
    "Auto resolutorio en apelación.",
}


def _norm(s: str) -> str:
    return (s or "").upper()\
        .replace("Á", "A").replace("É", "E").replace("Í", "I")\
        .replace("Ó", "O").replace("Ú", "U").replace("Ñ", "N")


def encontrar_incidente_apelacion(incidentes: list) -> dict | None:
    """Retorna el incidente de la Sala de la Corte Provincial, o None.

    Si hay varios (reenvíos, nulidades), se prefiere el más reciente según
    la fecha de su primera actuación disponible; a falta de fechas, el último
    de la lista (SATJE los ordena por creación)."""
    if not MATERIA.tiene_apelacion():
        return None
    candidatos = [
        i for i in incidentes
        if MATERIA.es_apelacion(i.get("nombreJudicatura", ""))
    ]
    if not candidatos:
        return None
    return candidatos[-1]


def detectar_sentencia_apelacion(actuaciones: list) -> tuple:
    """
    Recorre las actuaciones (DESC por fecha, como las entrega SATJE) y devuelve
    la primera que es resolución de la apelación.

    Retorna (descripcion, actuacion) o ("", None).
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


def obtener_actuaciones_apelacion(id_juicio: str, inc_apelacion: dict) -> list:
    """Descarga las actuaciones del incidente de la Corte Provincial."""
    return obtener_actuaciones(id_juicio, inc_apelacion)
