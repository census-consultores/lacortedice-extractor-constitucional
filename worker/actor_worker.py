"""
Worker de clasificación de actores.

Clasifica y normaliza el campo Actor de las causas en Strapi como
'empresa' o 'persona_natural' usando reglas léxicas (sin IA).

Puede correr:
  1. Inline durante la extracción: extractor.py ya llama clasificar_actor.py
     y strapi_client.py lo persiste al guardar la causa.
  2. Como backfill: backfill_clasificacion_actores() procesa todas las causas
     en Strapi que tienen actor pero no tienen tipo_actor.
"""

import sys
import time
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from resolucion.clasificar_actor import clasificar_tipo_actor, normalizar_nombre_actor
from worker.strapi_client import StrapiClient

PAUSA_SEG = 0.3  # pausa entre requests a Strapi para no saturarlo

# ── Estado del backfill ────────────────────────────────────────────────────────

_lock = threading.Lock()
_estado = {
    "corriendo":   False,
    "procesados":  0,
    "actualizados": 0,
    "errores":     0,
    "total":       0,
    "iniciado_en": None,
    "ultimo_error": None,
}


def estado_backfill() -> dict:
    with _lock:
        return dict(_estado)


# ── Backfill ──────────────────────────────────────────────────────────────────

def backfill_clasificacion_actores(dry_run: bool = False) -> dict:
    """
    Recorre todas las causas en Strapi que tienen actor pero no tipo_actor
    y les asigna tipo_actor y actor_normalizado.

    dry_run=True: clasifica pero NO escribe en Strapi.
    """
    with _lock:
        if _estado["corriendo"]:
            return {"error": "Backfill ya está corriendo"}
        from datetime import datetime, timezone
        _estado.update({
            "corriendo": True, "procesados": 0, "actualizados": 0,
            "errores": 0, "total": 0,
            "iniciado_en": datetime.now(timezone.utc).isoformat(),
            "ultimo_error": None,
        })

    strapi = StrapiClient()

    try:
        page = 1
        total_pages = None

        while True:
            resultado = strapi.listar_causas_sin_clasificar(page=page, page_size=100)
            causas    = resultado.get("data", [])
            paginacion = resultado.get("meta", {}).get("pagination", {})

            if total_pages is None:
                total_pages = paginacion.get("pageCount", 1)
                # Estimación de total en primera pasada
                total_est = paginacion.get("total", 0)
                with _lock:
                    _estado["total"] = total_est

            if not causas:
                break

            for item in causas:
                attrs  = item.get("attributes", item)  # Strapi v4 vs v5
                numero = attrs.get("juicio", "")
                actor  = attrs.get("actor", "") or ""

                if not actor.strip():
                    with _lock:
                        _estado["procesados"] += 1
                    continue

                try:
                    tipo = clasificar_tipo_actor(actor)
                    norm = normalizar_nombre_actor(actor, tipo)

                    if not dry_run:
                        ok = strapi.actualizar_actor(numero, tipo, norm)
                        if ok:
                            with _lock:
                                _estado["actualizados"] += 1
                        else:
                            with _lock:
                                _estado["errores"]     += 1
                                _estado["ultimo_error"] = f"{numero}: no encontrado en Strapi"
                    else:
                        print(f"  [dry] {numero}: [{tipo}] {norm[:50]}")
                        with _lock:
                            _estado["actualizados"] += 1

                except Exception as e:
                    with _lock:
                        _estado["errores"]     += 1
                        _estado["ultimo_error"] = f"{numero}: {e}"
                    print(f"  [ERROR] {numero}: {e}")

                with _lock:
                    _estado["procesados"] += 1

                time.sleep(PAUSA_SEG)

            if page >= total_pages:
                break
            page += 1

        with _lock:
            resultado_final = {
                "procesados":  _estado["procesados"],
                "actualizados": _estado["actualizados"],
                "errores":     _estado["errores"],
                "total":       _estado["total"],
            }

        print(f"[actor_worker] Backfill completo: {resultado_final}")
        return resultado_final

    finally:
        with _lock:
            _estado["corriendo"] = False


def lanzar_backfill_async(dry_run: bool = False):
    """Lanza el backfill en un hilo daemon para no bloquear la API."""
    t = threading.Thread(
        target=backfill_clasificacion_actores,
        kwargs={"dry_run": dry_run},
        daemon=True,
        name="actor-backfill",
    )
    t.start()
    return t
