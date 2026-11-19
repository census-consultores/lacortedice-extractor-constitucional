"""
Segunda vuelta MANUAL de las causas cuyo PDF (tribunal, apelación o casación)
estaba indicado en SATJE pero no se pudo descargar. Se corre al terminar la
barrida, no en automático (ver MARCA_PDF_FALLIDO en resolucion/orquestador.py).

Toma el ÚLTIMO registro de cada número en worker_caso_log: si una pasada
posterior ya lo bajó, no entra.

  DATABASE_URL=... python scripts/segunda_vuelta_pdf_fallido.py            # solo cuenta
  DATABASE_URL=... python scripts/segunda_vuelta_pdf_fallido.py --crear    # crea los jobs (pendientes)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import job_manager as jm


def marcados() -> list:
    with jm._conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT numero, observaciones FROM (
                SELECT DISTINCT ON (numero) numero, observaciones FROM worker_caso_log
                 WHERE numero ~ '^[0-9]{5}-[0-9]{4}-[0-9]+$'
                 ORDER BY numero, id DESC) u
             WHERE observaciones LIKE '[PDF_FALLIDO%%'""")
        return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crear", action="store_true")
    a = ap.parse_args()
    filas = marcados()
    por_nivel = {}
    for _, obs in filas:
        for nivel in ("tribunal", "apelacion", "cnj"):
            if f"[PDF_FALLIDO:{nivel}]" in obs:
                por_nivel[nivel] = por_nivel.get(nivel, 0) + 1
    print(f"{len(filas)} causas marcadas · {por_nivel}")
    if not a.crear or not filas:
        return
    grupos = {}
    for numero, _ in filas:
        t, an, s = numero.split("-")
        grupos.setdefault((t, an), []).append(int(s))
    for (t, an), secs in sorted(grupos.items()):
        jm.crear_job(tribunal=t, año=an, desde=min(secs), hasta=max(secs), dry_run=False,
                     modelo="gpt-4o", iniciado_por="segunda-vuelta-pdf", workers="reintento",
                     solo_descarga=True, parametros=json.dumps({"numeros": sorted(secs)}),
                     arrancar=False)
    print(f"{len(grupos)} jobs creados en pendiente")


if __name__ == "__main__":
    main()
