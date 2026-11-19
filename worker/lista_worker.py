"""
Worker de LISTA personalizada (motor).

A diferencia de runner.py (barre tribunal+año+desde..hasta), procesa un conjunto
PERSONALIZADO de causas (worker_lista_items), cada una con flags de qué extraer:
  - do_tribunal → corre el motor de tribunal y guarda/actualiza la causa.
  - do_corte    → corre el motor de corte y guarda la casación vinculada.

Casos que resuelve:
  - tribunal (o ambos): flujo normal — extrae y guarda la causa; si hay corte, la
    casación cuelga de esa causa.
  - solo corte, causa YA existe: agrega la casación SIN sobrescribir la sentencia
    de tribunal (no se llama guardar_causa).
  - solo corte, causa NO existe: crea una causa "cascarón" (identidad + motivo de
    por qué no hubo sentencia de tribunal) y le cuelga la casación.

Es SOLO el motor: qué extraer se le indica en la lista. La lógica de revisión /
contraste (decidir qué falta) va aparte.
"""
import time
import traceback
from datetime import datetime, timezone

from config.config import PROJECT_ROOT, TRIBUNALES
from resolucion.orquestador import procesar_causa
from resolucion.cms import tribunal as cms_tribunal
from resolucion.cms import corte as cms_corte
from resolucion.cms import apelacion as cms_apelacion
from worker import job_manager as jm
from worker import logger as log
from worker.runner import _subir_archivos_strapi
from worker.strapi_client import StrapiClient

PAUSA_SEG = 1.0


def _now():
    return datetime.now(timezone.utc)


def _persistir_extras_tribunal(fila: dict, causa_doc_id: str, job_id: int,
                               modelo: str, flujo_proveedores: str):
    """Pasos post-guardado del modo completo (runner) que también deben correr
    en la lista: tipos de defecto, evidencia ausente, extracción estructurada
    (prompt 10 — alimenta 2 de los 6 factores de jurimetría) y tipos de prueba
    (prompt 8). Cada uno es best-effort; un fallo no tumba el guardado."""
    if not causa_doc_id:
        return
    numero = fila.get("Número de juicio", "")
    if fila.get("_tipos_defecto"):
        try:
            jm.guardar_tipos_defecto(causa_doc_id, fila["_tipos_defecto"])
        except Exception as e:
            log.error(f"Tipos de defecto (interno) — {type(e).__name__}: {e}", job_id=job_id, causa=numero)
    if fila.get("_evidencia_ausente"):
        try:
            jm.guardar_evidencia_ausente(causa_doc_id, fila["_evidencia_ausente"])
        except Exception as e:
            log.error(f"Evidencia ausente (interno) — {type(e).__name__}: {e}", job_id=job_id, causa=numero)
    try:
        jm.clasificar_y_guardar_extraccion(
            causa_doc_id, modelo, numero=numero, job_id=job_id,
            flujo_proveedores=flujo_proveedores,
        )
    except Exception as e:
        log.error(f"Extraccion estructurada (interno) — {type(e).__name__}: {e}", job_id=job_id, causa=numero)
    try:
        jm.clasificar_y_guardar_tipos_prueba(causa_doc_id, modelo, numero=numero, job_id=job_id)
    except Exception as e:
        log.error(f"Tipos de prueba (interno) — {type(e).__name__}: {e}", job_id=job_id, causa=numero)


def _doc_id(resultado) -> str:
    return ((resultado or {}).get("data") or {}).get("documentId", "") if resultado else ""


def _tiene_sentencia(fila: dict) -> bool:
    return "Con sentencia" in (fila.get("Resultado tribunal", "") or "")


def _tiene_casacion(fila: dict) -> bool:
    return bool(fila.get("_resumen_cnj") or fila.get("_extraccion_cnj"))


def ejecutar_job_lista(job_id: int, pause_ev, kill_ev,
                       modelo: str, flujo_proveedores: str = "alibaba"):
    items = jm.listar_items_lista(job_id, solo_pendientes=True)
    if not items:
        log.info("Lista sin ítems pendientes", job_id=job_id)
        jm._actualizar(job_id, estado="completado", completado_en=_now())
        return

    corrida_id = f"lista-{job_id}"
    carpeta_pdfs = PROJECT_ROOT / "resultados" / "pdfs" / "resolucion" / corrida_id
    carpeta_words = PROJECT_ROOT / "resultados" / "words" / "resolucion" / corrida_id
    carpeta_pdfs.mkdir(parents=True, exist_ok=True)
    carpeta_words.mkdir(parents=True, exist_ok=True)

    strapi = StrapiClient()
    jm._actualizar(job_id, estado="corriendo", iniciado_en=_now())
    log.info(f"Iniciando job LISTA — {len(items)} causas, modelo={modelo}", job_id=job_id)

    procesadas = exitosas = errores = 0
    for idx, item in enumerate(items):
        if kill_ev.is_set():
            jm._actualizar(job_id, estado="cancelado", completado_en=_now())
            log.info("Lista cancelada", job_id=job_id)
            return
        if pause_ev.is_set():
            jm._actualizar(job_id, estado="pausado")
            log.info("Lista pausada", job_id=job_id)
            return

        juicio = (item["juicio"] or "").strip()
        do_trib, do_corte = item["do_tribunal"], item["do_corte"]
        tribunal = juicio.split("-")[0] if "-" in juicio else ""
        # El código de tribunal (09501) debe convertirse al NOMBRE para que
        # _lookup_tribunal enlace la relación causa_tribunal (busca por nombre).
        nombre_tribunal = TRIBUNALES.get(tribunal, tribunal)
        ctx = {
            "tribunal": tribunal, "nombre_tribunal": nombre_tribunal, "ano": "",
            "corrida_id": corrida_id, "carpeta_local": carpeta_pdfs,
            "carpeta_docx": carpeta_words,
            "gpt_ok": do_trib, "gpt_cnj_ok": do_corte, "skip_pdf": False,
            "modelo": modelo, "flujo_proveedores": flujo_proveedores, "job_id": job_id,
        }
        hecho = []
        try:
            fila = procesar_causa(juicio, ctx)
            fila["_tribunal_codigo"] = tribunal
            causa_doc_id = None
            error_pdf = bool(fila.get("_error_descarga_pdf"))
            error_cnj = bool(fila.get("_error_descarga_cnj"))

            # Sube PDFs/Word a Strapi y setea _file_id_* / _url_* en la fila (mismo
            # paso que el modo completo). SIN esto, las causas/casaciones NUEVAS
            # quedaban sin PDF ni Word adjunto, y la tabla "Archivos generados" del
            # detalle del job salía vacía. Solo sube lo que tenga bytes (una causa
            # solo-corte no trae PDF de tribunal, etc.).
            _subir_archivos_strapi(fila, strapi, job_id)

            # ── Tribunal ──
            if do_trib:
                if error_pdf:
                    # Guard 1 — el PDF SÍ estaba indicado pero la descarga falló tras
                    # los reintentos: NO se guarda una causa vacía; se deja PENDIENTE
                    # para reintentar en otra corrida (igual que runner.py).
                    hecho.append("tribunal PENDIENTE (descarga de PDF falló — reintentar)")
                elif _tiene_sentencia(fila):
                    causa_doc_id = _doc_id(cms_tribunal.guardar(fila, strapi, job_id))
                    if causa_doc_id:
                        hecho.append("tribunal ✓")
                        _persistir_extras_tribunal(fila, causa_doc_id, job_id,
                                                   modelo, flujo_proveedores)
                    else:
                        # El "✓" se escribía sin mirar si guardar() devolvió un
                        # documentId. Si Strapi respondía algo que no levantaba
                        # excepción pero tampoco creaba la causa, el ítem quedaba
                        # 'procesado · tribunal ✓' y la causa no existía en ningún
                        # lado. Visto en el job 107: 09501-2019-00117 marcada OK,
                        # ausente de la base. Un fallo que se reporta como éxito es
                        # peor que uno que falla: nadie lo reintenta.
                        hecho.append("tribunal FALLÓ (Strapi no devolvió documentId)")
                else:
                    motivo = (fila.get("Resultado tribunal", "") or "")[:40]
                    hecho.append(f"tribunal omitido (sin sentencia: {motivo})")

            # ── Corte ──
            if do_corte:
                if error_cnj:
                    # Guard 1 (corte) — PDF de la casación indicado pero descarga
                    # fallida: no se guarda casación vacía, se deja PENDIENTE.
                    hecho.append("corte PENDIENTE (descarga de PDF CNJ falló — reintentar)")
                elif _tiene_casacion(fila):
                    if not causa_doc_id:
                        existente = strapi._buscar_causa(juicio)
                        causa_doc_id = existente.get("documentId") if existente else None
                    if causa_doc_id:
                        # La casación cuelga de la causa (que SÍ tiene sentencia de
                        # tribunal, ya existente o recién extraída en este mismo ítem).
                        if fila.get("_resumen_apel") or fila.get("_extraccion_apel"):
                            cms_apelacion.guardar(fila, causa_doc_id, strapi, job_id)
                        cms_corte.guardar(fila, causa_doc_id, strapi, job_id)
                        hecho.append("corte ✓")
                    else:
                        # Política: NO se sube casación sin sentencia de tribunal.
                        # Si la causa no existe, se omite (no se crea cascarón).
                        hecho.append("corte omitido (sin sentencia de tribunal)")
                else:
                    hecho.append("corte omitido (sin casación sustantiva)")

            # Registro canónico (worker_causas / worker_causa_datos / worker_caso_log)
            # que alimenta la tabla "Archivos generados" del detalle del job y el
            # checklist de campos faltantes. Best-effort: no debe tumbar el ítem.
            try:
                jm.registrar_caso(job_id, fila)
            except Exception as e_reg:
                log.error(f"registrar_caso — {type(e_reg).__name__}: {e_reg}",
                          job_id=job_id, causa=juicio)

            detalle = " · ".join(hecho) or "sin acción"
            if any("PENDIENTE" in h or "FALLÓ" in h for h in hecho):
                # Falló la descarga del PDF, o el guardado no devolvió id: se
                # deja como error para re-correr, aunque el otro motor haya
                # guardado (el upsert es idempotente). Sin incluir "FALLÓ" acá,
                # un guardado fallido caía en la rama 'omitido' y se veía como
                # una decisión deliberada en vez de un problema.
                errores += 1
                jm.actualizar_item_lista(item["id"], "error", detalle)
            elif any("✓" in h for h in hecho):
                exitosas += 1
                jm.actualizar_item_lista(item["id"], "procesado", detalle)
            else:
                jm.actualizar_item_lista(item["id"], "omitido", detalle)
            log.info(f"{juicio} → {detalle}", job_id=job_id, causa=juicio)
        except Exception as e:
            errores += 1
            jm.actualizar_item_lista(item["id"], "error", f"{type(e).__name__}: {e}")
            log.error(f"{juicio} → {type(e).__name__}: {e}\n{traceback.format_exc()}",
                      job_id=job_id, causa=juicio)

        procesadas += 1
        jm._actualizar(job_id, checkpoint=idx + 1, total_procesadas=procesadas,
                       exitosas=exitosas, errores=errores)
        time.sleep(PAUSA_SEG)

    jm._actualizar(job_id, estado="completado", completado_en=_now())
    log.info(f"Lista completada — {exitosas} ok, {errores} error, {procesadas} total",
             job_id=job_id)
