"""
Subida a CMS de sentencias de APELACIÓN (Corte Provincial de Justicia).

guardar(fila, causa_doc_id, strapi, job_id) → dict | None

Delega en StrapiClient.guardar_apelacion() y reporta via logger.
Solo se invoca en materias con tres niveles (ver config/materia.py).
"""

from worker import logger as log
from resolucion.cms.tipos_defecto import guardar_tipos_defecto_recurso


def guardar(fila: dict, causa_doc_id: str, strapi, job_id: int = None) -> dict | None:
    """
    Persiste la ficha de apelación en Strapi (content-type `apelaciones`),
    vinculada a la causa.
    """
    numero = fila.get("Número de juicio", "?")
    log.info("CMS apelación: guardando sentencia de segunda instancia", job_id=job_id, causa=numero)
    resultado = strapi.guardar_apelacion(fila, causa_doc_id)
    guardar_tipos_defecto_recurso(fila, resultado, "_tipos_defecto_apel", job_id)
    log.info("CMS apelación: guardada", job_id=job_id, causa=numero)
    return resultado
