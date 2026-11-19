"""
Worker genérico de backfill por lote (no por rango de número de causa).

A diferencia de runner.py (itera tribunal+año+desde..hasta) o
duration_worker.py (idem, pero sin IA), este worker procesa un LOTE de
tamaño fijo de causas que YA están en worker_causa_datos con
faltante_<campo>=true — sin que el usuario elija cuáles: siempre agarra
las próximas, priorizando las nunca intentadas (job_manager.
listar_pendientes_backfill). Cada corrida completa su lote y termina;
para seguir, se lanza otra corrida — las causas ya resueltas dejan de
aparecer en la consulta, así que no hace falta un cursor/offset manual.

Handlers por campo — agregar uno nuevo acá a medida que se construya:
  "abogados"       -> _backfill_abogados_una_causa (via resolucion/ai/abogados_vision.py)
  "jurisprudencia" -> _backfill_jurisprudencia_una_causa (prompts 3 y 6, tribunal y CNJ)
"""
import time
from datetime import datetime, timezone

from api import obtener_incidentes, normalizar_numero, buscar_causa, id_juicio_de
from resolucion.satje.tribunal import obtener_escritos_pdf, buscar_actuacion_resolucion, descargar_doc_actuacion
from resolucion.satje.corte import (
    encontrar_incidente_cnj, obtener_actuaciones_cnj,
    detectar_sentencia_cnj, DESCRIPCIONES_SUBSTANTIVAS,
)
from resolucion.ai.abogados_vision import completar_abogados_con_escritos
from resolucion.ai.tribunal import analizar_solo_jurisprudencia
from resolucion.ai.corte import analizar_solo_jurisprudencia_cnj
from worker import job_manager as jm
from worker import logger as log
from worker.strapi_client import StrapiClient, _split_jurisprudencia

_MODELO_JURISPRUDENCIA = "Qwen/Qwen3-235B-A22B-Instruct-2507"

PAUSA_SEG = 1.0


def _now():
    return datetime.now(timezone.utc)


# ── Handler: abogados ────────────────────────────────────────────────────────

def _backfill_abogados_una_causa(numero: str, actor: str, demandado: str, job_id: int) -> dict:
    """
    No vuelve a correr Prompt 2 sobre la sentencia (ya sabemos que ahí no
    estaba, por eso la causa está en la cola de backfill) — va directo
    al fallback de escritos + visión, que es la parte barata.
    """
    causa = buscar_causa(numero)
    if not causa:
        return {"actualizado": False, "detalle": "causa no encontrada en SATJE"}

    id_juicio = id_juicio_de(causa, numero)
    incidentes = obtener_incidentes(id_juicio)
    if not incidentes:
        return {"actualizado": False, "detalle": "sin incidentes en SATJE"}

    escritos = obtener_escritos_pdf(id_juicio, incidentes, job_id=job_id, causa=numero)
    if not escritos:
        return {"actualizado": False, "detalle": "sin escritos descargables"}

    completados = completar_abogados_con_escritos(
        escritos, actor=actor or "", demandado=demandado or "",
        falta_actor=True, falta_demandado=True,
        job_id=job_id, causa=numero,
    )
    if not completados:
        return {"actualizado": False, "detalle": "no se identificó ningún abogado en los escritos"}

    jm.actualizar_abogados_causa(
        numero,
        abogado_actor=completados.get("abogado_actor"),
        abogado_demandado=completados.get("abogado_demandado"),
    )
    detalle = ", ".join(f"{k}={v}" for k, v in completados.items())
    return {"actualizado": True, "detalle": detalle}


# ── Handler: jurisprudencia ───────────────────────────────────────────────────

def _backfill_jurisprudencia_una_causa(numero: str, actor: str, demandado: str, job_id: int) -> dict:
    """
    Re-corre SOLO el prompt de jurisprudencia — tribunal (prompt 3) y,
    si la causa llegó a casación, también CNJ (prompt 6) — y actualiza
    ÚNICAMENTE jurisprudencia_mencionada/criterio_sala_para_resolver en
    Strapi, sin tocar ningún otro campo del registro (ni de causa ni de
    casación). Pensado para causas que quedaron con "No consta" antes
    de un ajuste al prompt — no hace falta re-correr resumen/extracción/
    clasificación, que ya sabemos que están bien.
    """
    causa = buscar_causa(numero)
    if not causa:
        return {"actualizado": False, "detalle": "causa no encontrada en SATJE"}

    id_juicio = id_juicio_de(causa, numero)
    incidentes = obtener_incidentes(id_juicio)
    if not incidentes:
        return {"actualizado": False, "detalle": "sin incidentes en SATJE"}

    tribunal = numero.split("-")[0]
    resultado_tribunal, act_resolucion, actuaciones = buscar_actuacion_resolucion(
        id_juicio, incidentes, tribunal, job_id=job_id, causa=numero
    )
    if not (act_resolucion and "Con sentencia" in resultado_tribunal):
        return {"actualizado": False, "detalle": "sin sentencia de tribunal"}

    pdf_bytes, _ = descargar_doc_actuacion(act_resolucion, id_juicio, "sentencia")
    if not pdf_bytes:
        return {"actualizado": False, "detalle": "no se pudo descargar el PDF de la sentencia"}

    cliente = StrapiClient()
    actualizado_algo = False
    detalles = []

    r_trib = analizar_solo_jurisprudencia(pdf_bytes, numero, modelo=_MODELO_JURISPRUDENCIA, job_id=job_id)
    if r_trib.get("error"):
        detalles.append(f"tribunal: error ({r_trib['error'][:80]})")
    else:
        citada, criterio = _split_jurisprudencia(r_trib.get("jurisprudencia", ""))
        if citada or criterio:
            if cliente.actualizar_jurisprudencia_causa(numero, citada, criterio):
                jm.actualizar_flag_jurisprudencia(numero)
                actualizado_algo = True
                detalles.append("tribunal: actualizado")
            else:
                detalles.append("tribunal: no se encontró la causa en Strapi")
        else:
            detalles.append("tribunal: sigue sin jurisprudencia")

    # CNJ, solo si esta causa efectivamente llegó a casación con sentencia sustantiva.
    inc_cnj = encontrar_incidente_cnj(incidentes)
    if inc_cnj:
        actuaciones_cnj = obtener_actuaciones_cnj(id_juicio, inc_cnj)
        desc_cnj, act_cnj = detectar_sentencia_cnj(actuaciones_cnj)
        if desc_cnj in DESCRIPCIONES_SUBSTANTIVAS and act_cnj:
            pdf_cnj, _ = descargar_doc_actuacion(act_cnj, id_juicio, "sentencia_cnj")
            if pdf_cnj:
                r_cnj = analizar_solo_jurisprudencia_cnj(
                    pdf_cnj, numero, modelo=_MODELO_JURISPRUDENCIA, job_id=job_id
                )
                if r_cnj.get("error"):
                    detalles.append(f"CNJ: error ({r_cnj['error'][:80]})")
                else:
                    citada_cnj, criterio_cnj = _split_jurisprudencia(r_cnj.get("jurisprudencia", ""))
                    if citada_cnj or criterio_cnj:
                        if cliente.actualizar_jurisprudencia_casacion(numero, citada_cnj, criterio_cnj):
                            actualizado_algo = True
                            detalles.append("CNJ: actualizado")
                        else:
                            detalles.append("CNJ: no se encontró la casación en Strapi")
                    else:
                        detalles.append("CNJ: sigue sin jurisprudencia")

    return {"actualizado": actualizado_algo, "detalle": "; ".join(detalles) or "sin cambios"}


_HANDLERS = {
    "abogados":       _backfill_abogados_una_causa,
    "jurisprudencia": _backfill_jurisprudencia_una_causa,
}


# ── Loop principal ────────────────────────────────────────────────────────────

def ejecutar_job_backfill(job_id: int, campo: str, lote: int, pause_ev, kill_ev, modo: str = "completar",
                          tribunal: str = None, año: str = None, desde: int = None, hasta: int = None):
    handler = _HANDLERS.get(campo)
    if not handler:
        log.error(f"Sin handler de backfill para campo '{campo}'", job_id=job_id)
        jm._actualizar(job_id, estado="error", completado_en=_now())
        return

    pendientes = jm.listar_pendientes_backfill(campo, lote, modo=modo,
                                               tribunal=tribunal, año=año, desde=desde, hasta=hasta)
    total_lote = len(pendientes)
    rango_desc = (f", rango {tribunal}-{año}-{desde:05d}..{hasta or '∞'}"
                  if tribunal and tribunal != "-" and desde is not None else "")
    log.info(f"Backfill '{campo}' (modo={modo}{rango_desc}): {total_lote} causa(s) en este lote", job_id=job_id)

    inicio = _now()
    procesados = 0
    actualizados = 0

    for causa in pendientes:
        if kill_ev.is_set():
            jm._actualizar(job_id, estado="cancelado", completado_en=_now())
            _guardar_stats(campo, procesados, inicio)
            return
        if pause_ev.is_set():
            jm._actualizar(job_id, estado="pausado")
            _guardar_stats(campo, procesados, inicio)
            return

        numero = causa["numero"]
        try:
            r = handler(numero, causa.get("actor"), causa.get("demandado"), job_id)
        except Exception as e:
            r = {"actualizado": False, "detalle": f"error: {type(e).__name__}: {e}"}
            log.error(f"Backfill '{campo}' — {numero}: {e}", job_id=job_id, causa=numero)

        jm.registrar_intento_backfill(numero, campo)
        if r["actualizado"]:
            actualizados += 1
            log.info(f"{numero}: {r['detalle']}", job_id=job_id, causa=numero)
        else:
            log.info(f"{numero}: sin cambios — {r['detalle']}", job_id=job_id, causa=numero)

        procesados += 1
        elapsed = (_now() - inicio).total_seconds()
        jm._actualizar(job_id, total_procesadas=procesados, exitosas=actualizados)
        jm._emitir_sse(job_id, {
            "tipo": "progreso",
            "causa": numero,
            "estado": "Actualizado" if r["actualizado"] else "Sin cambios",
            "resultado": r["detalle"],
            "procesadas": procesados,
            "total_lote": total_lote,
            "exitosas": actualizados,
            "elapsed_seg": int(elapsed),
        })

        time.sleep(PAUSA_SEG)

    _guardar_stats(campo, procesados, inicio)
    ts_completado = _now()
    jm._actualizar(job_id, estado="completado", completado_en=ts_completado)
    jm._emitir_sse(job_id, {
        "tipo": "estado", "estado": "completado",
        "completado_en": ts_completado.isoformat(),
        "resumen": f"{actualizados}/{procesados} actualizadas",
    })
    log.info(f"Backfill '{campo}' completo: {actualizados}/{procesados} actualizadas", job_id=job_id)


def _guardar_stats(campo: str, procesados: int, inicio: datetime) -> None:
    if procesados > 0:
        segundos = (_now() - inicio).total_seconds()
        jm.registrar_stats_backfill(campo, procesados, segundos)
