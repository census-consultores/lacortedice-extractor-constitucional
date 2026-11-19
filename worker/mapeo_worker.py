"""
Mapeo de volumen en SATJE: cuántas causas tiene cada judicatura por año, sin
barrer número por número.

Sirve para dimensionar una materia nueva (p. ej. penal, "La Fiscalía Dice")
antes de crear su worker. Usa la misma salida a SATJE que el resto del motor
(api._post: Bright Data, ritmo máximo y freno de emergencia).

Fase 1 — descubrir: por cada prefijo NN000..NN999 de las provincias pedidas
se consulta el secuencial 1 y 2 de 2024 (y el 1 de 2019, para judicaturas que
ya no existen) y, si hay causa, el nombre de la judicatura (incidentes).
Fase 2 — contar: para las judicaturas cuyo nombre contiene alguna `clave`, y
por cada año, búsqueda binaria del último secuencial (~20 consultas por par)
más una muestra de 10 números repartidos para ver la materia y el delito.

Parámetros (worker_jobs.parametros, JSON):
  {"provincias": ["01", ...], "años": ["2016", ...], "claves": ["PENAL", ...],
   "hilos": 6, "pausa": 0.3}
Reanudable: lo sondeado queda en worker_mapeo_judicatura / worker_mapeo_par.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2.extras

import api
from worker import job_manager as jm
from worker import logger as log
from worker.reindexar_worker import _todos

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS worker_mapeo_judicatura (
    prefijo     VARCHAR(10) PRIMARY KEY,
    nombre      TEXT,          -- NULL = sin causas en los secuenciales sondeados
    muestra     VARCHAR(30),
    id_materia  INTEGER,
    materia     VARCHAR(200),
    sondeado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS worker_mapeo_par (
    prefijo     VARCHAR(10) NOT NULL,
    anio        VARCHAR(4)  NOT NULL,
    ultimo      INTEGER     NOT NULL,
    muestras    JSONB       NOT NULL DEFAULT '[]',
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (prefijo, anio)
);
"""

CLAVES_PENAL = ["PENAL", "FLAGRAN", "TRANSITO", "VIOLENCIA", "INFRACTOR", "MULTICOMPETENTE"]
# Apelación y casación conservan el número de primera instancia: contarlas duplicaría.
EXCLUIR = ["CORTE PROVINCIAL", "CORTE NACIONAL", "SALA "]
PROVINCIAS = [f"{n:02d}" for n in range(1, 25)]
TAMANO_MUESTRA = 10


class SinRespuesta(Exception):
    pass


def init_tablas():
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
        conn.commit()


def _norm(s: str) -> str:
    return (s or "").upper().replace("Á", "A").replace("É", "E").replace("Í", "I")\
        .replace("Ó", "O").replace("Ú", "U").replace("Ñ", "N")


def se_cuenta(nombre: str, claves: list) -> bool:
    n = _norm(nombre)
    return any(_norm(c) in n for c in claves) and not any(e in n for e in EXCLUIR)


def _consultar(numero: str, pausa: float) -> list:
    try:
        r = _todos(numero)
    except api.SatjeNoDisponible:          # freno de emergencia o sin proxy: el par queda pendiente
        r = None
    if pausa:
        time.sleep(pausa)
    if r is None:
        raise SinRespuesta(numero)
    return r


def ultimo_secuencial(existe, tope: int = 99999) -> int:
    """Último n con existe(n) True, asumiendo que existe() es True hasta un
    punto y False después. Galopa (64, 128, …) y luego busca binario."""
    if not existe(1):
        return 0
    lo, hi = 1, 64
    while hi < tope and existe(hi):
        lo, hi = hi, min(hi * 2, tope)
    if hi == tope and existe(hi):
        return tope
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if existe(mid):
            lo = mid
        else:
            hi = mid
    return lo


def posiciones_muestra(ultimo: int, k: int = TAMANO_MUESTRA) -> list:
    return sorted({max(1, round(i * ultimo / (k + 1))) for i in range(1, k + 1)}) if ultimo else []


# ── Fase 1 ────────────────────────────────────────────────────────────────────

def descubrir_prefijo(prefijo: str, pausa: float):
    for numero in (f"{prefijo}-2024-00001", f"{prefijo}-2024-00002", f"{prefijo}-2019-00001"):
        res = _consultar(numero, pausa)
        if not res:
            continue
        cab = res[0]
        nombre = ""
        try:
            for inc in api.obtener_incidentes((cab.get("idJuicio") or api.normalizar_numero(numero)).strip()) or []:
                if str(inc.get("idJudicatura", "")) == prefijo or not nombre:
                    nombre = inc.get("nombreJudicatura", "") or nombre
        except api.SatjeNoDisponible:
            raise SinRespuesta(numero)
        return {"nombre": nombre or "(sin nombre)", "muestra": numero,
                "id_materia": cab.get("idMateria"),
                "materia": (cab.get("nombreMateria") or cab.get("materia") or "")[:200]}
    return None


def _guardar_judicatura(prefijo: str, r):
    r = r or {}
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO worker_mapeo_judicatura (prefijo, nombre, muestra, id_materia, materia)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (prefijo) DO NOTHING""",
                        (prefijo, r.get("nombre"), r.get("muestra"), r.get("id_materia"), r.get("materia")))
        conn.commit()


# ── Fase 2 ────────────────────────────────────────────────────────────────────

def contar_par(prefijo: str, anio: str, pausa: float) -> tuple[int, list]:
    def existe(n):
        # shortcut: tolera huecos de hasta 2 números (causas anuladas); el último puede quedar corto en ±2.
        return any(_consultar(f"{prefijo}-{anio}-{m:05d}", pausa) for m in (n, n + 1, n + 2))
    ultimo = ultimo_secuencial(existe)
    muestras = []
    for s in posiciones_muestra(ultimo):
        for c in _consultar(f"{prefijo}-{anio}-{s:05d}", pausa):
            muestras.append({"s": s, "id": c.get("idMateria"),
                             "m": c.get("nombreMateria") or c.get("materia") or "",
                             "d": (c.get("nombreDelito") or "")[:120]})
    return ultimo, muestras


def _guardar_par(prefijo, anio, ultimo, muestras):
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO worker_mapeo_par (prefijo, anio, ultimo, muestras)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (prefijo, anio) DO UPDATE
                              SET ultimo=EXCLUDED.ultimo, muestras=EXCLUDED.muestras, actualizado=now()""",
                        (prefijo, anio, ultimo, json.dumps(muestras, ensure_ascii=False)))
        conn.commit()


def _hechos(sql: str) -> set:
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return {tuple(r) if len(r) > 1 else r[0] for r in cur.fetchall()}


# ── Job ───────────────────────────────────────────────────────────────────────

def ejecutar_job_mapeo(job_id: int, pause_ev, kill_ev):
    init_tablas()
    p = json.loads((jm.obtener_job(job_id) or {}).get("parametros") or "{}")
    provincias = [str(x).zfill(2) for x in p.get("provincias") or PROVINCIAS]
    años = [str(a) for a in p.get("años") or range(2016, time.localtime().tm_year + 1)]
    claves = p.get("claves") or CLAVES_PENAL
    hilos = max(1, min(int(p.get("hilos", 6)), 12))
    pausa = float(p.get("pausa", 0.3))

    detener = threading.Event()
    def _vigilar():
        while not detener.is_set():
            if kill_ev.is_set() or pause_ev.is_set():
                detener.set()
            time.sleep(1)
    threading.Thread(target=_vigilar, daemon=True).start()

    lock = threading.Lock()
    hechos = {"prefijos": 0, "judicaturas": 0, "pares": 0, "causas": 0, "sin_respuesta": 0}

    def _avance():
        jm._actualizar(job_id, checkpoint=hechos["prefijos"] + hechos["pares"],
                       total_procesadas=hechos["pares"], exitosas=hechos["causas"])

    ya = _hechos("SELECT prefijo FROM worker_mapeo_judicatura")
    prefijos = [f"{pv}{n:03d}" for pv in provincias for n in range(1000) if f"{pv}{n:03d}" not in ya]
    log.info(f"Mapeo — fase 1: {len(prefijos)} prefijos por sondear, {hilos} hilos", job_id=job_id)

    def _uno_prefijo(pf):
        if detener.is_set():
            return
        try:
            r = descubrir_prefijo(pf, pausa)
        except SinRespuesta:
            with lock:
                hechos["sin_respuesta"] += 1
            return          # queda sin anotar: se reintenta en otra corrida
        _guardar_judicatura(pf, r)
        with lock:
            hechos["prefijos"] += 1
            hechos["judicaturas"] += bool(r)
            if hechos["prefijos"] % 200 == 0:
                _avance()
                log.info(f"Mapeo fase 1: {hechos}", job_id=job_id)

    with ThreadPoolExecutor(max_workers=hilos) as ex:
        list(ex.map(_uno_prefijo, prefijos))

    if not detener.is_set():
        judic = _hechos("SELECT prefijo, nombre FROM worker_mapeo_judicatura WHERE nombre IS NOT NULL")
        contar = sorted(pf for pf, nombre in judic if se_cuenta(nombre, claves) and pf[:2] in provincias)
        ya_pares = _hechos("SELECT prefijo, anio FROM worker_mapeo_par")
        # Años del más reciente al más viejo: el volumen anual se ve pronto.
        pares = [(pf, a) for a in sorted(años, reverse=True) for pf in contar if (pf, a) not in ya_pares]
        log.info(f"Mapeo — fase 2: {len(contar)} judicaturas, {len(pares)} pares por contar", job_id=job_id)

        def _uno_par(par):
            if detener.is_set():
                return
            try:
                ultimo, muestras = contar_par(par[0], par[1], pausa)
            except SinRespuesta:
                with lock:
                    hechos["sin_respuesta"] += 1
                return
            _guardar_par(par[0], par[1], ultimo, muestras)
            with lock:
                hechos["pares"] += 1
                hechos["causas"] += ultimo
                if hechos["pares"] % 25 == 0:
                    _avance()
                    log.info(f"Mapeo fase 2: {hechos}", job_id=job_id)

        with ThreadPoolExecutor(max_workers=hilos) as ex:
            list(ex.map(_uno_par, pares))

    _avance()
    detener.set()
    if kill_ev.is_set():
        jm._actualizar(job_id, estado="cancelado", completado_en=jm._now())
    elif pause_ev.is_set():
        jm._actualizar(job_id, estado="pausado")
    else:
        jm._actualizar(job_id, estado="completado", completado_en=jm._now())
        log.info(f"Mapeo terminado: {hechos} (con sin_respuesta > 0, relanzar: retoma lo pendiente)",
                 job_id=job_id)


# ── Resumen para la API ───────────────────────────────────────────────────────

def estimar(pares: list) -> dict:
    """pares: [{prefijo, nombre, anio, ultimo, muestras}] → totales y reparto
    estimado por materia y delito (cada muestra pesa ultimo / nº de muestras)."""
    por_anio, por_materia, por_delito, por_judicatura = {}, {}, {}, {}
    for p in pares:
        por_anio[p["anio"]] = por_anio.get(p["anio"], 0) + p["ultimo"]
        clave_j = f'{p["prefijo"]} {p["nombre"]}'
        por_judicatura[clave_j] = por_judicatura.get(clave_j, 0) + p["ultimo"]
        m = p["muestras"] or []
        for x in m:
            peso = p["ultimo"] / len(m)
            mat = f'{x.get("id")} {x.get("m")}'.strip()
            por_materia[mat] = por_materia.get(mat, 0) + peso
            por_delito[x.get("d") or "?"] = por_delito.get(x.get("d") or "?", 0) + peso
    orden = lambda d, n=None: [{"clave": k, "causas": round(v)} for k, v in
                               sorted(d.items(), key=lambda kv: -kv[1])[:n]]
    return {"total_numeros": sum(por_anio.values()),
            "por_anio": dict(sorted(por_anio.items())),
            "por_materia_estimado": orden(por_materia),
            "por_delito_estimado_top50": orden(por_delito, 50),
            "por_judicatura": orden(por_judicatura)}


def resumen() -> dict:
    init_tablas()
    with jm._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT count(*) AS sondeados, count(nombre) AS judicaturas
                             FROM worker_mapeo_judicatura""")
            prog = dict(cur.fetchone())
            cur.execute("SELECT prefijo, nombre, materia FROM worker_mapeo_judicatura WHERE nombre IS NOT NULL ORDER BY prefijo")
            judicaturas = [dict(r) for r in cur.fetchall()]
            cur.execute("""SELECT p.prefijo, j.nombre, p.anio, p.ultimo, p.muestras
                             FROM worker_mapeo_par p JOIN worker_mapeo_judicatura j USING (prefijo)""")
            pares = [dict(r) for r in cur.fetchall()]
    return {"progreso": {**prog, "pares_contados": len(pares)}, **estimar(pares),
            "judicaturas_descubiertas": judicaturas}
