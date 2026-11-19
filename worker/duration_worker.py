"""
Worker de cálculo de duración de casos.

Mide cuánto tardó un caso desde su primera actuación registrada en el SATJE
(la más antigua, generalmente una notificación o acta de sorteo) hasta la
actuación que produjo la sentencia.

No requiere IA — solo consulta el SATJE para leer fechas de actuaciones.

Puede correr de dos formas:
  1. Inline durante la extracción normal: extractor.py ya calcula los campos
     _fecha_inicio_caso / _fecha_sentencia_caso / _duracion_dias y job_manager
     los persiste al insertar en worker_caso_log.

  2. Como backfill independiente: backfill_duraciones() lee worker_caso_log,
     busca casos con sentencia que no tienen duracion_dias, consulta el SATJE
     y actualiza la fila.
"""

import sys
import time
import threading
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from api import (
    normalizar_numero, obtener_incidentes, obtener_actuaciones,
    _fecha_iso, _es_razon_notificacion,
)
from resolucion.satje.tribunal import clasificar_cogep, es_incidente_primera_instancia
from worker import job_manager as jm
from worker import numeracion

PAUSA_SEG_BACKFILL = 1.5  # pausa entre causas para no saturar el SATJE


# ── Estado del backfill en memoria ────────────────────────────────────────────

_backfill_lock   = threading.Lock()
_backfill_estado = {
    "corriendo":   False,
    "procesados":  0,
    "actualizados": 0,
    "errores":     0,
    "total":       0,
    "iniciado_en": None,
    "ultimo_error": None,
}


def estado_backfill() -> dict:
    with _backfill_lock:
        return dict(_backfill_estado)


# ── Lógica principal ──────────────────────────────────────────────────────────

def calcular_duracion_numero(numero: str) -> dict | None:
    """
    Consulta el SATJE para `numero` y devuelve un dict con:
        {"fecha_inicio": "2019-03-15", "fecha_sentencia": "2022-10-30", "duracion_dias": 1324}
    o None si no se puede calcular (causa no existe, sin actuaciones, sin sentencia).

    Esta función es la misma lógica que usa extractor.py en el flujo normal,
    pero opera de forma independiente para el backfill.
    """
    if not normalizar_numero(numero):
        return None
    # El idJuicio exacto del juicio de la materia (puede llevar sufijo "G").
    from api import buscar_causa, id_juicio_de
    causa = buscar_causa(numero)
    if not causa:
        return None
    id_juicio = id_juicio_de(causa, numero)

    incidentes = obtener_incidentes(id_juicio)
    if not incidentes:
        return None

    incs_primera = [
        i for i in incidentes
        if es_incidente_primera_instancia(i)
    ]
    if not incs_primera:
        return None

    act_resolucion = None
    todas_las_actuaciones: list = []

    for inc in incs_primera:
        acts = obtener_actuaciones(id_juicio, inc)
        if not acts:
            continue
        if not todas_las_actuaciones:
            todas_las_actuaciones = acts  # fallback para fecha de inicio

        for act in acts:
            if _es_razon_notificacion(act.get("tipo", "")):
                continue
            tipo      = act.get("tipo", "").strip()
            actividad = act.get("actividad", "")
            motivo    = clasificar_cogep(tipo, actividad)
            if motivo and "Con sentencia" in motivo:
                act_resolucion = act
                todas_las_actuaciones = acts
                break
        if act_resolucion:
            break

    if not act_resolucion or not todas_las_actuaciones:
        return None

    fecha_sentencia_dt = _fecha_iso(act_resolucion.get("fecha", ""))
    if not fecha_sentencia_dt:
        return None

    actas_con_fecha = [a for a in todas_las_actuaciones if _fecha_iso(a.get("fecha", ""))]
    if not actas_con_fecha:
        return None

    # actuaciones vienen DESC — la más antigua es la última de la lista
    fecha_inicio_dt = _fecha_iso(actas_con_fecha[-1].get("fecha", ""))
    if not fecha_inicio_dt:
        return None

    duracion = max(0, (fecha_sentencia_dt.date() - fecha_inicio_dt.date()).days)
    return {
        "fecha_inicio":    actas_con_fecha[-1]["fecha"][:10],
        "fecha_sentencia": act_resolucion["fecha"][:10],
        "duracion_dias":   duracion,
    }


def backfill_duraciones(job_id: int | None = None, dry_run: bool = False) -> dict:
    """
    Calcula y persiste duracion_dias para todos los casos en worker_caso_log
    que tienen sentencia pero les falta el campo de duración.

    job_id: si se especifica, solo procesa ese job; None = todos los jobs.
    dry_run: si True, calcula pero no escribe en la BD.

    Devuelve estadísticas de la pasada: {"procesados": N, "actualizados": N, "errores": N}.
    """
    with _backfill_lock:
        if _backfill_estado["corriendo"]:
            return {"error": "Backfill ya está corriendo"}
        _backfill_estado.update({
            "corriendo": True, "procesados": 0, "actualizados": 0,
            "errores": 0, "total": 0, "iniciado_en": datetime.utcnow().isoformat(),
            "ultimo_error": None,
        })

    try:
        pendientes = jm.listar_casos_sin_duracion(job_id=job_id)
        with _backfill_lock:
            _backfill_estado["total"] = len(pendientes)

        print(f"[duration_worker] {len(pendientes)} casos sin duración a procesar"
              + (f" (job {job_id})" if job_id else ""))

        for caso in pendientes:
            numero  = caso["numero"]
            caso_id = caso["id"]
            try:
                resultado = calcular_duracion_numero(numero)
                if resultado:
                    if not dry_run:
                        jm.actualizar_duracion_caso(
                            caso_id,
                            resultado["fecha_inicio"],
                            resultado["fecha_sentencia"],
                            resultado["duracion_dias"],
                        )
                    with _backfill_lock:
                        _backfill_estado["actualizados"] += 1
                    print(f"  ✓ {numero}: {resultado['duracion_dias']} días")
                else:
                    print(f"  – {numero}: sin datos de duración")
            except Exception as e:
                with _backfill_lock:
                    _backfill_estado["errores"]     += 1
                    _backfill_estado["ultimo_error"] = f"{numero}: {e}"
                print(f"  [ERROR] {numero}: {e}")

            with _backfill_lock:
                _backfill_estado["procesados"] += 1

            time.sleep(PAUSA_SEG_BACKFILL)

        with _backfill_lock:
            resultado_final = {
                "procesados":  _backfill_estado["procesados"],
                "actualizados": _backfill_estado["actualizados"],
                "errores":     _backfill_estado["errores"],
                "total":       _backfill_estado["total"],
            }

        print(f"[duration_worker] Backfill completo: {resultado_final}")
        return resultado_final

    finally:
        with _backfill_lock:
            _backfill_estado["corriendo"] = False


def ejecutar_job_duracion(job_id: int, tribunal: str, año: str, desde: int,
                          hasta: int | None, pause_ev: threading.Event,
                          kill_ev: threading.Event):
    """
    Loop principal de un job de solo duración.
    Itera el rango de causas, calcula la duración de cada una consultando el SATJE
    y persiste el resultado sin IA ni PDFs.
    Corre en el hilo de extracción como sustituto del loop normal de runner.py.
    """
    from config.config import TRIBUNALES, PAUSA_SEG
    nombre_tribunal = TRIBUNALES.get(tribunal, tribunal)

    inicio = datetime.now(timezone.utc)
    n = desde
    vacias_consec = 0
    MAX_VACIAS = 10  # parar tras N causas consecutivas sin resultados

    while True:
        if kill_ev.is_set():
            jm._actualizar(job_id, estado="cancelado", completado_en=_now())
            return
        if pause_ev.is_set():
            jm._actualizar(job_id, estado="pausado")
            return
        if hasta and n > hasta:
            break

        numero = numeracion.numero(tribunal, año, n)
        exitosa = False
        omitida = False
        es_error = False
        resultado = None

        try:
            resultado = calcular_duracion_numero(numero)
            if resultado:
                # 1. Actualizar todos los registros existentes con este número
                jm.actualizar_duracion_todos(
                    numero,
                    resultado["fecha_inicio"],
                    resultado["fecha_sentencia"],
                    resultado["duracion_dias"],
                )
                # 2. Insertar registro propio de este job (sin duplicar si ya existe para este job)
                fila = {
                    "_tribunal_codigo": tribunal,
                    "Tribunal":         nombre_tribunal,
                    "Número de juicio": numero,
                    "Año":              año,
                    "Estado de Juicio": "Con sentencia (duración)",
                    "Resultado tribunal": "Con sentencia.",
                    "_fecha_inicio_caso":   resultado["fecha_inicio"],
                    "_fecha_sentencia_caso": resultado["fecha_sentencia"],
                    "_duracion_dias":        resultado["duracion_dias"],
                }
                jm.registrar_caso(job_id, fila)
                exitosa = True
                vacias_consec = 0
                print(f"  ✓ {numero}: {resultado['duracion_dias']} días")
            else:
                omitida = True
                vacias_consec += 1
                if vacias_consec >= MAX_VACIAS:
                    break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            es_error = True
            vacias_consec = 0
            print(f"  [ERROR] {numero}: {e}")

        elapsed = (datetime.now(timezone.utc) - inicio).total_seconds()
        job_bd  = jm.obtener_job(job_id)
        procesadas = (job_bd["total_procesadas"] or 0) + 1

        evento = {
            "tipo":      "progreso",
            "n":         n,
            "causa":     numero,
            "estado":    "Con sentencia." if exitosa else ("Sin datos" if omitida else "Error"),
            "resultado": f"{resultado['duracion_dias']} días" if (exitosa and resultado) else "",
            "resultado_cnj":  "",
            "completitud":    "",
            "procesadas":     procesadas,
            "exitosas":       (job_bd["exitosas"] or 0) + (1 if exitosa else 0),
            "omitidas":       (job_bd["omitidas"] or 0) + (1 if omitida else 0),
            "errores":        (job_bd["errores"] or 0) + (1 if es_error else 0),
            "strapi_fallo":   False,
            "checkpoint":     n,
            "elapsed_seg":    int(elapsed),
            "eta_seg":        None,
        }
        jm.reportar_progreso(job_id, n, exitosa, evento, omitida=omitida)

        n += 1
        time.sleep(PAUSA_SEG)

    ts_completado = _now()
    jm._actualizar(job_id, estado="completado", completado_en=ts_completado)
    jm._emitir_sse(job_id, {"tipo": "estado", "estado": "completado",
                             "completado_en": ts_completado.isoformat()})


def _now():
    return datetime.now(timezone.utc)


def lanzar_backfill_async(job_id: int | None = None, dry_run: bool = False):
    """Lanza backfill_duraciones en un hilo daemon para no bloquear la API."""
    t = threading.Thread(
        target=backfill_duraciones,
        kwargs={"job_id": job_id, "dry_run": dry_run},
        daemon=True,
        name="duration-backfill",
    )
    t.start()
    return t
