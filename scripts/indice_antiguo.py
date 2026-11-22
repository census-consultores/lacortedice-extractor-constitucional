"""
Crea el job del índice rápido para 2008–2015 sobre los juzgados donde el mapeo
del sistema anterior (scripts/mapeo_antiguo.py) encontró garantías (idMateria 9
en las muestras) y que están en MATERIA.JUDICATURAS. Las descargas las encola
después la cola automática (desde_indice) a medida que cada par termina.

Uso, en el shell del worker:  python scripts/indice_antiguo.py [--hilos 10]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import materia as MATERIA  # noqa: E402
from worker import job_manager as jm  # noqa: E402
from worker import numeracion  # noqa: E402

AÑOS = [str(a) for a in range(2008, 2016)]

ap = argparse.ArgumentParser()
ap.add_argument("--hilos", type=int, default=10)
a = ap.parse_args()

with jm._conn() as conn:
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT prefijo FROM worker_mapeo_antiguo_par p
                        WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(p.muestras) x
                                       WHERE (x->>'id')::int = ANY(%s))""",
                    (sorted(MATERIA.IDS_MATERIA_SATJE),))
        prefijos = sorted(p for (p,) in cur.fetchall() if p in MATERIA.JUDICATURAS)
numeracion.recargar()
job = jm.crear_job(tribunal="-", año="-", desde=1, hasta=len(prefijos) * len(AÑOS),
                   dry_run=False, modelo="-", iniciado_por="indice-antiguo",
                   workers="indice", categoria="indice",
                   parametros=json.dumps({"prefijos": prefijos, "años": AÑOS,
                                          "hilos": max(1, min(a.hilos, 12)),
                                          "pausa": 0.3, "max_vacias": 30}))
print(f"Job {job['id']}: índice 2008–2015 sobre {len(prefijos)} juzgados ({len(prefijos) * len(AÑOS)} pares)")
