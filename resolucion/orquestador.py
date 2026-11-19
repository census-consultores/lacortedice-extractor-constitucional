"""
Orquestador de extracción de sentencias.

procesar_causa(numero, ctx) → dict

Punto de entrada único que une SATJE → IA → CMS.
Cada capa (satje/, extractores/, cms/) puede también usarse individualmente.

ctx es un dict con:
  tribunal       : str   — código del tribunal ("09501")
  nombre_tribunal: str   — nombre legible de la judicatura de primera instancia
  ano            : str   — año de la corrida ("2023")
  corrida_id     : str   — id de la corrida para trazabilidad
  carpeta_local  : Path  — carpeta local para PDFs
  carpeta_docx   : Path  — carpeta local para Words
  gpt_ok         : bool  — True si se debe llamar a la IA (False = dry-run)
  gpt_cnj_ok     : bool  — True si se debe llamar a la IA para CNJ
  skip_pdf       : bool  — True = no bajar PDF (solo clasificación COGEP)
  modelo         : str   — modelo de IA ("gpt-4o", "qwen-...", etc.)
  job_id         : int   — id del job para logs y SSE (None en modo local)
"""

import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api import (
    buscar_causa, obtener_incidentes,
    normalizar_numero, id_juicio_de,
)
from config.config import (
    ID_CNJ,
    RESULTADO_EN_TRAMITE, RESULTADO_SIN_ACTUACIONES,
    RESULTADO_CNJ_EN_TRAMITE, RESULTADO_APELACION_EN_TRAMITE,
    COMPLETITUD_COMPLETO, COMPLETITUD_PARCIAL,
    COMPLETITUD_PDF_SIN_GPT, COMPLETITUD_PENDIENTE,
    COMPLETITUD_SIN_ACTUS, COMPLETITUD_NO_APLICABLE,
    COMPLETITUD_ERROR_GPT, COMPLETITUD_ARCHIVADO,
)
from resolucion.satje.tribunal import (
    buscar_actuacion_resolucion, descargar_doc_actuacion,
    calcular_duracion, determinar_estado, obtener_escritos_pdf,
)
from resolucion.satje.corte import (
    encontrar_incidente_cnj, obtener_actuaciones_cnj,
    detectar_sentencia_cnj, DESCRIPCIONES_SUBSTANTIVAS, DESCRIPCIONES_TERMINALES_SIN_FONDO,
)
from resolucion.satje.apelacion import (
    encontrar_incidente_apelacion, obtener_actuaciones_apelacion,
    detectar_sentencia_apelacion,
    DESCRIPCIONES_SUBSTANTIVAS as DESCRIPCIONES_SUBSTANTIVAS_APEL,
)
from resolucion.extractores.tribunal import extraer as extraer_tribunal
from resolucion.extractores.corte import extraer as extraer_corte
from resolucion.extractores.apelacion import extraer as extraer_apelacion
from config import materia as MATERIA
from resolucion.ai.abogados_vision import completar_abogados_con_escritos
from worker import logger as log


# Marca de "PDF indicado en SATJE pero no se pudo descargar". Va al inicio de
# Observaciones (worker_caso_log la corta a 800 caracteres) para encontrar el
# grupo después. NO se reintenta en automático: la mayoría son fallas pasajeras
# (19-20 oct, con SATJE bloqueando: 27 de 30 descargan después), pero reintentar
# miles en plena barrida la frena. Al terminar se corre a mano una segunda vuelta:
#   SELECT DISTINCT numero FROM worker_caso_log WHERE observaciones LIKE '[PDF_FALLIDO%'
MARCA_PDF_FALLIDO = "[PDF_FALLIDO:{}]"


def _marcar_pdf_fallido(fila: dict, nivel: str) -> None:
    marca = MARCA_PDF_FALLIDO.format(nivel)
    obs = str(fila.get("Observaciones") or "")
    if marca not in obs:
        fila["Observaciones"] = f"{marca} {obs}".strip()


def procesar_causa(numero: str, ctx: dict) -> dict:
    """
    Pipeline completo para una causa: SATJE → IA → fila dict.

    No persiste en Strapi; el runner.py se encarga de eso para poder
    controlar los reintentos y el reporte de progreso.
    """
    tribunal        = ctx["tribunal"]
    nombre_tribunal = ctx.get("nombre_tribunal", tribunal)
    ano             = ctx.get("ano", "")
    corrida_id      = ctx.get("corrida_id", "")
    carpeta_local   = ctx.get("carpeta_local")
    carpeta_docx    = ctx.get("carpeta_docx")
    gpt_ok          = ctx.get("gpt_ok", True)
    gpt_cnj_ok      = ctx.get("gpt_cnj_ok", True)
    skip_pdf        = ctx.get("skip_pdf", False)
    modelo          = ctx.get("modelo", "gpt-4o")
    flujo_proveedores = ctx.get("flujo_proveedores", "alibaba")
    job_id          = ctx.get("job_id")


    fila = {
        "Corrida":                                corrida_id,
        "Completitud":                            "",
        "Completitud CNJ":                        "",
        "Año":                                    ano,
        "Número de juicio":                       numero,
        "Tribunal":                               nombre_tribunal,
        "_tribunal_codigo":                       tribunal,
        "Estado de Juicio":                       "Sin resultados",
        "Sentencia Tribunal":                     "",
        "Análisis IA - Sentencias Tribunal":      "",
        "Resultado tribunal":                     "",
        "Sentencia Corte Nacional":               "",
        "Análisis IA - Sentencia Corte Nacional": "",
        "Resultado en Corte":                     "",
        "Sentencia Apelación":                    "",
        "Análisis IA - Sentencia Apelación":      "",
        "Resultado en Apelación":                 "",
        "Completitud Apelación":                  "",
        "Categorización":                         "",
        "Acción Extraordinaria de Protección (AEP)": "",
        "Fecha de ingreso":                       "",
        "Observaciones":                          "",
        "Actor":                                  "",
        "Demandado":                              "",
        "Abogado del Actor":                      "",
        "Abogado del Demandado":                  "",
        "Cuantía":                                "",
        "Tipo de Acción":                         "",
        "Objeto Controversia":                    "",
        "Decisión Final":                         "",
        "Resumen Ejecutivo":                      "",
        "Link Drive PDF":                         "",
        "Link Drive Ficha":                       "",
        "Origen Cuantía":                         "",
        "Fecha Emisión":                          "",
        "PDF Truncado":                           "",
        "Modo Extracción":                        "",
    }

    # ── 1. Buscar causa en SATJE ───────────────────────────────────────────────
    causa = buscar_causa(numero)
    if not causa:
        fila["Completitud"] = COMPLETITUD_SIN_ACTUS
        log.info("Causa no encontrada en SATJE", job_id=job_id, causa=numero)
        return fila
    id_juicio = id_juicio_de(causa, numero)

    # Filtro de materia (config/materia.py). Si la materia tiene ids SATJE
    # verificados se decide aquí; si no, se decide con los incidentes (abajo).
    def _otra_materia():
        fila["Estado de Juicio"]   = MATERIA.ETIQUETA_OTRA_MATERIA
        fila["Resultado tribunal"] = (
            f"idMateria={causa.get('idMateria')} — {causa.get('nombreDelito', '')}"
        )
        fila["Completitud"] = COMPLETITUD_NO_APLICABLE
        log.info(f"{MATERIA.ETIQUETA_OTRA_MATERIA}: idMateria={causa.get('idMateria')}",
                 job_id=job_id, causa=numero)
        return fila

    if MATERIA.IDS_MATERIA_SATJE and not MATERIA.es_de_la_materia(causa):
        return _otra_materia()

    fecha_ingreso = causa.get("fechaIngreso", "")
    if fecha_ingreso:
        try:
            fila["Fecha de ingreso"] = datetime.fromisoformat(
                fecha_ingreso.replace("+00:00", "")
            ).strftime("%d/%m/%Y")
        except Exception:
            fila["Fecha de ingreso"] = fecha_ingreso[:10]

    # ── 2. Incidentes ──────────────────────────────────────────────────────────
    incidentes = obtener_incidentes(id_juicio)
    if not incidentes:
        fila["Estado de Juicio"] = "Sin resultados"
        fila["Completitud"]      = COMPLETITUD_SIN_ACTUS
        log.info("Sin incidentes", job_id=job_id, causa=numero)
        return fila

    if not MATERIA.IDS_MATERIA_SATJE and not MATERIA.es_de_la_materia(causa, incidentes):
        return _otra_materia()

    # ── 3. Actor y Demandado ───────────────────────────────────────────────────
    from resolucion.satje.tribunal import es_incidente_primera_instancia
    incs_primera = [i for i in incidentes if es_incidente_primera_instancia(i)]
    incs_primera.sort(key=lambda i: 0 if i.get("idJudicatura") == tribunal else 1)

    inc_para_partes = (
        next((i for i in incs_primera if i.get("idJudicatura") == tribunal), None)
        or (incs_primera[0] if incs_primera else None)
    )
    if inc_para_partes:
        lst_inc = inc_para_partes.get("lstIncidenteJudicatura", [])
        if lst_inc:
            actores    = lst_inc[0].get("lstLitiganteActor", [])
            demandados = lst_inc[0].get("lstLitiganteDemandado", [])
            if actores:
                fila["Actor"] = actores[0].get("nombresLitigante", "")
            if demandados:
                fila["Demandado"] = demandados[0].get("nombresLitigante", "")

    if fila.get("Actor"):
        try:
            from resolucion.clasificar_actor import clasificar_tipo_actor, normalizar_nombre_actor
            _tipo = clasificar_tipo_actor(fila["Actor"])
            fila["Tipo de Actor"]      = _tipo
            fila["Actor Normalizado"]  = normalizar_nombre_actor(fila["Actor"], _tipo)
        except Exception as e:
            log.warn(f"Error clasificando actor: {e}", job_id=job_id, causa=numero)

    # ── 4. Buscar resolución de primera instancia ──────────────────────────────
    resultado_tribunal, act_resolucion, actuaciones = buscar_actuacion_resolucion(
        id_juicio, incidentes, tribunal, job_id=job_id, causa=numero
    )
    fila["Resultado tribunal"] = resultado_tribunal

    # Duración del caso
    duracion = calcular_duracion(act_resolucion, actuaciones)
    fila.update(duracion)

    # ── 5. PDF + IA de primera instancia ──────────────────────────────────────
    es_sentencia = act_resolucion and "Con sentencia" in resultado_tribunal

    if es_sentencia and skip_pdf:
        log.info("PDF omitido (skip_pdf)", job_id=job_id, causa=numero)
        fila["Completitud"] = COMPLETITUD_PDF_SIN_GPT

    elif es_sentencia:
        # Guard 2 — reintentar la descarga del PDF ante fallo transitorio de
        # SATJE cuando el documento SÍ está indicado (ieDocumentoAdjunto=='S').
        # Sin esto, un fallo puntual de red deja la causa sin PDF ni análisis.
        _pdf_indicado = act_resolucion.get("ieDocumentoAdjunto") == "S"
        _intentos_pdf = 3 if _pdf_indicado else 1
        pdf_bytes, pdf_nombre = None, ""
        for _i in range(_intentos_pdf):
            pdf_bytes, pdf_nombre = descargar_doc_actuacion(
                act_resolucion, id_juicio, "sentencia"
            )
            if pdf_bytes or _i == _intentos_pdf - 1:
                break
            log.warn(f"Descarga de PDF falló (intento {_i + 1}/{_intentos_pdf}) — reintentando",
                     job_id=job_id, causa=numero)
            time.sleep(2 * (_i + 1))
        if pdf_bytes:
            log.info(f"PDF descargado ({len(pdf_bytes)//1024} KB)", job_id=job_id, causa=numero)
            fila["Sentencia Tribunal"] = f"{numero}_sentencia.pdf"

            if gpt_ok:
                campos = extraer_tribunal(
                    numero=numero,
                    pdf_bytes=pdf_bytes,
                    demandado=fila.get("Demandado", ""),
                    modelo=modelo,
                    job_id=job_id,
                    nombre_tribunal=nombre_tribunal,
                    ano=ano,
                    carpeta_pdfs=carpeta_local,
                    carpeta_docx=carpeta_docx,
                    flujo_proveedores=flujo_proveedores,
                )
                # Propagar todos los campos del extractor
                fila["_pdf_bytes_tribunal"]  = campos.get("_pdf_bytes_tribunal", pdf_bytes)
                fila["_pdf_nombre_tribunal"] = campos.get("_pdf_nombre_tribunal", f"{numero}_sentencia.pdf")
                fila["Cuantía"]              = campos.get("Cuantía", "")
                fila["Tipo de Acción"]       = campos.get("Tipo de Acción", "")
                fila["Objeto Controversia"]  = campos.get("Objeto Controversia", "")
                fila["Decisión Final"]       = campos.get("Decisión Final", "")
                fila["Resumen Ejecutivo"]    = campos.get("Resumen Ejecutivo", "")
                fila["Categorización"]       = campos.get("Categorización", "")
                fila["Abogado del Actor"]     = campos.get("Abogado del Actor", "")
                fila["Abogado del Demandado"] = campos.get("Abogado del Demandado", "")
                fila["_extraccion"]          = campos.get("_extraccion", "")
                fila["_jurisprudencia"]      = campos.get("_jurisprudencia", "")
                fila["Origen Cuantía"]       = campos.get("Origen Cuantía", "")
                fila["Fecha Emisión"]        = campos.get("Fecha Emisión", "")
                fila["PDF Truncado"]         = campos.get("PDF Truncado", "")
                fila["Modo Extracción"]      = campos.get("Modo Extracción", "")
                fila["Completitud"]          = campos.get("Completitud", "")
                fila["Observaciones"]        = campos.get("Observaciones", "")
                fila["_auditoria"]           = campos.get("_auditoria", {})

                docx_bytes = campos.get("_docx_bytes_tribunal")
                if docx_bytes:
                    nombre_docx = campos.get("_docx_nombre_tribunal", f"{numero}_ficha_tecnica.docx")
                    fila["_docx_bytes_tribunal"]             = docx_bytes
                    fila["_docx_nombre_tribunal"]            = nombre_docx
                    fila["Análisis IA - Sentencias Tribunal"] = nombre_docx

                if campos.get("error"):
                    log.warn(f"IA tribunal: {campos['error']}", job_id=job_id, causa=numero)

                # Fallback de abogados: si la sentencia no los trajo, buscar
                # en los escritos más antiguos del expediente (visión, Qwen).
                falta_actor     = not fila["Abogado del Actor"].strip()
                falta_demandado = not fila["Abogado del Demandado"].strip()
                if falta_actor or falta_demandado:
                    escritos_pdf = obtener_escritos_pdf(
                        id_juicio, incidentes, job_id=job_id, causa=numero,
                    )
                    if escritos_pdf:
                        completados = completar_abogados_con_escritos(
                            escritos_pdf,
                            actor=fila.get("Actor", ""),
                            demandado=fila.get("Demandado", ""),
                            falta_actor=falta_actor,
                            falta_demandado=falta_demandado,
                            job_id=job_id, causa=numero,
                        )
                        if completados.get("abogado_actor"):
                            fila["Abogado del Actor"] = completados["abogado_actor"]
                            fila["_auditoria"]["abogados"] = True
                            log.info(f"Abogado actor recuperado de escrito: {completados['abogado_actor']}", job_id=job_id, causa=numero)
                        if completados.get("abogado_demandado"):
                            fila["Abogado del Demandado"] = completados["abogado_demandado"]
                            fila["_auditoria"]["abogados"] = True
                            log.info(f"Abogado demandado recuperado de escrito: {completados['abogado_demandado']}", job_id=job_id, causa=numero)
            else:
                # Tiene sentencia y PDF pero GPT desactivado
                fila["_pdf_bytes_tribunal"]  = pdf_bytes
                fila["_pdf_nombre_tribunal"] = f"{numero}_sentencia.pdf"
                fila["Completitud"]          = COMPLETITUD_PDF_SIN_GPT
        else:
            # PDF no disponible en SATJE
            if not _pdf_indicado:
                log.info("Sin PDF adjunto en SATJE", job_id=job_id, causa=numero)
            else:
                # Guard 1 — el PDF SÍ estaba indicado pero la descarga falló tras
                # los reintentos. NO guardar una causa vacía (sin PDF ni análisis):
                # se marca para reintentar en la próxima corrida. El runner respeta
                # esta marca y no persiste la causa.
                log.warn("PDF adjunto indicado pero descarga fallida tras reintentos "
                         "— se marca para reintentar (no se guarda vacía)",
                         job_id=job_id, causa=numero)
                fila["_error_descarga_pdf"] = True
                _marcar_pdf_fallido(fila, "tribunal")

    elif act_resolucion:
        # Motivo COGEP de archivado — sin PDF
        log.info(f"Motivo COGEP (no sentencia): {resultado_tribunal}", job_id=job_id, causa=numero)

    # ── 6. Completitud si aún no fue asignada ─────────────────────────────────
    if not fila["Completitud"]:
        rt = fila["Resultado tribunal"]
        if rt == RESULTADO_EN_TRAMITE:
            fila["Completitud"] = COMPLETITUD_PENDIENTE
        elif rt == RESULTADO_SIN_ACTUACIONES:
            fila["Completitud"] = COMPLETITUD_SIN_ACTUS
        elif rt and "Con sentencia" not in rt:
            fila["Completitud"] = COMPLETITUD_ARCHIVADO
        else:
            fila["Completitud"] = COMPLETITUD_PENDIENTE

    # ── 7. Estado de Juicio ───────────────────────────────────────────────────
    fila["Estado de Juicio"] = determinar_estado(incidentes, resultado_tribunal)

    # ── 7b. Incidente de APELACIÓN (Corte Provincial) — solo materias de 3 niveles
    inc_apel = encontrar_incidente_apelacion(incidentes)
    if inc_apel:
        log.info("Procesando segunda instancia (Corte Provincial)...", job_id=job_id, causa=numero)
        acts_apel = obtener_actuaciones_apelacion(id_juicio, inc_apel)
        resultado_apel, act_apel = detectar_sentencia_apelacion(acts_apel)

        duracion_apel = calcular_duracion(act_apel, acts_apel)
        fila["_fecha_inicio_apel"]    = duracion_apel.get("_fecha_inicio_caso", "")
        fila["_fecha_sentencia_apel"] = duracion_apel.get("_fecha_sentencia_caso", "")
        fila["_duracion_dias_apel"]   = duracion_apel.get("_duracion_dias")

        if not resultado_apel:
            resultado_apel = RESULTADO_APELACION_EN_TRAMITE if acts_apel else "Sin actuaciones en apelación"
        fila["Resultado en Apelación"] = resultado_apel
        # La Sala ya resolvió (sentencia, desistimiento, abandono…): la causa
        # terminó en la Corte Provincial, no "está" en ella. Si hay casación,
        # el paso de la Corte Nacional (abajo) vuelve a fijar el estado.
        if act_apel and fila["Estado de Juicio"] == MATERIA.ESTADO_EN_APELACION:
            fila["Estado de Juicio"] = MATERIA.ESTADO_TERMINADO_APELACION

        obs_apel = []
        es_sentencia_apel = act_apel and resultado_apel in DESCRIPCIONES_SUBSTANTIVAS_APEL

        if es_sentencia_apel and skip_pdf:
            log.info("PDF apelación omitido (skip_pdf)", job_id=job_id, causa=numero)
            fila["Completitud Apelación"] = COMPLETITUD_PDF_SIN_GPT

        elif es_sentencia_apel:
            pdf_apel, _ = descargar_doc_actuacion(act_apel, id_juicio, "sentencia_apelacion")
            if pdf_apel:
                log.info(f"PDF apelación descargado ({len(pdf_apel)//1024} KB)", job_id=job_id, causa=numero)
                fila["Sentencia Apelación"] = f"{numero}_sentencia_apelacion.pdf"

                if gpt_cnj_ok:
                    campos_apel = extraer_apelacion(
                        pdf_bytes=pdf_apel,
                        numero=numero,
                        modelo=modelo,
                        job_id=job_id,
                        carpeta_pdfs=carpeta_local,
                        carpeta_docx=carpeta_docx,
                        flujo_proveedores=flujo_proveedores,
                    )
                    fila["_pdf_bytes_apel"]      = campos_apel.get("_pdf_bytes_apel", pdf_apel)
                    fila["_pdf_nombre_apel"]     = campos_apel.get("_pdf_nombre_apel", f"{numero}_sentencia_apelacion.pdf")
                    fila["_resumen_apel"]        = campos_apel.get("_resumen_apel", "")
                    fila["_extraccion_apel"]     = campos_apel.get("_extraccion_apel", "")
                    fila["_jurisprudencia_apel"] = campos_apel.get("_jurisprudencia_apel", "")
                    for k in ("_categorizacion_apel", "_tipos_defecto_apel"):
                        if k in campos_apel:
                            fila[k] = campos_apel[k]
                    fila["Completitud Apelación"] = campos_apel.get("Completitud Apelación", "")

                    docx_apel = campos_apel.get("_docx_bytes_apel")
                    if docx_apel:
                        nombre_docx_apel = campos_apel.get("_docx_nombre_apel", f"{numero}_ficha_tecnica_apelacion.docx")
                        fila["_docx_bytes_apel"]  = docx_apel
                        fila["_docx_nombre_apel"] = nombre_docx_apel
                        fila["Análisis IA - Sentencia Apelación"] = nombre_docx_apel

                    if campos_apel.get("_obs_apel"):
                        obs_apel.append(campos_apel["_obs_apel"])
                    if campos_apel.get("error"):
                        obs_apel.append(f"GPT-APEL: {campos_apel['error']}")
                else:
                    fila["_pdf_bytes_apel"]  = pdf_apel
                    fila["_pdf_nombre_apel"] = f"{numero}_sentencia_apelacion.pdf"
                    fila["Completitud Apelación"] = COMPLETITUD_PDF_SIN_GPT
            else:
                if act_apel.get("ieDocumentoAdjunto") != "S":
                    log.info("Apelación: sin PDF adjunto en SATJE", job_id=job_id, causa=numero)
                else:
                    log.warn("Apelación: PDF indicado pero descarga fallida — se marca para reintentar",
                             job_id=job_id, causa=numero)
                    fila["_error_descarga_apel"] = True
                    _marcar_pdf_fallido(fila, "apelacion")
                fila["Completitud Apelación"] = COMPLETITUD_PENDIENTE

        elif act_apel:
            log.info(f"Apelación: no sustantiva → {resultado_apel}", job_id=job_id, causa=numero)
            fila["Completitud Apelación"] = COMPLETITUD_ARCHIVADO
        else:
            log.info("Apelación: sin resolución todavía", job_id=job_id, causa=numero)
            fila["Completitud Apelación"] = COMPLETITUD_PENDIENTE

        if obs_apel:
            existing = fila.get("Observaciones", "")
            obs_str  = "; ".join(obs_apel)
            fila["Observaciones"] = (
                f"{existing} | APEL: {obs_str}"[:400] if existing
                else f"APEL: {obs_str}"[:400]
            )

    # ── 8. Incidente CNJ ──────────────────────────────────────────────────────
    inc_cnj = encontrar_incidente_cnj(incidentes)
    if inc_cnj:
        log.info("Procesando segunda instancia (CNJ)...", job_id=job_id, causa=numero)
        acts_cnj        = obtener_actuaciones_cnj(id_juicio, inc_cnj)
        resultado_cnj, act_cnj = detectar_sentencia_cnj(acts_cnj)

        # Duración del caso EN CORTE — misma lógica que primera instancia
        # (de la actuación más antigua del incidente CNJ, ej. acta de
        # sorteo, a la fecha de la resolución CNJ). Claves distintas para
        # no pisar _fecha_inicio_caso/_duracion_dias del tribunal.
        duracion_cnj = calcular_duracion(act_cnj, acts_cnj)
        fila["_fecha_inicio_cnj"]    = duracion_cnj.get("_fecha_inicio_caso", "")
        fila["_fecha_sentencia_cnj"] = duracion_cnj.get("_fecha_sentencia_caso", "")
        fila["_duracion_dias_cnj"]   = duracion_cnj.get("_duracion_dias")

        if not resultado_cnj:
            resultado_cnj = RESULTADO_CNJ_EN_TRAMITE if acts_cnj else "Sin actuaciones CNJ"
        fila["Resultado en Corte"] = resultado_cnj

        obs_cnj = []
        es_sentencia_cnj = act_cnj and resultado_cnj in DESCRIPCIONES_SUBSTANTIVAS

        if es_sentencia_cnj and skip_pdf:
            log.info("PDF CNJ omitido (skip_pdf)", job_id=job_id, causa=numero)
            fila["Completitud CNJ"] = COMPLETITUD_PDF_SIN_GPT

        elif es_sentencia_cnj:
            pdf_cnj, _ = descargar_doc_actuacion(act_cnj, id_juicio, "sentencia_cnj")
            if pdf_cnj:
                log.info(f"PDF CNJ descargado ({len(pdf_cnj)//1024} KB)", job_id=job_id, causa=numero)
                fila["Sentencia Corte Nacional"] = f"{numero}_sentencia_cnj.pdf"

                if gpt_cnj_ok:
                    campos_cnj = extraer_corte(
                        pdf_bytes=pdf_cnj,
                        numero=numero,
                        modelo=modelo,
                        job_id=job_id,
                        carpeta_pdfs=carpeta_local,
                        carpeta_docx=carpeta_docx,
                        flujo_proveedores=flujo_proveedores,
                    )
                    fila["_pdf_bytes_cnj"]    = campos_cnj.get("_pdf_bytes_cnj", pdf_cnj)
                    fila["_pdf_nombre_cnj"]   = campos_cnj.get("_pdf_nombre_cnj", f"{numero}_sentencia_cnj.pdf")
                    fila["_resumen_cnj"]      = campos_cnj.get("_resumen_cnj", "")
                    fila["_extraccion_cnj"]   = campos_cnj.get("_extraccion_cnj", "")
                    fila["_jurisprudencia_cnj"] = campos_cnj.get("_jurisprudencia_cnj", "")
                    for k in ("_categorizacion_cnj", "_tipos_defecto_cnj"):
                        if k in campos_cnj:
                            fila[k] = campos_cnj[k]
                    fila["Completitud CNJ"]   = campos_cnj.get("Completitud CNJ", "")

                    docx_cnj = campos_cnj.get("_docx_bytes_cnj")
                    if docx_cnj:
                        nombre_docx_cnj = campos_cnj.get("_docx_nombre_cnj", f"{numero}_ficha_tecnica_cnj.docx")
                        fila["_docx_bytes_cnj"]  = docx_cnj
                        fila["_docx_nombre_cnj"] = nombre_docx_cnj
                        fila["Análisis IA - Sentencia Corte Nacional"] = nombre_docx_cnj

                    if campos_cnj.get("_obs_cnj"):
                        obs_cnj.append(campos_cnj["_obs_cnj"])
                    if campos_cnj.get("error"):
                        obs_cnj.append(f"GPT-CNJ: {campos_cnj['error']}")
                else:
                    fila["_pdf_bytes_cnj"]  = pdf_cnj
                    fila["_pdf_nombre_cnj"] = f"{numero}_sentencia_cnj.pdf"
                    fila["Completitud CNJ"] = COMPLETITUD_PDF_SIN_GPT
            else:
                if act_cnj.get("ieDocumentoAdjunto") != "S":
                    log.info("CNJ: sin PDF adjunto en SATJE", job_id=job_id, causa=numero)
                else:
                    # Guard 1 (corte) — PDF de la casación indicado pero descarga
                    # fallida: se marca para reintentar; no se guarda casación vacía.
                    log.warn("CNJ: PDF indicado pero descarga fallida "
                             "— casación se marca para reintentar (no se guarda)",
                             job_id=job_id, causa=numero)
                    fila["_error_descarga_cnj"] = True
                    _marcar_pdf_fallido(fila, "cnj")
                fila["Completitud CNJ"] = COMPLETITUD_PENDIENTE

        elif act_cnj and resultado_cnj in DESCRIPCIONES_TERMINALES_SIN_FONDO:
            # Inadmisión / improcedencia / desistimiento / abandono: la casación
            # terminó sin sentencia de fondo. Se baja el auto y se marca para que
            # el runner cree la ficha de casación (sin IA) con su decisión.
            log.info(f"CNJ: terminada sin fondo → {resultado_cnj}", job_id=job_id, causa=numero)
            fila["_cnj_sin_fondo"] = True
            fila["Completitud CNJ"] = COMPLETITUD_ARCHIVADO
            if not skip_pdf and act_cnj.get("ieDocumentoAdjunto") == "S":
                pdf_auto, _ = descargar_doc_actuacion(act_cnj, id_juicio, "auto_cnj")
                if pdf_auto:
                    log.info(f"PDF auto CNJ descargado ({len(pdf_auto)//1024} KB)", job_id=job_id, causa=numero)
                    fila["Sentencia Corte Nacional"] = f"{numero}_auto_cnj.pdf"
                    fila["_pdf_bytes_cnj"]  = pdf_auto
                    fila["_pdf_nombre_cnj"] = f"{numero}_auto_cnj.pdf"
                else:
                    log.warn("CNJ: auto indicado pero descarga fallida", job_id=job_id, causa=numero)
                    _marcar_pdf_fallido(fila, "cnj")

        elif act_cnj:
            log.info(f"CNJ: no sustantiva → {resultado_cnj}", job_id=job_id, causa=numero)
            fila["Completitud CNJ"] = COMPLETITUD_ARCHIVADO
        else:
            log.info("CNJ: sin resolución todavía", job_id=job_id, causa=numero)
            fila["Completitud CNJ"] = COMPLETITUD_PENDIENTE

        if obs_cnj:
            existing = fila.get("Observaciones", "")
            obs_str  = "; ".join(obs_cnj)
            fila["Observaciones"] = (
                f"{existing} | CNJ: {obs_str}"[:400] if existing
                else f"CNJ: {obs_str}"[:400]
            )

        # La casación ya se resolvió (con o sin fondo): la causa terminó en la
        # Corte Nacional, no "está" en ella.
        if MATERIA.ESTADO_TERMINADO_CNJ and act_cnj and (
            resultado_cnj in DESCRIPCIONES_SUBSTANTIVAS
            or resultado_cnj in DESCRIPCIONES_TERMINALES_SIN_FONDO
        ):
            fila["Estado de Juicio"] = MATERIA.ESTADO_TERMINADO_CNJ

    return fila
