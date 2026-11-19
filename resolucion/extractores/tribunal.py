"""
Extractor de PRIMERA INSTANCIA.

extraer(numero, pdf_bytes, demandado, modelo, job_id, config) → dict

Recibe el PDF ya descargado, corre el análisis IA y devuelve el dict
de campos (JSON) más los bytes del Word generado.

No sabe nada de SATJE ni de Strapi — esa lógica vive en satje/ y cms/.
"""

from pathlib import Path

from resolucion.ai import analizar_sentencia
from worker import logger as log


def extraer(
    numero: str,
    pdf_bytes: bytes,
    demandado: str = "",
    modelo: str = "gpt-4o",
    job_id: int = None,
    nombre_tribunal: str = "",
    ano: str = "",
    carpeta_pdfs: Path = None,
    carpeta_docx: Path = None,
    flujo_proveedores: str = "alibaba",
) -> dict:
    """
    Ejecuta el análisis IA sobre un PDF de sentencia de tribunal.

    Parámetros:
      numero         — número de juicio ("09501-2019-00010")
      pdf_bytes      — contenido del PDF
      demandado      — nombre del demandado (para detectar SRI/SENAE/GAD)
      modelo         — modelo de IA a usar
      job_id         — id del job para logs
      nombre_tribunal — para el encabezado del Word
      ano            — para el encabezado del Word
      carpeta_pdfs   — si se pasa, guarda el PDF localmente
      carpeta_docx   — si se pasa, guarda el Word localmente

    Retorna dict con todos los campos de la ficha técnica más:
      _pdf_bytes_tribunal, _pdf_nombre_tribunal
      _docx_bytes_tribunal, _docx_nombre_tribunal
      _extraccion, _jurisprudencia
      error (str vacío si OK)
    """
    resultado = {
        "error": "",
        # Campos de auditoría
        "Cuantía":             "",
        "Tipo de Acción":      "",
        "Objeto Controversia": "",
        "Decisión Final":      "",
        "Resumen Ejecutivo":   "",
        "Categorización":      "",
        "Abogado del Actor":     "",
        "Abogado del Demandado": "",
        "Completitud":         "",
        "Observaciones":       "",
        # Internos para Strapi y archivos
        "_extraccion":              "",
        "_jurisprudencia":          "",
        "_tipos_defecto":           "",
        "_evidencia_ausente":       "",
        "_pdf_bytes_tribunal":      pdf_bytes,
        "_pdf_nombre_tribunal":     f"{numero}_sentencia.pdf",
        "_docx_bytes_tribunal":     None,
        "_docx_nombre_tribunal":    "",
        "_fecha_emision":           "",
        "_administracion":          "",
    }

    # Guardar PDF localmente
    if carpeta_pdfs and pdf_bytes:
        try:
            carpeta_pdfs.mkdir(parents=True, exist_ok=True)
            (carpeta_pdfs / f"{numero}_sentencia.pdf").write_bytes(pdf_bytes)
        except Exception as e:
            log.warn(f"No se pudo guardar PDF local: {e}", job_id=job_id, causa=numero)

    # Llamar al análisis IA
    gpt = analizar_sentencia(
        pdf_bytes, numero,
        demandado=demandado,
        modelo=modelo,
        job_id=job_id,
        nombre_tribunal=nombre_tribunal,
        ano=ano,
        flujo_proveedores=flujo_proveedores,
    )

    if gpt.get("error"):
        resultado["error"]      = gpt["error"]
        resultado["Completitud"] = "Error GPT"
        resultado["Observaciones"] = f"IA: {gpt['error']}"[:400]
        log.error(f"Extractor tribunal: {gpt['error']}", job_id=job_id, causa=numero)
        return resultado

    from config.config import COMPLETITUD_COMPLETO, COMPLETITUD_PARCIAL, COMPLETITUD_ERROR_GPT

    resultado["Cuantía"]             = gpt.get("cuantia", "")
    resultado["Tipo de Acción"]      = gpt.get("tipo_accion", "")
    resultado["Objeto Controversia"] = gpt.get("objeto_controversia", "")
    resultado["Decisión Final"]      = gpt.get("decision_final", "")
    resultado["Resumen Ejecutivo"]   = gpt.get("resumen", "")
    resultado["Categorización"]      = gpt.get("categorias", "")
    resultado["Abogado del Actor"]     = gpt.get("abogado_actor", "")
    resultado["Abogado del Demandado"] = gpt.get("abogado_demandado", "")
    resultado["_extraccion"]         = gpt.get("extraccion", "")
    resultado["_jurisprudencia"]     = gpt.get("jurisprudencia", "")
    resultado["_tipos_defecto"]      = gpt.get("tipos_defecto", "")
    resultado["_evidencia_ausente"]  = gpt.get("evidencia_ausente", "")
    resultado["_fecha_emision"]      = gpt.get("fecha_emision", "")
    resultado["_administracion"]     = gpt.get("administracion", "")
    resultado["_auditoria"]          = gpt.get("_auditoria", {})

    # Campos de auditoría para la fila Excel/BD
    resultado["Origen Cuantía"]  = gpt.get("cuantia_origen", "")
    resultado["Fecha Emisión"]   = gpt.get("fecha_emision", "")
    resultado["PDF Truncado"]    = "Sí" if gpt.get("pdf_truncado") else "No"
    resultado["Modo Extracción"] = gpt.get("modo", "")

    # Word generado
    docx_bytes = gpt.get("docx_bytes")
    if docx_bytes:
        nombre_docx = f"{numero}_ficha_tecnica.docx"
        resultado["_docx_bytes_tribunal"]  = docx_bytes
        resultado["_docx_nombre_tribunal"] = nombre_docx
        if carpeta_docx:
            try:
                carpeta_docx.mkdir(parents=True, exist_ok=True)
                (carpeta_docx / nombre_docx).write_bytes(docx_bytes)
                log.info(f"Word guardado: {nombre_docx}", job_id=job_id, causa=numero)
            except Exception as e:
                log.warn(f"No se pudo guardar Word local: {e}", job_id=job_id, causa=numero)

    # Completitud
    if gpt.get("validacion_ok"):
        resultado["Completitud"] = COMPLETITUD_COMPLETO
    else:
        resultado["Completitud"] = COMPLETITUD_PARCIAL
        problemas = gpt.get("validacion_problemas", [])
        if problemas:
            resultado["Observaciones"] = "; ".join(problemas)[:400]

    return resultado
