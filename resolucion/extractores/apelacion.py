"""
Extractor de APELACIÓN (Corte Provincial de Justicia).

extraer(pdf_bytes, numero, modelo, job_id, carpeta_pdfs, carpeta_docx) → dict

Recibe el PDF ya descargado, corre el análisis IA y devuelve el dict
de campos de apelación más los bytes del Word generado.

No sabe nada de SATJE ni de Strapi — esa lógica vive en satje/ y cms/.
"""

from pathlib import Path

from resolucion.ai import analizar_apelacion
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
    Ejecuta el análisis IA sobre un PDF de sentencia de apelación (Corte Provincial).

    Retorna dict con campos de apelación más:
      _pdf_bytes_apel, _pdf_nombre_apel
      _docx_bytes_apel, _docx_nombre_apel
      _resumen_apel, _extraccion_apel, _jurisprudencia_apel
      _categorizacion_apel, _tipos_defecto_apel (prompts 4 y 7)
      Completitud Apelación, error (str vacío si OK)
    """
    from config.config import COMPLETITUD_COMPLETO, COMPLETITUD_PARCIAL, COMPLETITUD_ERROR_GPT

    nombre_pdf_apel = f"{numero}_sentencia_apelacion.pdf"

    resultado = {
        "error":              "",
        "Completitud Apelación":    "",
        "_resumen_apel":       "",
        "_extraccion_apel":    "",
        "_jurisprudencia_apel": "",
        "_pdf_bytes_apel":     pdf_bytes,
        "_pdf_nombre_apel":    nombre_pdf_apel,
        "_docx_bytes_apel":    None,
        "_docx_nombre_apel":   "",
    }

    # Guardar PDF localmente
    if carpeta_pdfs and pdf_bytes:
        try:
            carpeta_pdfs.mkdir(parents=True, exist_ok=True)
            (carpeta_pdfs / nombre_pdf_apel).write_bytes(pdf_bytes)
        except Exception as e:
            log.warn(f"No se pudo guardar PDF apelación local: {e}", job_id=job_id, causa=numero)

    # Llamar al análisis IA
    gpt = analizar_apelacion(pdf_bytes, numero, modelo=modelo, job_id=job_id,
                            flujo_proveedores=flujo_proveedores)

    if gpt.get("error"):
        resultado["error"]           = gpt["error"]
        resultado["Completitud Apelación"] = COMPLETITUD_ERROR_GPT
        log.error(f"Extractor apelación: {gpt['error']}", job_id=job_id, causa=numero)
        return resultado

    resultado["_resumen_apel"]       = gpt.get("resumen", "")
    resultado["_extraccion_apel"]    = gpt.get("extraccion", "")
    resultado["_jurisprudencia_apel"] = gpt.get("jurisprudencia", "")
    # Prompts 4 y 7 sobre el recurso: categorías del catálogo y tipos de defecto
    # (puntos controvertidos). Solo en éxito: si faltan, el guardado no toca las
    # categorías que ya tenga el registro.
    resultado["_categorizacion_apel"]  = gpt.get("categorias", "")
    resultado["_tipos_defecto_apel"]   = gpt.get("tipos_defecto", "")

    # Word generado
    docx_bytes = gpt.get("docx_bytes")
    if docx_bytes:
        nombre_docx = f"{numero}_ficha_tecnica_apelacion.docx"
        resultado["_docx_bytes_apel"]  = docx_bytes
        resultado["_docx_nombre_apel"] = nombre_docx
        if carpeta_docx:
            try:
                carpeta_docx.mkdir(parents=True, exist_ok=True)
                (carpeta_docx / nombre_docx).write_bytes(docx_bytes)
                log.info(f"Word apelación guardado: {nombre_docx}", job_id=job_id, causa=numero)
            except Exception as e:
                log.warn(f"No se pudo guardar Word apelación local: {e}", job_id=job_id, causa=numero)

    # Completitud
    if gpt.get("validacion_ok"):
        resultado["Completitud Apelación"] = COMPLETITUD_COMPLETO
    else:
        resultado["Completitud Apelación"] = COMPLETITUD_PARCIAL
        problemas = gpt.get("validacion_problemas", [])
        if problemas:
            resultado["_obs_apel"] = "; ".join(problemas)[:400]

    return resultado
