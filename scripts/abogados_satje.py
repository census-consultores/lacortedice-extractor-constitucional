"""
Abogados desde las notificaciones de SATJE. Las sentencias casi no traen el
correo del abogado (muestra civil 13-nov: 15 de 289); las razones de
notificación sí: "a: PARTE … correo electrónico … del Dr./Ab. NOMBRE".

Por cada causa de worker_causas: buscar la causa, sus incidentes (todas las
instancias) y sus actuaciones; de las que mencionan un correo se extraen los
abogados con las reglas de directorio_abogados.py. Se guarda solo lo extraído
(nombre, correos, matrícula, teléfono), no el texto, en la tabla abogado_satje.
Reanudable: una causa ya consultada no se vuelve a pedir.

Las consultas pasan por api._get/_post: proxy de Bright Data, ritmo máximo y
freno de emergencia de todo el worker.

  python scripts/abogados_satje.py --limite 500                  # prueba
  nohup python -u scripts/abogados_satje.py --hilos 4 > /tmp/abogados_satje.log 2>&1 &

Después: python scripts/directorio_abogados.py --fuente satje
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PAUSA_SEG = 0.5   # entre causas, por hilo: no apretar a SATJE
TANDA = 50        # causas enviadas a los hilos a la vez (memoria acotada)
CONSOLIDAR_CADA_SEG = 2 * 3600   # el directorio central se rehace cada 2 h

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS abogado_satje (
    numero          VARCHAR(50) PRIMARY KEY,
    n_notificaciones INTEGER NOT NULL DEFAULT 0,
    menciones       JSONB   NOT NULL DEFAULT '[]',
    error           TEXT,
    consultado_en   TIMESTAMPTZ DEFAULT NOW()
);
"""


def menciones_de_actuaciones(actuaciones: list) -> tuple:
    """(n actuaciones con correo, menciones de abogados) de una lista de SATJE."""
    from api import limpiar_html
    from scripts.directorio_abogados import extraer_notificacion
    n, por_clave = 0, {}
    for act in actuaciones:
        texto = limpiar_html(act.get("actividad") or "")
        if "@" not in texto:
            continue
        n += 1
        for a in extraer_notificacion(texto):
            y = por_clave.setdefault(a["clave"], a)
            if y is not a:
                y["emails"] |= a["emails"]
                y["matricula"] = y["matricula"] or a["matricula"]
                y["telefono"] = y["telefono"] or a["telefono"]
    return n, [dict(a, emails=sorted(a["emails"])) for a in por_clave.values()]


def consultar(numero: str) -> tuple:
    import api
    causa = api.buscar_causa(numero)
    if not causa:
        return 0, []
    id_juicio = api.id_juicio_de(causa, numero)
    actuaciones = []
    for inc in api.obtener_incidentes(id_juicio):
        actuaciones += api.obtener_actuaciones(id_juicio, inc)
    return menciones_de_actuaciones(actuaciones)


def procesar(numero: str) -> dict:
    from worker import job_manager as jm
    t0 = time.monotonic()
    try:
        n, menciones, error = *consultar(numero), None
    except Exception as e:      # SATJE caído o respuesta rara: se guarda y se reintenta luego
        n, menciones, error = 0, [], f"{type(e).__name__}: {e}"[:500]
    with jm._conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO abogado_satje (numero, n_notificaciones, menciones, error)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (numero) DO UPDATE SET n_notificaciones = EXCLUDED.n_notificaciones,
                menciones = EXCLUDED.menciones, error = EXCLUDED.error, consultado_en = NOW()
        """, (numero, n, json.dumps(menciones, ensure_ascii=False), error))
    time.sleep(PAUSA_SEG)
    return {"n": n, "menciones": menciones, "error": error, "seg": time.monotonic() - t0}


def pendientes(limite=None, tribunal=None) -> list:
    """Causas sin consultar o con error, de la más reciente a la más antigua."""
    from worker import job_manager as jm
    sql = """SELECT c.numero FROM worker_causas c
             LEFT JOIN abogado_satje a ON a.numero = c.numero
             WHERE (a.numero IS NULL OR a.error IS NOT NULL)"""
    params = []
    if tribunal:
        sql += " AND c.tribunal_codigo = %s"
        params.append(tribunal)
    sql += " ORDER BY c.año DESC, c.numero"
    if limite:
        sql += " LIMIT %s"
        params.append(limite)
    with jm._conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limite", type=int, help="máximo de causas (prueba)")
    ap.add_argument("--tribunal", help="solo una judicatura (código)")
    ap.add_argument("--hilos", type=int, default=2)
    args = ap.parse_args()

    import api
    from worker import job_manager as jm
    ok, msg = api.activar_proxy_desde_entorno()
    print(msg, flush=True)
    if not ok and os.environ.get("RENDER"):
        # Sin proxy cada consulta falla: no tiene sentido recorrer el corpus
        # anotando errores (administrativo, 13-nov: 6.690 en 10 minutos).
        sys.exit("Detenido: configure BDATA_DATACENTER_PROXY_URL en el worker y relance.")
    with jm._conn() as conn, conn.cursor() as cur:
        cur.execute(CREATE_SQL)
    numeros = pendientes(args.limite, args.tribunal)
    print(f"{len(numeros)} causas por consultar con {args.hilos} hilos", flush=True)

    t0 = time.monotonic()
    tot = {"causas": 0, "con_correo": 0, "abogados": 0, "con_email": 0, "errores": 0}
    # ex.map(procesar, numeros) crearía un Future por cada causa de golpe (207.000
    # en civil) y guardaría los resultados que llegan desordenados: el 13-nov eso
    # pasó los 512 MB del worker. Se envían por tandas pequeñas.
    def resultados():
        with ThreadPoolExecutor(max_workers=max(1, min(args.hilos, 8))) as ex:
            for k in range(0, len(numeros), TANDA):
                yield from ex.map(procesar, numeros[k:k + TANDA])

    def consolidar():
        from scripts.directorio_abogados import consolidar as _c
        from config import materia as MATERIA
        try:
            print(f"  directorio central: {_c(MATERIA.SLUG)} abogados de {MATERIA.SLUG}", flush=True)
        except Exception as e:   # no debe tumbar la descarga
            print(f"  directorio central: error {type(e).__name__}: {e}", flush=True)

    ultima = time.monotonic()
    for i, r in enumerate(resultados(), 1):
        if time.monotonic() - ultima >= CONSOLIDAR_CADA_SEG:
            consolidar()
            ultima = time.monotonic()
        tot["causas"] += 1
        tot["errores"] += bool(r["error"])
        tot["con_correo"] += bool(r["n"])
        tot["abogados"] += len(r["menciones"])
        tot["con_email"] += sum(1 for m in r["menciones"] if m["emails"])
        if i % 100 == 0 or i == len(numeros):
            seg = (time.monotonic() - t0) / i
            print(f"  {i}/{len(numeros)} · {seg:.1f} s/causa · {tot}", flush=True)
    print(f"Listo: {tot}", flush=True)
    consolidar()


if __name__ == "__main__":
    main()
