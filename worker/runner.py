"""
Hilo de extracción: itera causas y llama al motor de extracción (orquestador).
Reporta progreso a job_manager después de cada causa.
"""

import json
import sys
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import resolucion.orquestador as orq
from resolucion.cms import tribunal as cms_tribunal
from resolucion.cms import corte as cms_corte
from resolucion.cms import apelacion as cms_apelacion
from config.materia import ETIQUETA_OTRA_MATERIA as MATERIA_ETIQUETA_OTRA
from config.config import TRIBUNALES, PAUSA_SEG, HISTORICO_PRE_REFACTOR, MAX_VACIAS_RESOLUCION
from worker import job_manager as jm
from worker import logger as log
from worker.strapi_client import StrapiClient
from api import SatjeNoDisponible
from worker import numeracion

STRAPI = StrapiClient()

TIMEOUT_CAUSA_SEG = 40 * 60  # 40 minutos máximo por causa

# Caída de SATJE: minutos de pausa tras cada fallo seguido (el último se repite).
SATJE_BACKOFF_MIN = (2, 5, 10, 20, 30)
# Estado compartido por los jobs del proceso: con SATJE caído, un solo job
# sondea cuando vence la pausa y los demás esperan su resultado.
_satje = {"hasta": 0.0, "fallos": 0, "sondea": None}
_satje_lock = threading.Lock()


def ejecutar_job(job_id: int, pause_ev: threading.Event, kill_ev: threading.Event):
    """
    Loop principal de un job. Corre en un hilo propio.
    - pause_ev.set()  → pausa después de la causa actual
    - kill_ev.set()   → cancela inmediatamente tras la causa actual
    Si el job tiene workers activos (ej. 'duracion'), delega al worker correspondiente
    sin ejecutar la extracción completa de sentencias.
    """
    job = jm.obtener_job(job_id)
    if not job:
        return

    tribunal     = job["tribunal"]
    año          = job["año"]
    desde        = job["checkpoint"] + 1 if job["checkpoint"] > 0 else job["desde"]
    hasta        = job["hasta"]
    dry_run      = job["dry_run"]
    # vista_previa: corre la IA igual que una corrida completa, pero no
    # escribe nada en Strapi (ni archivos ni el registro de causa/casación)
    # — para revisar en /admin/registros y en este mismo log qué extrae el
    # extractor con prompts/modelo actuales sin tocar lo publicado.
    vista_previa = job.get("vista_previa", False)
    # solo_descarga: SATJE + PDF + metadatos, sin IA, y SÍ persiste (Strapi +
    # registro canónico) con Completitud "PDF sin análisis". Lo contrario de
    # vista_previa (IA sin persistir). Ver POST /reextraer para la IA después.
    solo_descarga = bool(job.get("solo_descarga", False))
    modelo       = job["modelo"]
    # flujo_proveedores: "alibaba" (default), "novita" o "gmicloud" — cuál proveedor de
    # OpenRouter se intenta primero para este modelo (ver resolver_orden_
    # proveedores en resolucion/ai/_cliente.py). Mismo modelo, mismas pesas.
    flujo_proveedores = job.get("flujo_proveedores", "alibaba")
    workers      = (job.get("workers") or "").strip()

    # ── Modo worker-only: delegar sin ejecutar extracción completa ───────────
    activos = [w.strip() for w in workers.split(",") if w.strip()]

    if "duracion" in activos:
        from worker.duration_worker import ejecutar_job_duracion
        ejecutar_job_duracion(
            job_id, tribunal, año, desde, hasta, pause_ev, kill_ev
        )
        return

    # "lista" — procesa un conjunto PERSONALIZADO de causas (worker_lista_items),
    # cada una con flags de qué extraer (tribunal / corte). Ver worker/lista_worker.py.
    # "indice" — índice rápido de SATJE (solo cabeceras, varios hilos).
    if "indice" in activos:
        from worker.indice_worker import ejecutar_job_indice
        ejecutar_job_indice(job_id, pause_ev, kill_ev)
        return

    # "mapeo" — cuenta causas por judicatura y año (worker/mapeo_worker.py).
    if "mapeo" in activos:
        from worker.mapeo_worker import ejecutar_job_mapeo
        ejecutar_job_mapeo(job_id, pause_ev, kill_ev)
        return

    # "reindexar_otra_materia" — re-consulta los números que el índice anotó
    # como de otra materia (worker/reindexar_worker.py). Categoría "indice".
    if "reindexar_otra_materia" in activos:
        from worker.reindexar_worker import ejecutar_job_reindexar
        ejecutar_job_reindexar(job_id, pause_ev, kill_ev)
        return

    if "lista" in activos:
        from worker.lista_worker import ejecutar_job_lista
        ejecutar_job_lista(job_id, pause_ev, kill_ev, modelo=modelo,
                           flujo_proveedores=flujo_proveedores)
        return

    # "backfill:<campo>:<lote>:<modo>" — procesa causas candidatas para ese
    # campo, en modo "completar" (default, solo lo faltante) o
    # "sobrescribir" (todo el historial con sentencia) — ver
    # worker/backfill_worker.py. Si el job se lanzó con tribunal/año reales
    # (no el placeholder "-" que usa el endpoint /backfill genérico), se
    # acota al rango desde/hasta pedido — igual que una extracción normal.
    # "embeddings[:<modo>]" — (re)embebe secciones en causa_section_embeddings.
    # modo "faltantes" (default) solo toca lo nuevo o cambiado; "todo" ignora el
    # hash y rehace el corpus entero (se usa al cambiar de modelo, ver
    # worker/embed_worker.py). Comparte los 4 slots de extracción: si están
    # ocupados el job espera en 'pendiente', no se adelanta.
    # "brief[:<seccion>]" — briefs doctrinales con OpenAI. Sección vacía = todas
    # (jueces, tribunales, cnj, abogados, entidades, actores). Ver
    # worker/brief_worker.py: no genera embeddings, es generación de texto.
    # "revision[:<año>][:vivas]" — consulta SATJE y encola en una lista solo
    # las causas que cambiaron. No baja PDFs ni llama a la IA: de eso se
    # encarga el job de lista que crea al final. Ver worker/revision_worker.py.
    marca_rev = next((w for w in activos if w == "revision" or w.startswith("revision:")), None)
    if marca_rev:
        # La lista "t=a,b,c" lleva comas y el split de arriba la parte: se
        # relee la marca entera desde el texto original (si no, cada job de
        # una revisión particionada revisaba solo el primer tribunal).
        marca_rev = workers[workers.index(marca_rev):]
        partes = marca_rev.split(":")
        anio_rev = partes[1] if len(partes) > 1 and partes[1] else ""
        # "completa" = barrido de línea base desde el 1 (para un año nunca
        # revisado). Sin eso, "diaria": vigila lo que puede moverse y avanza
        # la frontera. Ver worker/revision_worker.py.
        modo_rev = "completa" if "completa" in partes[2:] else "diaria"
        # "t=09501,17510" acota el job a esos tribunales, para repartir el
        # trabajo entre los 4 slots en vez de hacerlo todo en uno solo.
        marca_t = next((x for x in partes[2:] if x.startswith("t=")), None)
        tribs = marca_t[2:].split(",") if marca_t else None
        # "descarga": lo que falte se baja sin IA (jobs solo_descarga), para la
        # fase "corpus primero" de las materias nuevas.
        from worker.revision_worker import ejecutar_job_revision
        ejecutar_job_revision(job_id, pause_ev, kill_ev, anio=anio_rev,
                              modo=modo_rev, tribunales=tribs,
                              descarga="descarga" in partes[2:])
        return

    # "reextraccion[:niveles=…][:completitud=…][:limite=N]" — corre la IA sobre
    # PDFs ya guardados en Strapi, sin ir a SATJE. Ver worker/reextraccion_worker.py.
    marca_reex = next((w for w in activos if w == "reextraccion" or w.startswith("reextraccion:")), None)
    if marca_reex:
        from worker.reextraccion_worker import ejecutar_job_reextraccion
        ejecutar_job_reextraccion(job_id, pause_ev, kill_ev, modelo=modelo,
                                  flujo_proveedores=flujo_proveedores, marca=marca_reex)
        return

    marca_brief = next((w for w in activos if w == "brief" or w.startswith("brief:")), None)
    if marca_brief:
        partes = marca_brief.split(":")
        seccion = partes[1] if len(partes) > 1 else ""
        from worker.brief_worker import ejecutar_job_brief
        ejecutar_job_brief(job_id, pause_ev, kill_ev, only=seccion)
        return

    marca_embed = next((w for w in activos if w == "embeddings" or w.startswith("embeddings:")), None)
    if marca_embed:
        partes = marca_embed.split(":")
        modo_embed = partes[1] if len(partes) > 1 and partes[1] else "faltantes"
        from worker.embed_worker import ejecutar_job_embeddings
        ejecutar_job_embeddings(job_id, pause_ev, kill_ev, modo=modo_embed)
        return

    backfill_marca = next((w for w in activos if w.startswith("backfill:")), None)
    if backfill_marca:
        partes = backfill_marca.split(":")
        campo = partes[1] if len(partes) > 1 else ""
        lote = int(partes[2]) if len(partes) > 2 and partes[2].isdigit() else 100
        modo = partes[3] if len(partes) > 3 and partes[3] else "completar"
        from worker.backfill_worker import ejecutar_job_backfill
        ejecutar_job_backfill(job_id, campo, lote, pause_ev, kill_ev, modo=modo,
                              tribunal=tribunal, año=año, desde=desde, hasta=hasta)
        return

    nombre_tribunal = TRIBUNALES.get(tribunal, tribunal)

    corrida_id       = f"worker-job-{job_id}"
    carpeta_pdfs     = PROJECT_ROOT / "resultados" / "pdfs" / "resolucion" / corrida_id
    carpeta_words    = PROJECT_ROOT / "resultados" / "words" / "resolucion" / corrida_id
    carpeta_pdfs.mkdir(parents=True, exist_ok=True)
    carpeta_words.mkdir(parents=True, exist_ok=True)

    ctx = {
        "tribunal":        tribunal,
        "nombre_tribunal": nombre_tribunal,
        "ano":             año,
        "corrida_id":      corrida_id,
        "carpeta_local":   carpeta_pdfs,
        "carpeta_docx":    carpeta_words,
        "gpt_ok":          not dry_run and not solo_descarga,
        "gpt_cnj_ok":      not dry_run and not solo_descarga,
        "skip_pdf":        False,
        "modelo":          modelo,
        "flujo_proveedores": flujo_proveedores,
        "job_id":          job_id,
    }

    log.info(f"Iniciando job — tribunal={tribunal} año={año} desde={desde} hasta={hasta or '∞'} "
             f"modelo={modelo} dry_run={dry_run} vista_previa={vista_previa} solo_descarga={solo_descarga} "
             f"flujo_proveedores={flujo_proveedores}", job_id=job_id)

    inicio        = datetime.now(timezone.utc)
    vacias_consec = 0
    n             = desde

    # "desde_indice": solo se procesan los números que el índice de SATJE
    # (worker/indice_worker.py) marcó como de la materia; el resto se salta
    # sin consultar SATJE. Requiere el par (tribunal, año) indexado completo.
    permitidos = None
    if "desde_indice" in activos:
        from worker.indice_worker import secuenciales_de_materia
        from config import materia as _MAT
        lista_idx = secuenciales_de_materia(tribunal, año, _MAT.IDS_MATERIA_SATJE)
        if lista_idx is None:
            log.warn("desde_indice: el par no está indexado por completo — job pausado",
                     job_id=job_id)
            jm._actualizar(job_id, estado="pausado")
            return
        permitidos = set(lista_idx)
        hasta = min(hasta, max(lista_idx)) if (hasta and lista_idx) else (max(lista_idx) if lista_idx else 0)
        log.info(f"desde_indice: {len(lista_idx)} causas de la materia en el índice", job_id=job_id)

    # "reintento": solo los secuenciales de parametros.numeros (causas que
    # quedaron con error de red; ver jm._reintentar_errores_red).
    if "reintento" in activos:
        permitidos = set(json.loads(job.get("parametros") or "{}").get("numeros", []))

    while True:
        if kill_ev.is_set():
            jm._actualizar(job_id, estado="cancelado", completado_en=_now())
            log.info("Job cancelado por señal kill", job_id=job_id)
            return

        if pause_ev.is_set():
            jm._actualizar(job_id, estado="pausado")
            log.info("Job pausado", job_id=job_id)
            return

        if permitidos is not None and n > hasta:
            break
        if hasta and n > hasta:
            break
        if permitidos is not None and n not in permitidos:
            n += 1
            continue

        numero = numeracion.numero(tribunal, año, n)

        if not _esperar_turno_satje(job_id, pause_ev, kill_ev):
            continue  # llegó pausa o kill: lo registra el inicio del bucle

        evento_procesando = {"tipo": "procesando", "causa": numero, "n": n}
        jm._emitir_sse(job_id, evento_procesando)

        _stop_heartbeat = threading.Event()

        def _heartbeat():
            while not _stop_heartbeat.wait(30):
                jm._emitir_sse(job_id, evento_procesando)

        hb = threading.Thread(target=_heartbeat, daemon=True)
        hb.start()

        try:
            try:
                fila = _procesar_con_timeout(numero, ctx)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if isinstance(e, RuntimeError) and "interpreter shutdown" in str(e):
                    # El proceso se está apagando (deploy/reinicio): no registrar
                    # la causa como error ni avanzar el checkpoint; el job vuelve
                    # a la cola al arrancar el nuevo proceso y la retoma aquí.
                    log.warn("Apagado del proceso — se retoma tras el reinicio",
                             job_id=job_id, causa=numero)
                    return
                if _satje_caido(e):
                    # Sin fila de error ni checkpoint: el mismo número se
                    # reintenta cuando vence la pausa.
                    _satje_resultado(job_id, numero, caido=True)
                    continue
                categoria = _clasificar_error(e)
                tb = traceback.format_exc()
                log.error(
                    f"[{categoria}] {type(e).__name__}: {e}\n{tb}",
                    job_id=job_id, causa=numero,
                )
                fila = {
                    "Número de juicio": numero,
                    "Estado de Juicio": "Error de procesamiento",
                    "Completitud":      "Error GPT",
                    "Observaciones":    f"[{categoria}] {type(e).__name__}: {str(e)[:350]}",
                }
        finally:
            _stop_heartbeat.set()
        _satje_resultado(job_id, numero, caido=False)

        estado_juicio = fila.get("Estado de Juicio", "Sin resultados")

        if estado_juicio == "Sin resultados":
            vacias_consec += 1
            fila["_tribunal_codigo"] = tribunal
            try:
                jm.registrar_caso(job_id, fila)
            except Exception as e:
                log.error(f"DB_LOG: {type(e).__name__}: {e}", job_id=job_id, causa=numero)
            if vacias_consec >= MAX_VACIAS_RESOLUCION:
                break
        else:
            vacias_consec = 0
            fila["_tribunal_codigo"] = tribunal

            if not dry_run and not vista_previa:
                _subir_archivos_strapi(fila, STRAPI, job_id)

            try:
                jm.registrar_caso(job_id, fila)
            except Exception as e:
                log.error(f"DB_LOG: {type(e).__name__}: {e}", job_id=job_id, causa=numero)

            resultado_tribunal = fila.get("Resultado tribunal", "")
            tiene_sentencia    = "Con sentencia" in resultado_tribunal
            es_error           = "error" in estado_juicio.lower()
            # Guard 1 — el PDF estaba indicado pero no se pudo descargar (tras
            # reintentos). No se guarda una causa vacía; queda para reintentar.
            error_pdf          = bool(fila.get("_error_descarga_pdf"))

            strapi_fallo = False
            strapi_error = None

            if not es_error and not error_pdf and tiene_sentencia and not dry_run and not vista_previa:
                try:
                    result_causa = cms_tribunal.guardar(fila, STRAPI, job_id)
                    causa_doc_id = (result_causa.get("data") or {}).get("documentId", "") if result_causa else ""

                    if causa_doc_id and fila.get("_tipos_defecto") and not solo_descarga:
                        try:
                            jm.guardar_tipos_defecto(causa_doc_id, fila["_tipos_defecto"])
                        except Exception as e_tipos:
                            # No es un content-type de Strapi, es una tabla propia del
                            # worker — un fallo acá no debe tumbar el guardado de la causa.
                            log.error(
                                f"Tipos de defecto (interno) — {type(e_tipos).__name__}: {e_tipos}",
                                job_id=job_id, causa=numero,
                            )

                    if causa_doc_id and fila.get("_evidencia_ausente"):
                        try:
                            jm.guardar_evidencia_ausente(causa_doc_id, fila["_evidencia_ausente"])
                        except Exception as e_ev:
                            log.error(
                                f"Evidencia ausente (interno) — {type(e_ev).__name__}: {e_ev}",
                                job_id=job_id, causa=numero,
                            )

                    # Prompt 10 — extracción estructurada (causa_extraccion).
                    # Antes vivía en un script suelto de la webapp que corrió una
                    # sola vez: 578 causas (16%) quedaron sin estos campos, y sin
                    # ellos se caen dos de los seis factores de atribución.
                    if causa_doc_id and not solo_descarga:
                        try:
                            jm.clasificar_y_guardar_extraccion(
                                causa_doc_id, modelo, numero=numero, job_id=job_id,
                                flujo_proveedores=flujo_proveedores,
                            )
                        except Exception as e_ext:
                            log.error(
                                f"Extraccion estructurada (interno) — {type(e_ext).__name__}: {e_ext}",
                                job_id=job_id, causa=numero,
                            )

                    # Prompt 8 — corre DESPUÉS de guardar en Strapi a propósito:
                    # lee de vuelta de la base los 4 campos ya parseados en vez de
                    # re-parsear el texto crudo, para que la clasificación de causas
                    # nuevas use exactamente el mismo input que el backfill histórico.
                    if causa_doc_id and not solo_descarga:
                        try:
                            jm.clasificar_y_guardar_tipos_prueba(
                                causa_doc_id, modelo, numero=numero, job_id=job_id,
                            )
                        except Exception as e_prueba:
                            log.error(
                                f"Tipos de prueba (interno) — {type(e_prueba).__name__}: {e_prueba}",
                                job_id=job_id, causa=numero,
                            )

                    # Apelación (Corte Provincial) — solo materias de tres niveles.
                    # En solo_descarga se crea el registro con el PDF y los
                    # metadatos aunque no haya extracción (cascarón para reextraer).
                    if result_causa and causa_doc_id and (
                        fila.get("_resumen_apel") or fila.get("_extraccion_apel")
                        or (solo_descarga and fila.get("_file_id_pdf_apel"))
                    ):
                        try:
                            cms_apelacion.guardar(fila, causa_doc_id, STRAPI, job_id)
                        except Exception as e_apel:
                            log.error(
                                f"Apelación — {type(e_apel).__name__}: {e_apel}\n{traceback.format_exc()}",
                                job_id=job_id, causa=numero,
                            )

                    if result_causa and (
                        fila.get("_resumen_cnj") or fila.get("_extraccion_cnj")
                        or (solo_descarga and fila.get("_file_id_pdf_cnj"))
                        or fila.get("_cnj_sin_fondo")
                    ):
                        if causa_doc_id:
                            try:
                                cms_corte.guardar(fila, causa_doc_id, STRAPI, job_id)
                            except Exception as e_cnj:
                                log.error(
                                    f"Casación — {type(e_cnj).__name__}: {e_cnj}\n{traceback.format_exc()}",
                                    job_id=job_id, causa=numero,
                                )
                except Exception as e:
                    strapi_error = str(e)[:400]
                    strapi_fallo = True
                    log.error(f"Strapi causa — {strapi_error}", job_id=job_id, causa=numero)
                    jm.guardar_pendiente(job_id, numero, fila, strapi_error)

            exitosa = tiene_sentencia and not es_error and not error_pdf and (dry_run or not strapi_fallo)
            omitida = not es_error and not error_pdf and not tiene_sentencia

            if error_pdf:
                log.warn("PDF no descargado — no se guarda; queda para reintentar",
                         job_id=job_id, causa=numero)
            elif tiene_sentencia:
                log.info(
                    f"Con sentencia{' (dry_run)' if dry_run else ' (vista previa, sin guardar en Strapi)' if vista_previa else ' (solo descarga, sin IA)' if solo_descarga else ''}",
                    job_id=job_id, causa=numero,
                )
            elif not es_error:
                log.info(f"Omitida: {resultado_tribunal or estado_juicio}", job_id=job_id, causa=numero)

            elapsed    = (datetime.now(timezone.utc) - inicio).total_seconds()
            job_bd     = jm.obtener_job(job_id)
            procesadas = (job_bd["total_procesadas"] or 0) + 1
            ritmo      = elapsed / procesadas if procesadas else 0
            rango      = (hasta - desde + 1) if hasta else None
            eta_seg    = int(ritmo * (rango - procesadas)) if rango else None

            resultado_cnj = fila.get("Resultado en Corte", "") or ""

            evento = {
                "tipo":            "progreso",
                "n":               n,
                "causa":           numero,
                "estado":          estado_juicio,
                "resultado":       resultado_tribunal,
                "resultado_cnj":   resultado_cnj,
                "completitud":     fila.get("Completitud", ""),
                "procesadas":      procesadas,
                "exitosas":        (job_bd["exitosas"] or 0) + (1 if exitosa else 0),
                "omitidas":        (job_bd["omitidas"] or 0) + (1 if omitida else 0),
                "errores":         (job_bd["errores"] or 0) + (1 if (es_error or error_pdf) else 0),
                "strapi_fallo":    strapi_fallo,
                "strapi_error":    strapi_error,
                "checkpoint":      n,
                "elapsed_seg":     int(elapsed),
                "eta_seg":         eta_seg,
            }
            jm.reportar_progreso(job_id, n, exitosa, evento,
                                 strapi_fallo=strapi_fallo, omitida=omitida)

        n += 1
        # La pausa larga solo hace falta cuando se bajaron documentos; una
        # causa inexistente o de otra materia cuesta una sola consulta.
        if (fila.get("Estado de Juicio") in ("Sin resultados", MATERIA_ETIQUETA_OTRA)):
            time.sleep(0.2)
        else:
            time.sleep(PAUSA_SEG)

    ts_completado = _now()
    jm._actualizar(job_id, estado="completado", completado_en=ts_completado)
    jm._emitir_sse(job_id, {"tipo": "estado", "estado": "completado",
                             "completado_en": ts_completado.isoformat()})
    log.info("Job completado", job_id=job_id)


def _procesar_con_timeout(numero: str, ctx: dict) -> dict:
    """
    Llama a orquestador.procesar_causa() en un executor y levanta TimeoutError
    si supera TIMEOUT_CAUSA_SEG. El hilo subyacente no se puede matar desde
    Python, pero el runner deja de esperarlo y avanza a la siguiente causa.
    """
    job_id = ctx.get("job_id")
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(orq.procesar_causa, numero, ctx)
        try:
            return future.result(timeout=TIMEOUT_CAUSA_SEG)
        except FuturesTimeout:
            mins = TIMEOUT_CAUSA_SEG // 60
            log.error(f"Timeout: causa superó {mins} minutos", job_id=job_id, causa=numero)
            raise TimeoutError(f"procesar_causa({numero}) superó {mins} min")


def _satje_caido(e: Exception) -> bool:
    """SATJE no responde: agotó reintentos, o error de red hacia su host."""
    if isinstance(e, SatjeNoDisponible):
        return True
    return _clasificar_error(e) == "RED_TIMEOUT" and "funcionjudicial" in str(e)


def _esperar_turno_satje(job_id: int, pause_ev: threading.Event,
                         kill_ev: threading.Event) -> bool:
    """Con SATJE caído, espera a que venza la pausa y reserva el sondeo para
    este job. Devuelve False si llegó pausa o kill durante la espera."""
    while True:
        with _satje_lock:
            ahora = time.monotonic()
            if not _satje["fallos"]:
                return True
            if _satje["hasta"] <= ahora and _satje["sondea"] in (None, job_id):
                _satje["sondea"] = job_id
                return True
            espera = _satje["hasta"] - ahora if _satje["hasta"] > ahora else 5  # otro job sondea
        if pause_ev.is_set() or kill_ev.wait(min(espera, 5)):
            return False


def _satje_resultado(job_id: int, numero: str, caido: bool) -> None:
    """Anota si SATJE respondió. Un fallo alarga la pausa (backoff) solo si la
    trae el sondeo o la pausa ya venció; un éxito la reinicia."""
    if not caido:
        with _satje_lock:
            volvio = bool(_satje["fallos"])
            _satje.update(hasta=0.0, fallos=0, sondea=None)
        if volvio:
            log.info("SATJE responde de nuevo — se reanuda", job_id=job_id, causa=numero)
        return
    with _satje_lock:
        ahora = time.monotonic()
        if _satje["sondea"] == job_id or _satje["hasta"] <= ahora:
            _satje["fallos"] += 1
            minutos = SATJE_BACKOFF_MIN[min(_satje["fallos"], len(SATJE_BACKOFF_MIN)) - 1]
            _satje["hasta"] = ahora + minutos * 60
        if _satje["sondea"] == job_id:
            _satje["sondea"] = None
        minutos = max(1, round((_satje["hasta"] - ahora) / 60))
    log.warn(f"SATJE sin respuesta — pausa {minutos} min antes de reintentar {numero}",
             job_id=job_id, causa=numero)


def _clasificar_error(e: Exception) -> str:
    nombre = type(e).__name__
    msg    = str(e).lower()
    if nombre in ("ConnectionError", "ConnectTimeout", "ReadTimeout", "ConnectError"):
        return "RED_TIMEOUT"
    if nombre in ("HTTPStatusError", "HTTPError"):
        return "HTTP_ERROR"
    if "satje" in msg or "actuaciones" in msg or "incidentes" in msg:
        return "SATJE_ERROR"
    if "pdf" in msg or "pypdf" in msg or "pdfread" in msg.lower():
        return "PDF_ERROR"
    if nombre in ("ImportError", "ModuleNotFoundError"):
        return "IMPORT_ERROR"
    if nombre in ("KeyError", "AttributeError", "TypeError", "ValueError"):
        return "DATA_ERROR"
    if "openai" in msg or "deepinfra" in msg or "api key" in msg:
        return "GPT_API_ERROR"
    if "timeout" in msg or "timed out" in msg:
        return "TIMEOUT"
    if nombre == "JSONDecodeError":
        return "JSON_ERROR"
    return f"UNKNOWN_{nombre}"


def _subir_archivos_strapi(fila: dict, strapi, job_id: int) -> None:
    pares = [
        ("_pdf_bytes_tribunal",  "_pdf_nombre_tribunal",  "_url_pdf_tribunal",  "_file_id_pdf_tribunal", "pdf"),
        ("_docx_bytes_tribunal", "_docx_nombre_tribunal", "_url_docx_tribunal", None,                    "docx"),
        ("_pdf_bytes_cnj",       "_pdf_nombre_cnj",       "_url_pdf_cnj",       "_file_id_pdf_cnj",      "pdf"),
        ("_docx_bytes_cnj",      "_docx_nombre_cnj",      "_url_docx_cnj",      None,                    "docx"),
        ("_pdf_bytes_apel",      "_pdf_nombre_apel",      "_url_pdf_apel",      "_file_id_pdf_apel",     "pdf"),
        ("_docx_bytes_apel",     "_docx_nombre_apel",     "_url_docx_apel",     None,                    "docx"),
    ]
    numero = fila.get("Número de juicio", "causa")
    for key_bytes, key_nombre, key_url, key_file_id, tipo in pares:
        data = fila.get(key_bytes)
        if not data:
            continue
        nombre = fila.get(key_nombre) or f"{numero}_{tipo}.{tipo}"
        try:
            if tipo == "pdf":
                url, file_id = strapi.subir_pdf_con_id(data, nombre)
                # Vincular el archivo por su id aunque la URL venga vacía (algunos
                # proveedores/media no devuelven url sincrónica): así el PDF queda
                # adjunto a la causa igual.
                if file_id and key_file_id:
                    fila[key_file_id] = file_id
                if url:
                    fila[key_url] = url
                if file_id or url:
                    log.info(f"Subido {nombre} (id={file_id})", job_id=job_id, causa=numero)
                else:
                    log.warn(f"Strapi no aceptó la subida de {nombre} (sin id ni URL)", job_id=job_id, causa=numero)
            else:
                url = strapi.subir_docx(data, nombre)
                if url:
                    fila[key_url] = url
                    # Ya subido: la re-extracción vuelve a llamar esta función por
                    # nivel y, sin esto, subía otra vez la ficha del tribunal.
                    fila.pop(key_bytes, None)
                    log.info(f"Subido {nombre}", job_id=job_id, causa=numero)
                else:
                    log.warn(f"Strapi devolvió URL vacía para {nombre}", job_id=job_id, causa=numero)
        except Exception as e:
            log.error(f"Upload {nombre}: {type(e).__name__}: {e}", job_id=job_id, causa=numero)


def _now():
    return datetime.now(timezone.utc)
