"""
Subida a CMS de sentencias de CASACIÓN (Corte Nacional de Justicia).

guardar(fila, causa_doc_id, strapi, job_id) → dict | None

Delega en StrapiClient.guardar_casacion() y reporta via logger.
"""

from worker import logger as log
from resolucion.cms.tipos_defecto import guardar_tipos_defecto_recurso


def guardar(fila: dict, causa_doc_id: str, strapi, job_id: int = None) -> dict | None:
    """
    Persiste la ficha de casación en Strapi, vinculada a la causa.

    Parámetros:
      fila          — dict completo de la causa (incluye _resumen_cnj, _extraccion_cnj, etc.)
      causa_doc_id  — documentId de la causa en Strapi (obtenido al guardar el tribunal)
      strapi        — instancia de StrapiClient ya inicializada
      job_id        — para logs

    Retorna la respuesta de Strapi o None si falla (la excepción se propaga).
    """
    numero = fila.get("Número de juicio", "?")
    log.info("CMS corte: guardando casación", job_id=job_id, causa=numero)
    resultado = strapi.guardar_casacion(fila, causa_doc_id)
    guardar_tipos_defecto_recurso(fila, resultado, "_tipos_defecto_cnj", job_id)
    log.info("CMS corte: casación guardada", job_id=job_id, causa=numero)
    return resultado
