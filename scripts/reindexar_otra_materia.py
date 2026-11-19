"""Prueba local del re-índice (ver worker/reindexar_worker.py).
  DATABASE_URL=... BDATA_DATACENTER_PROXY_URL=... \
    python scripts/reindexar_otra_materia.py --prefijo 14305 --anio 2016 [--sin-jobs]
En producción corre como job: workers="reindexar_otra_materia", categoria="indice"."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api
from worker import reindexar_worker as rw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefijo"); ap.add_argument("--anio")
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--paralelo", type=int, default=8)
    ap.add_argument("--sin-jobs", action="store_true")
    a = ap.parse_args()
    api.activar_proxy_desde_entorno()
    tot = rw.correr(a.prefijo, a.anio, a.limite, a.paralelo, not a.sin_jobs,
                    al_avanzar=lambda t: print(t, flush=True))
    print(f"listo · {tot}")


if __name__ == "__main__":
    main()
