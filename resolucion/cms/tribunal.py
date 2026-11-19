"""
Subida a CMS de sentencias de PRIMERA INSTANCIA.

guardar(fila, strapi, job_id) → dict | None

Delega en StrapiClient.guardar_causa() y reporta el resultado via logger.
"""

from worker import logger as log


def guardar(fila: dict, strapi, job_id: int = None) -> dict | None:
    """
    Persiste la ficha de primera instancia en Strapi.

    Parámetros:
      fila   — dict completo de la causa (incluye _url_pdf_tribunal, etc.)
      strapi — instancia de StrapiClient ya inicializada
      job_id — para logs

    Retorna la respuesta de Strapi o None si falla (la excepción se propaga).
    """
    numero = fila.get("Número de juicio", "?")
    log.info("CMS tribunal: guardando causa", job_id=job_id, causa=numero)
    resultado = strapi.guardar_causa(fila)
    log.info("CMS tribunal: causa guardada", job_id=job_id, causa=numero)
    return resultado
