"""
Módulo de logging centralizado del worker.

Tres canales:
  1. Render (stdout)     — para el equipo técnico. Siempre se escribe.
  2. Cliente (SSE)        — para la webapp en vivo. Solo si se pasa job_id.
  3. BD (worker_job_logs) — historial persistente por job, para poder
     auditar un job desde la webapp aunque se cambie de pestaña/se cierre
     el navegador (GET /jobs/{id}/logs, paginado). Solo si se pasa job_id.

Uso:
    from worker.logger import log, info, warn, error

    log("INFO",  "Causa procesada OK",              job_id=3, causa="09501-2019-00010")
    info("Iniciando extracción",                     job_id=3)
    warn("PDF vacío, reintentando",                  job_id=3, causa=numero)
    error("Strapi rechazó el payload: HTTP 400",     job_id=3, causa=numero)

Niveles: DEBUG < INFO < WARN < ERROR

Variables de entorno opcionales:
  LOG_LEVEL      — nivel mínimo para stdout   (default: INFO)
  LOG_LEVEL_SSE  — nivel mínimo para cliente  (default: INFO)
  LOG_LEVEL_DB   — nivel mínimo para BD       (default: WARN; un INSERT por línea INFO saturaba la base)
"""

import os
import sys
import threading
from datetime import datetime, timezone

# ── Niveles ───────────────────────────────────────────────────────────────────

_NIVELES = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

_NIVEL_STDOUT = _NIVELES.get(os.environ.get("LOG_LEVEL",     "INFO").upper(), 20)
_NIVEL_SSE    = _NIVELES.get(os.environ.get("LOG_LEVEL_SSE", "INFO").upper(), 20)

# Prefijos ANSI para stdout (Render los muestra en color en algunos entornos)
_PREFIJOS = {
    "DEBUG": "  ",
    "INFO":  "  ",
    "WARN":  "  [!]",
    "ERROR": "  [✗]",
}

_stdout_lock = threading.Lock()


# ── Función principal ─────────────────────────────────────────────────────────

_NIVEL_DB = _NIVELES.get(os.environ.get("LOG_LEVEL_DB", "WARN").upper(), 30)

def log(nivel: str, mensaje: str, *, job_id: int = None, causa: str = None) -> None:
    """
    Escribe un log en stdout y, si se proporciona job_id, emite también
    un evento SSE de tipo 'log' al cliente suscrito a ese job.
    Adicionalmente persiste WARN+ en worker_job_logs (BD).

    Canales independientes: stdout siempre se escribe;
    SSE solo si hay suscriptores; BD para WARN+.
    """
    nivel = nivel.upper()
    nivel_num = _NIVELES.get(nivel, 20)
    ts_dt = datetime.now(timezone.utc)
    ts = ts_dt.strftime("%Y-%m-%d %H:%M:%S")

    # ── Canal 1: Render (stdout) ──────────────────────────────────────────────
    # Sin timestamp propio: Render ya antepone fecha/hora a cada línea de stdout.
    if nivel_num >= _NIVEL_STDOUT:
        prefijo   = _PREFIJOS.get(nivel, "  ")
        job_tag   = f"[J{job_id}] " if job_id is not None else ""
        causa_tag = f"{causa} → " if causa else ""
        linea     = f"{job_tag}[{nivel}]{prefijo} {causa_tag}{mensaje}"
        with _stdout_lock:
            print(linea, file=sys.stdout, flush=True)

    # ── Canal 2: Cliente (SSE) ────────────────────────────────────────────────
    if job_id is not None and nivel_num >= _NIVEL_SSE:
        _emit_sse(job_id, {
            "tipo":  "log",
            "nivel": nivel,
            "ts":    ts,
            "causa": causa or "",
            "msg":   mensaje,
        })

    # ── Canal 3: BD (historial por job) ───────────────────────────────────────
    if job_id is not None and nivel_num >= _NIVEL_DB:
        try:
            from worker.job_manager import insertar_log
            insertar_log(job_id, nivel, causa, mensaje, ts_dt)
        except Exception:
            pass


def _emit_sse(job_id: int, evento: dict) -> None:
    """Emite al bus SSE del job_manager. Silencia cualquier error para no
    interrumpir el flujo de extracción si SSE no está disponible."""
    try:
        from worker.job_manager import _emitir_sse
        _emitir_sse(job_id, evento)
    except Exception:
        pass


# ── Atajos por nivel ──────────────────────────────────────────────────────────

def debug(mensaje: str, *, job_id: int = None, causa: str = None) -> None:
    log("DEBUG", mensaje, job_id=job_id, causa=causa)

def info(mensaje: str, *, job_id: int = None, causa: str = None) -> None:
    log("INFO",  mensaje, job_id=job_id, causa=causa)

def warn(mensaje: str, *, job_id: int = None, causa: str = None) -> None:
    log("WARN",  mensaje, job_id=job_id, causa=causa)

def error(mensaje: str, *, job_id: int = None, causa: str = None) -> None:
    log("ERROR", mensaje, job_id=job_id, causa=causa)


# ── Log de sistema (sin job, solo stdout) ────────────────────────────────────

def sistema(nivel: str, mensaje: str) -> None:
    """Para logs de arranque/shutdown y eventos que no pertenecen a ningún job."""
    log(nivel, mensaje, job_id=None, causa=None)
