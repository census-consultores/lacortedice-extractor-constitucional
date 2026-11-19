"""
Mapeo del sistema anterior de SATJE (2008–2015) para garantías constitucionales.

Antes de 2016 las causas se numeran con los códigos de los juzgados antiguos
(09111, 09301…) y, en esos juzgados, con secuencial de 4 dígitos
(09111-2008-0139). Este script descubre, sin descargar nada:

  Fase 1 — qué prefijos NN000–NN999 tuvieron causas entre 2008 y 2015 y con
           cuántos dígitos (prueba 0001 y 00001 en tres años).
  Fase 2 — por cada (prefijo, año) con causas: el último secuencial (búsqueda
           galopante + binaria) y una muestra de 12 cabeceras con idMateria,
           materia y nombreDelito, para identificar el código de materia de
           las garantías del sistema anterior (acción de protección, hábeas
           corpus, amparo…).

Resultados en worker_mapeo_antiguo_judicatura y worker_mapeo_antiguo_par
(la columna digitos alimenta worker/numeracion.py). Reanudable: lo ya anotado
no se vuelve a consultar. Uso, en el shell del worker:

    nohup python -u scripts/mapeo_antiguo.py --hilos 6 > /tmp/mapeo_antiguo.log 2>&1 &
    python scripts/mapeo_antiguo.py --resumen

Correrlo cuando termine la descarga 2016–2026 (comparte el límite de consultas
a SATJE del worker).
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api  # noqa: E402
from worker import job_manager as jm  # noqa: E402
from worker.mapeo_worker import ultimo_secuencial, posiciones_muestra  # noqa: E402
from worker.reindexar_worker import _todos  # noqa: E402

AÑOS = [str(a) for a in range(2008, 2016)]
PROVINCIAS = [f"{n:02d}" for n in range(1, 25)]
SONDEOS = [("2013", 4), ("2013", 5), ("2010", 4), ("2015", 5), ("2008", 4)]
TAMANO_MUESTRA = 12
PALABRAS_GARANTIA = ("PROTECCION", "HABEAS", "AMPARO", "GARANTIA", "CONSTITUCIONAL",
                     "ACCESO A LA INFORMACION", "MEDIDAS CAUTELARES", "INCUMPLIMIENTO")

SQL = """
CREATE TABLE IF NOT EXISTS worker_mapeo_antiguo_judicatura (
    prefijo   VARCHAR(10) PRIMARY KEY,
    digitos   INTEGER,          -- NULL = sin causas 2008–2015 en los sondeos
    nombre    TEXT,
    muestra   VARCHAR(30),
    sondeado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS worker_mapeo_antiguo_par (
    prefijo   VARCHAR(10) NOT NULL,
    anio      VARCHAR(4)  NOT NULL,
    digitos   INTEGER     NOT NULL,
    ultimo    INTEGER     NOT NULL,
    muestras  JSONB       NOT NULL DEFAULT '[]',
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (prefijo, anio)
);
"""


class SinRespuesta(Exception):
    pass


def _norm(s):
    s = (s or "").upper()
    for a, b in zip("ÁÉÍÓÚ", "AEIOU"):
        s = s.replace(a, b)
    return s


def consultar(numero, pausa):
    try:
        r = _todos(numero)
    except api.SatjeNoDisponible:
        r = None
    time.sleep(pausa)
    if r is None:
        raise SinRespuesta(numero)
    return r


def num(prefijo, anio, n, d):
    return f"{prefijo}-{anio}-{n:0{d}d}"


def descubrir(prefijo, pausa):
    for anio, d in SONDEOS:
        res = consultar(num(prefijo, anio, 1, d), pausa)
        if res:
            nombre = ""
            try:
                cab = res[0]
                for inc in api.obtener_incidentes((cab.get("idJuicio") or "").strip()) or []:
                    nombre = inc.get("nombreJudicatura") or nombre
                    if str(inc.get("idJudicatura", "")) == prefijo:
                        break
            except api.SatjeNoDisponible:
                raise SinRespuesta(prefijo)
            return {"digitos": d, "nombre": nombre or "(sin nombre)", "muestra": num(prefijo, anio, 1, d)}
    return None


def contar(prefijo, anio, d, pausa):
    def existe(n):
        return any(consultar(num(prefijo, anio, m, d), pausa) for m in (n, n + 1, n + 2))
    ultimo = ultimo_secuencial(existe, tope=10 ** d - 1)
    muestras = []
    for s in posiciones_muestra(ultimo, TAMANO_MUESTRA):
        for c in consultar(num(prefijo, anio, s, d), pausa):
            muestras.append({"s": s, "id": c.get("idMateria"),
                             "m": (c.get("nombreMateria") or c.get("materia") or "")[:80],
                             "d": (c.get("nombreDelito") or "")[:120]})
    return ultimo, muestras


def _ya(sql):
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return {tuple(r) if len(r) > 1 else r[0] for r in cur.fetchall()}


def _exec(sql, args):
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
        conn.commit()


def correr(hilos, pausa, provincias):
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SQL)
        conn.commit()
    lock = threading.Lock()
    cuenta = Counter()

    hechos = _ya("SELECT prefijo FROM worker_mapeo_antiguo_judicatura")
    prefijos = [f"{pv}{n:03d}" for pv in provincias for n in range(1000) if f"{pv}{n:03d}" not in hechos]
    print(f"Fase 1: {len(prefijos)} prefijos por sondear con {hilos} hilos", flush=True)

    def uno(pf):
        try:
            r = descubrir(pf, pausa)
        except SinRespuesta:
            with lock:
                cuenta["sin_respuesta"] += 1
            return
        _exec("""INSERT INTO worker_mapeo_antiguo_judicatura (prefijo, digitos, nombre, muestra)
                 VALUES (%s,%s,%s,%s) ON CONFLICT (prefijo) DO NOTHING""",
              (pf, (r or {}).get("digitos"), (r or {}).get("nombre"), (r or {}).get("muestra")))
        with lock:
            cuenta["prefijos"] += 1
            cuenta["con_causas"] += bool(r)
            if cuenta["prefijos"] % 500 == 0:
                print(f"Fase 1: {dict(cuenta)}", flush=True)

    with ThreadPoolExecutor(hilos) as ex:
        list(ex.map(uno, prefijos))

    juzgados = _ya("SELECT prefijo, digitos FROM worker_mapeo_antiguo_judicatura WHERE digitos IS NOT NULL")
    hechos_par = _ya("SELECT prefijo, anio FROM worker_mapeo_antiguo_par")
    pares = [(pf, a, d) for a in reversed(AÑOS) for pf, d in sorted(juzgados)
             if pf[:2] in provincias and (pf, a) not in hechos_par]
    print(f"Fase 2: {len(juzgados)} juzgados, {len(pares)} pares por contar", flush=True)

    def par(t):
        pf, a, d = t
        try:
            ultimo, muestras = contar(pf, a, d, pausa)
            if not ultimo and d == 4:          # el juzgado pudo pasar a 5 dígitos ese año
                ultimo, muestras = contar(pf, a, 5, pausa)
                d = 5 if ultimo else d
        except SinRespuesta:
            with lock:
                cuenta["sin_respuesta"] += 1
            return
        _exec("""INSERT INTO worker_mapeo_antiguo_par (prefijo, anio, digitos, ultimo, muestras)
                 VALUES (%s,%s,%s,%s,%s) ON CONFLICT (prefijo, anio) DO UPDATE
                    SET digitos=EXCLUDED.digitos, ultimo=EXCLUDED.ultimo,
                        muestras=EXCLUDED.muestras, actualizado=now()""",
              (pf, a, d, ultimo, json.dumps(muestras, ensure_ascii=False)))
        with lock:
            cuenta["pares"] += 1
            cuenta["numeros"] += ultimo
            if cuenta["pares"] % 50 == 0:
                print(f"Fase 2: {dict(cuenta)}", flush=True)

    with ThreadPoolExecutor(hilos) as ex:
        list(ex.map(par, pares))
    print(f"Terminado: {dict(cuenta)} (con sin_respuesta > 0, relanzar: retoma lo pendiente)", flush=True)


def resumen():
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(digitos) FROM worker_mapeo_antiguo_judicatura")
            sondeados, con_causas = cur.fetchone()
            cur.execute("SELECT anio, sum(ultimo), count(*) FROM worker_mapeo_antiguo_par GROUP BY 1 ORDER BY 1")
            por_anio = cur.fetchall()
            cur.execute("SELECT muestras FROM worker_mapeo_antiguo_par")
            materias, garantias = Counter(), Counter()
            for (ms,) in cur.fetchall():
                for m in ms:
                    materias[(m.get("id"), m.get("m"))] += 1
                    if any(p in _norm(m.get("d")) for p in PALABRAS_GARANTIA):
                        garantias[(m.get("id"), m.get("m"), m.get("d"))] += 1
    print(f"Prefijos sondeados: {sondeados}; con causas 2008–2015: {con_causas}")
    print("Números por año (año, total, juzgados):", por_anio)
    print("idMateria en las muestras:", materias.most_common(25))
    print("Muestras con nombre de garantía (idMateria, materia, delito):", garantias.most_common(25))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hilos", type=int, default=6)
    ap.add_argument("--pausa", type=float, default=0.3)
    ap.add_argument("--provincias", default="")
    ap.add_argument("--resumen", action="store_true")
    a = ap.parse_args()
    if a.resumen:
        resumen()
    else:
        api.activar_proxy_desde_entorno()
        correr(max(1, min(a.hilos, 10)), a.pausa,
               [p.zfill(2) for p in a.provincias.split(",") if p] or PROVINCIAS)
