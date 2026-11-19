"""
Extractor de CASACIÓN (Corte Nacional de Justicia).

extraer(pdf_bytes, numero, modelo, job_id, carpeta_pdfs, carpeta_docx) → dict

Recibe el PDF ya descargado, corre el análisis IA y devuelve el dict
de campos CNJ más los bytes del Word generado.

No sabe nada de SATJE ni de Strapi — esa lógica vive en satje/ y cms/.
"""

from pathlib import Path

from resolucion.ai import analizar_casacion
from worker import logger as log


def extraer(
    pdf_bytes: bytes,
    numero: str,
    modelo: str = "gpt-4o",
    job_id: int = None,
    carpeta_pdfs: Path = None,
    carpeta_docx: Path = None,
    flujo_proveedores: str = "alibaba",
) -> dict:
    """
    Ejecuta el análisis IA sobre un PDF de sentencia de casación (CNJ).

    Retorna dict con campos CNJ más:
      _pdf_bytes_cnj, _pdf_nombre_cnj
      _docx_bytes_cnj, _docx_nombre_cnj
      _resumen_cnj, _extraccion_cnj, _jurisprudencia_cnj
      _categorizacion_cnj, _tipos_defecto_cnj (prompts 4 y 7)
      Completitud CNJ, error (str vacío si OK)
    """
    from config.config import COMPLETITUD_COMPLETO, COMPLETITUD_PARCIAL, COMPLETITUD_ERROR_GPT

    nombre_pdf_cnj = f"{numero}_sentencia_cnj.pdf"

    resultado = {
        "error":              "",
        "Completitud CNJ":    "",
        "_resumen_cnj":       "",
        "_extraccion_cnj":    "",
        "_jurisprudencia_cnj": "",
        "_pdf_bytes_cnj":     pdf_bytes,
        "_pdf_nombre_cnj":    nombre_pdf_cnj,
        "_docx_bytes_cnj":    None,
        "_docx_nombre_cnj":   "",
    }

    # Guardar PDF localmente
    if carpeta_pdfs and pdf_bytes:
        try:
            carpeta_pdfs.mkdir(parents=True, exist_ok=True)
            (carpeta_pdfs / nombre_pdf_cnj).write_bytes(pdf_bytes)
        except Exception as e:
            log.warn(f"No se pudo guardar PDF CNJ local: {e}", job_id=job_id, causa=numero)

    # Llamar al análisis IA
    gpt = analizar_casacion(pdf_bytes, numero, modelo=modelo, job_id=job_id,
                            flujo_proveedores=flujo_proveedores)

    if gpt.get("error"):
        resultado["error"]           = gpt["error"]
        resultado["Completitud CNJ"] = COMPLETITUD_ERROR_GPT
        log.error(f"Extractor corte: {gpt['error']}", job_id=job_id, causa=numero)
        return resultado

    resultado["_resumen_cnj"]       = gpt.get("resumen", "")
    resultado["_extraccion_cnj"]    = gpt.get("extraccion", "")
    resultado["_jurisprudencia_cnj"] = gpt.get("jurisprudencia", "")
    # Prompts 4 y 7 sobre el recurso: categorías del catálogo y tipos de defecto
    # (puntos controvertidos). Solo en éxito: si faltan, el guardado no toca las
    # categorías que ya tenga el registro.
    resultado["_categorizacion_cnj"]  = gpt.get("categorias", "")
    resultado["_tipos_defecto_cnj"]   = gpt.get("tipos_defecto", "")

    # Word generado
    docx_bytes = gpt.get("docx_bytes")
    if docx_bytes:
        nombre_docx = f"{numero}_ficha_tecnica_cnj.docx"
        resultado["_docx_bytes_cnj"]  = docx_bytes
        resultado["_docx_nombre_cnj"] = nombre_docx
        if carpeta_docx:
            try:
                carpeta_docx.mkdir(parents=True, exist_ok=True)
                (carpeta_docx / nombre_docx).write_bytes(docx_bytes)
                log.info(f"Word CNJ guardado: {nombre_docx}", job_id=job_id, causa=numero)
            except Exception as e:
                log.warn(f"No se pudo guardar Word CNJ local: {e}", job_id=job_id, causa=numero)

    # Completitud
    if gpt.get("validacion_ok"):
        resultado["Completitud CNJ"] = COMPLETITUD_COMPLETO
    else:
        resultado["Completitud CNJ"] = COMPLETITUD_PARCIAL
        problemas = gpt.get("validacion_problemas", [])
        if problemas:
            resultado["_obs_cnj"] = "; ".join(problemas)[:400]

    return resultado
