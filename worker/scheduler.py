"""
Scheduler de ejecuciones programadas (cron) para el worker.

Usa APScheduler 3.x con BackgroundScheduler. Cada schedule almacenado en
worker_cron_schedules genera un job normal en worker_jobs que compite por
slots de categoría 'cron' igual que cualquier otro job.

Ciclo de vida:
  init_schedules()  → init_db ya creó la tabla; carga schedules activos y arranca
  apagar_scheduler() → llamado en lifespan shutdown
"""

import logging
import os
from typing import Optional

import psycopg2.extras
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from worker import job_manager as jm
from worker import logger as log

# Suprimir logs internos de APScheduler en stdout (ya tenemos nuestro logger)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

_scheduler: Optional[BackgroundScheduler] = None

# Cada cuanto se reconcilia la tabla de jobs contra los hilos reales.
VIGILANCIA_CADA_MIN = int(os.environ.get("JOB_VIGILANCIA_CADA_MIN", "5"))


# ── Arranque / parada ─────────────────────────────────────────────────────────

def init_schedules() -> None:
    """Arranca el scheduler y carga los schedules activos desde BD."""
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.start()

    # _registrar_job() exige _scheduler.running=True (ver su guard clause) —
    # por eso start() va ANTES de cargar los schedules, no después.
    try:
        asegurar_revision_automatica()
    except Exception as e:  # una variable mal escrita no debe tumbar el worker
        log.sistema("ERROR", f"REVISION_AUTOMATICA: {type(e).__name__}: {e}")
    schedules = listar_schedules(solo_activos=True)
    for s in schedules:
        _registrar_job(s)

    # Vigilante de jobs ESTANCADOS (hilo vivo pero sin avanzar). Los que se
    # quedaron sin hilo ya los recupera rescatar_huerfanos() desde la cola
    # automatica; este cubre el caso contrario, que ahi no se ve.
    _scheduler.add_job(
        jm.vigilar_jobs_estancados,
        trigger="interval",
        minutes=VIGILANCIA_CADA_MIN,
        id="vigilante_jobs",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    log.sistema("INFO", f"Scheduler iniciado con {len(schedules)} schedule(s) activo(s) "
                        f"+ vigilante cada {VIGILANCIA_CADA_MIN} min")


def apagar_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.sistema("INFO", "Scheduler detenido")


NOMBRE_REVISION_AUTO = "Revisión automática"


def asegurar_revision_automatica() -> None:
    """Deja el cron de la revisión diaria tal como diga REVISION_AUTOMATICA,
    para encenderlo desde Render sin clave de API ni Shell.
      {"cron": "0 6 * * *", "particiones": 4, "descarga": true}
    Vacía = no se toca nada; "off" = se borra el cron creado por esta vía.
    El cron va en UTC: 06:00 UTC es la 1:00 en Ecuador."""
    import json
    valor = os.environ.get("REVISION_AUTOMATICA", "").strip()
    if not valor:
        return
    previos = [x for x in listar_schedules() if x["nombre"] == NOMBRE_REVISION_AUTO]
    if valor.lower() == "off":
        for x in previos:
            eliminar_schedule(x["id"])
        return
    cfg = json.loads(valor)
    p = int(cfg.get("particiones", 4))
    if not 1 <= p <= 6:
        raise ValueError("particiones debe estar entre 1 y 6")
    workers = "revision::diaria" + (":descarga" if cfg.get("descarga", True) else "") + f":p={p}"
    cron = cfg.get("cron", "0 6 * * *")
    CronTrigger.from_crontab(cron, timezone="UTC")  # valida antes de guardar
    if any(x["cron_expr"] == cron and x["workers"] == workers and x["activo"] for x in previos):
        return
    for x in previos:
        eliminar_schedule(x["id"])
    crear_schedule(NOMBRE_REVISION_AUTO, "-", "0000", 0, None, "-", cron,
                   workers=workers, categoria="extraccion")


# ── CRUD de schedules ─────────────────────────────────────────────────────────

def crear_schedule(nombre: str, tribunal: str, año: str, desde: int,
                   hasta: Optional[int], modelo: str, cron_expr: str,
                   workers: str = "", categoria: str = "cron") -> dict:
    """`workers` define QUÉ job dispara: vacío = extracción normal; también
    "embeddings[:modo]", "brief[:seccion]" o "revision[:año][:vivas]".

    `categoria` decide con quién compite por slots: "cron" tiene su propio
    cupo de 1, "extraccion" comparte los 4 con las extracciones.

    OJO con la hora: el scheduler corre en UTC. Ecuador es UTC-5, así que
    medianoche local es "0 5 * * *", no "0 0 * * *".
    """
    with jm._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO worker_cron_schedules
                    (nombre, tribunal, año, desde, hasta, modelo, cron_expr,
                     workers, categoria)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
            """, (nombre, tribunal, año, desde, hasta, modelo, cron_expr,
                  workers, categoria))
            row = dict(cur.fetchone())
        conn.commit()

    if _scheduler and _scheduler.running:
        _registrar_job(row)

    log.sistema("INFO", f"Schedule creado: {nombre} ({cron_expr})")
    return row


def listar_schedules(solo_activos: bool = False) -> list[dict]:
    import psycopg2.extras
    filtro = "WHERE activo = true" if solo_activos else ""
    with jm._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT * FROM worker_cron_schedules {filtro} ORDER BY id ASC")
            return [dict(r) for r in cur.fetchall()]


def toggle_schedule(schedule_id: int, activo: bool) -> dict:
    import psycopg2.extras
    with jm._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "UPDATE worker_cron_schedules SET activo = %s WHERE id = %s RETURNING *",
                (activo, schedule_id)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Schedule {schedule_id} no encontrado")
            row = dict(row)
        conn.commit()

    job_key = f"cron-{schedule_id}"
    if _scheduler and _scheduler.running:
        if activo:
            _registrar_job(row)
        else:
            try:
                _scheduler.remove_job(job_key)
            except Exception:
                pass

    return row


def eliminar_schedule(schedule_id: int) -> None:
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM worker_cron_schedules WHERE id = %s", (schedule_id,))
        conn.commit()

    if _scheduler and _scheduler.running:
        try:
            _scheduler.remove_job(f"cron-{schedule_id}")
        except Exception:
            pass


# ── Internos ──────────────────────────────────────────────────────────────────

def _registrar_job(schedule: dict) -> None:
    """Registra un schedule en APScheduler. Llama _disparar_cron en cada tick."""
    if not (_scheduler and _scheduler.running):
        return
    job_key = f"cron-{schedule['id']}"
    try:
        _scheduler.remove_job(job_key)
    except Exception:
        pass
    _scheduler.add_job(
        _disparar_cron,
        trigger=CronTrigger.from_crontab(schedule["cron_expr"], timezone="UTC"),
        args=[schedule["id"]],
        id=job_key,
        replace_existing=True,
        name=schedule["nombre"],
    )


def _disparar_cron(schedule_id: int) -> None:
    """Crea un job real con categoría 'cron' cuando el scheduler dispara."""
    import psycopg2.extras
    with jm._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM worker_cron_schedules WHERE id = %s AND activo = true",
                (schedule_id,)
            )
            row = cur.fetchone()
    if not row:
        return
    s = dict(row)

    workers = s.get("workers") or ""
    categoria = s.get("categoria") or "cron"

    # "p=N" en un schedule de revisión lo expande en N jobs, uno por grupo de
    # tribunales, para que corran en paralelo en los 4 slots. Medido en
    # Render: ~9 números/minuto, así que una revisión diaria entera en un
    # solo job son ~10 h y se solapa con los crons de la mañana. Repartida
    # baja a ~4 h.
    marca_p = next((x for x in workers.split(":") if x.startswith("p=")), None)
    if workers.startswith("revision") and marca_p:
        from worker.revision_worker import repartir_tribunales
        try:
            n = int(marca_p[2:])
        except ValueError:
            n = 1
        base = ":".join(x for x in workers.split(":") if not x.startswith("p="))
        grupos = repartir_tribunales(n)
        creados = []
        for i, grupo in enumerate(grupos, 1):
            creados.append(jm.crear_job(
                tribunal="-", año=s["año"], desde=0, hasta=None, dry_run=False,
                modelo=s["modelo"],
                iniciado_por=f"cron:{s['nombre']} {i}/{len(grupos)}",
                workers=f"{base}:t=" + ",".join(grupo),
                categoria=categoria,
                schedule_id=schedule_id,
            ))
        with jm._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE worker_cron_schedules SET ultimo_run = NOW() WHERE id = %s",
                            (schedule_id,))
            conn.commit()
        log.sistema("INFO", f"Cron disparó: {s['nombre']} → {len(creados)} job(s) "
                            f"{[j['id'] for j in creados]}")
        return

    job = jm.crear_job(
        tribunal     = s["tribunal"],
        año          = s["año"],
        desde        = s["desde"],
        hasta        = s.get("hasta"),
        dry_run      = False,
        modelo       = s["modelo"],
        iniciado_por = f"cron:{s['nombre']}",
        workers      = workers,
        categoria    = categoria,
        schedule_id  = schedule_id,
    )

    # Marca último run
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_cron_schedules SET ultimo_run = NOW() WHERE id = %s",
                (schedule_id,)
            )
        conn.commit()

    log.sistema("INFO", f"Cron disparó: schedule={s['nombre']} → job #{job['id']}")
