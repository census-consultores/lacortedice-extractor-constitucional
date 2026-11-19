"""
Importa el índice rápido (número → idMateria) ya construido por otro worker,
para no volver a consultar en SATJE los números que ya se recorrieron.

El índice penal (≈3,2 millones de números, 226 judicaturas, 2016–2026) ya
contiene el idMateria de cada número de las unidades penales, de flagrancia y
multicompetentes. Se copian solo las judicaturas de MATERIA.JUDICATURAS, con su
progreso; el índice de este worker continúa desde ahí (años 2008–2015 y
judicaturas que faltan).

Uso, en el shell del worker constitucional:
    python -u scripts/importar_indice.py            # usa INDICE_ORIGEN_URL
    python -u scripts/importar_indice.py --solo-contar

Idempotente: ON CONFLICT DO NOTHING en el índice; el progreso se queda con el
avance mayor. Solo lee la base de origen.
"""
import argparse
import os
import sys
import tempfile

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import materia as MATERIA  # noqa: E402
from worker.indice_worker import CREATE_INDICE_SQL  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--origen", default=os.environ.get("INDICE_ORIGEN_URL", ""))
    ap.add_argument("--solo-contar", action="store_true")
    a = ap.parse_args()
    if not a.origen:
        sys.exit("Falta INDICE_ORIGEN_URL (conexión de solo lectura a la base del worker de origen).")
    prefijos = sorted(MATERIA.JUDICATURAS)

    src = psycopg2.connect(a.origen)
    src.set_session(readonly=True)
    dst = psycopg2.connect(os.environ["DATABASE_URL"])
    with dst.cursor() as cur:
        cur.execute(CREATE_INDICE_SQL)
    dst.commit()

    with src.cursor() as cur:
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE id_materia = ANY(%s))
                         FROM worker_indice_satje WHERE prefijo = ANY(%s)""",
                    (list(MATERIA.IDS_MATERIA_SATJE), prefijos))
        total, de_materia = cur.fetchone()
    print(f"Origen: {total} números de {len(prefijos)} judicaturas; {de_materia} de la materia.", flush=True)
    if a.solo_contar:
        return

    with tempfile.TemporaryFile("w+") as f:
        with src.cursor() as cur:
            cur.copy_expert(cur.mogrify(
                """COPY (SELECT numero, prefijo, anio, secuencial, id_materia, materia, consultado_en
                           FROM worker_indice_satje WHERE prefijo = ANY(%s)) TO STDOUT""",
                (prefijos,)).decode(), f)
        f.seek(0)
        with dst.cursor() as cur:
            cur.execute("CREATE TEMP TABLE imp (LIKE worker_indice_satje INCLUDING DEFAULTS) ON COMMIT DROP")
            cur.copy_expert("COPY imp (numero, prefijo, anio, secuencial, id_materia, materia, consultado_en) FROM STDIN", f)
            cur.execute("INSERT INTO worker_indice_satje SELECT * FROM imp ON CONFLICT (numero) DO NOTHING")
            print(f"Índice: {cur.rowcount} números nuevos.", flush=True)
        dst.commit()

    with src.cursor() as cur:
        cur.execute("""SELECT prefijo, anio, ultimo, hallados, terminado FROM worker_indice_progreso
                        WHERE prefijo = ANY(%s)""", (prefijos,))
        filas = cur.fetchall()
    with dst.cursor() as cur:
        for fila in filas:
            cur.execute("""INSERT INTO worker_indice_progreso (prefijo, anio, ultimo, hallados, terminado)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (prefijo, anio) DO UPDATE
                              SET ultimo = GREATEST(worker_indice_progreso.ultimo, EXCLUDED.ultimo),
                                  hallados = GREATEST(worker_indice_progreso.hallados, EXCLUDED.hallados),
                                  terminado = worker_indice_progreso.terminado OR EXCLUDED.terminado,
                                  actualizado = now()""", fila)
    dst.commit()
    print(f"Progreso: {len(filas)} pares (prefijo, año) importados, "
          f"{sum(1 for x in filas if x[4])} terminados.", flush=True)


if __name__ == "__main__":
    main()
