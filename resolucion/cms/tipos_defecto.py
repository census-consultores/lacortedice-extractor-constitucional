"""
Tipos de defecto (prompt 7 → puntos controvertidos) de la apelación y la
casación, en la tabla interna `causa_tipos_defecto` con el documentId del
registro de esa instancia (el tribunal usa el de la causa).

Lo llaman cms/apelacion.py y cms/corte.py tras guardar en Strapi, así que
aplica igual al runner, la lista y la re-extracción.
"""

from worker import logger as log


def guardar_tipos_defecto_recurso(fila: dict, resultado: dict | None, clave: str,
                                  job_id: int = None) -> None:
    """Best-effort: un fallo de la tabla interna no tumba el guardado."""
    texto = fila.get(clave)
    doc_id = ((resultado or {}).get("data") or {}).get("documentId", "")
    if not texto or not doc_id:
        return
    try:
        from worker import job_manager as jm
        jm.guardar_tipos_defecto(doc_id, texto)
    except Exception as e:
        log.error(f"Tipos de defecto del recurso (interno) — {type(e).__name__}: {e}",
                  job_id=job_id, causa=fila.get("Número de juicio", "?"))
