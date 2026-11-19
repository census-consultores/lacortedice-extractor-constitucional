"""
Índice rápido de SATJE — "qué número es de qué materia", sin bajar nada.

Para encontrar las causas laborales/civiles en las Unidades Civiles y
Multicompetentes (donde solo el 3-9 % son de la materia) no hay atajo: SATJE
no permite listar por materia sin nombre o cédula de una parte. Pero consultar
un número cuesta ~0,25 s si solo se pide la cabecera (idMateria). Este worker
barre (prefijo × año) con varios hilos en paralelo y guarda en
`worker_indice_satje` cada causa que existe con su idMateria. Después, los
jobs con `workers="desde_indice"` descargan SOLO los números de la materia
(ver worker/runner.py), sin perder tiempo en lo demás.

El mismo índice sirve a cualquier materia (laboral 35, civil 32…): se puede
exportar a otro extractor.

Parámetros (worker_jobs.parametros, JSON):
  {"prefijos": ["09332", ...], "años": ["2016", ...], "hilos": 8,
   "pausa": 0.3, "max_vacias": 30}
Reanudable: `worker_indice_progreso` guarda el último secuencial consultado
por (prefijo, año); un par terminado no se vuelve a barrer.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extras

import api
from api import normalizar_numero
from worker import job_manager as jm
from worker import logger as log
from worker import numeracion

CREATE_INDICE_SQL = """
CREATE TABLE IF NOT EXISTS worker_indice_satje (
    numero        VARCHAR(30) PRIMARY KEY,
    prefijo       VARCHAR(10) NOT NULL,
    anio          VARCHAR(4)  NOT NULL,
    secuencial    INTEGER     NOT NULL,
    id_materia    INTEGER,
    materia       VARCHAR(200),
    consultado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_indice_pref_anio_mat
    ON worker_indice_satje (prefijo, anio, id_materia);
CREATE TABLE IF NOT EXISTS worker_indice_progreso (
    prefijo     VARCHAR(10) NOT NULL,
    anio        VARCHAR(4)  NOT NULL,
    ultimo      INTEGER     NOT NULL DEFAULT 0,
    hallados    INTEGER     NOT NULL DEFAULT 0,
    terminado   BOOLEAN     NOT NULL DEFAULT false,
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (prefijo, anio)
);
"""

MAX_VACIAS_DEFAULT = 30
MIN_ANTES_DE_CORTAR = 50   # no cortar por vacías antes del secuencial 50
REINTENTOS_CORTE = 10      # fallos seguidos de SATJE antes de dejar el par pendiente
ESPERA_CORTE_S = 60        # espera entre esos reintentos (~10 min en total)


def init_tablas():
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_INDICE_SQL)
        conn.commit()


def consultar_cabecera(numero: str) -> dict | None:
    """Una consulta a buscarCausas (sin los reintentos de api.buscar_causa).
    Devuelve la cabecera de la causa o None si no existe. Ante un error de red
    reintenta UNA vez; si vuelve a fallar, levanta la excepción."""
    body = {
        "numeroCausa": normalizar_numero(numero),
        "actor": {"cedulaActor": "", "nombreActor": ""},
        "demandado": {"cedulaDemandado": "", "nombreDemandado": ""},
        "provincia": "", "canton": None, "materia": None, "dependencia": None,
        "numeroFiscalia": "", "first": 1, "pageSize": 10, "recaptcha": "verdad",
    }
    for intento in (1, 2):
        try:
            r = api._post(f"{api.BASE_CAUSAS}/informacion/buscarCausas?page=1&size=10",
                          headers=api.HEADERS, json=body, timeout=(8, 20))
            r.raise_for_status()
            data = r.json()
            return api.elegir_de_la_materia(data)
        except Exception:
            if intento == 2:
                raise
            time.sleep(3)


def _progreso(prefijo: str, anio: str) -> dict:
    with jm._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""INSERT INTO worker_indice_progreso (prefijo, anio)
                           VALUES (%s,%s) ON CONFLICT DO NOTHING""", (prefijo, anio))
            cur.execute("SELECT * FROM worker_indice_progreso WHERE prefijo=%s AND anio=%s",
                        (prefijo, anio))
            r = dict(cur.fetchone())
        conn.commit()
    return r


def _guardar(prefijo: str, anio: str, hallados: list, ultimo: int, terminado: bool):
    with jm._conn() as conn:
        with conn.cursor() as cur:
            for sec, cab in hallados:
                cur.execute("""
                    INSERT INTO worker_indice_satje (numero, prefijo, anio, secuencial, id_materia, materia)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (numero) DO UPDATE
                       SET id_materia=EXCLUDED.id_materia, materia=EXCLUDED.materia,
                           consultado_en=now()""",
                    (numeracion.numero(prefijo, anio, sec), prefijo, anio, sec,
                     cab.get("idMateria"),
                     (cab.get("nombreMateria") or cab.get("materia") or "")[:200]))
            cur.execute("""
                UPDATE worker_indice_progreso
                   SET ultimo=%s, hallados=hallados+%s, terminado=%s, actualizado=now()
                 WHERE prefijo=%s AND anio=%s""",
                (ultimo, len(hallados), terminado, prefijo, anio))
        conn.commit()


def barrer_par(prefijo: str, anio: str, pausa: float, max_vacias: int,
               detener: threading.Event) -> tuple[int, int]:
    """Barre un (prefijo, año) desde donde quedó. Devuelve (consultados, hallados)."""
    prog = _progreso(prefijo, anio)
    if prog["terminado"]:
        return 0, 0
    n = prog["ultimo"] + 1
    vacias, consultados, total_hallados = 0, 0, 0
    lote: list = []
    fallos = 0
    while not detener.is_set():
        try:
            cab = consultar_cabecera(numeracion.numero(prefijo, anio, n))
        except Exception as e:
            # Un corte de SATJE de minutos (9-nov-2026 05:11) hacía abandonar
            # TODOS los pares pendientes: se espera y se reintenta el mismo
            # número; solo tras REINTENTOS_CORTE fallos seguidos se deja el par
            # sin terminar para otra corrida.
            fallos += 1
            if fallos > REINTENTOS_CORTE:
                _guardar(prefijo, anio, lote, n - 1, False)
                log.warn(f"Índice {prefijo}-{anio}: corte en {n} por {type(e).__name__}")
                return consultados, total_hallados + len(lote)
            detener.wait(ESPERA_CORTE_S)
            continue
        fallos = 0
        consultados += 1
        if cab:
            lote.append((n, cab))
            vacias = 0
        else:
            vacias += 1
        terminado = vacias >= max_vacias and n >= MIN_ANTES_DE_CORTAR
        if terminado or len(lote) >= 50 or consultados % 200 == 0:
            _guardar(prefijo, anio, lote, n, terminado)
            total_hallados += len(lote)
            lote = []
        if terminado:
            return consultados, total_hallados
        n += 1
        if pausa:
            time.sleep(pausa)
    _guardar(prefijo, anio, lote, n - 1, False)
    return consultados, total_hallados + len(lote)


def ejecutar_job_indice(job_id: int, pause_ev, kill_ev):
    init_tablas()
    job = jm.obtener_job(job_id) or {}
    p = json.loads(job.get("parametros") or "{}")
    prefijos   = [str(x).zfill(5) for x in p.get("prefijos", [])]
    años       = [str(x) for x in p.get("años", [])]
    hilos      = max(1, min(int(p.get("hilos", 8)), 16))
    pausa      = float(p.get("pausa", 0.3))
    max_vacias = int(p.get("max_vacias", MAX_VACIAS_DEFAULT))
    pares = [(pf, a) for pf in prefijos for a in años]

    detener = threading.Event()
    def _vigilar():
        while not detener.is_set():
            if kill_ev.is_set() or pause_ev.is_set():
                detener.set()
            time.sleep(1)
    threading.Thread(target=_vigilar, daemon=True).start()

    log.info(f"Índice SATJE — {len(pares)} pares (prefijo×año), {hilos} hilos, pausa {pausa}s",
             job_id=job_id)
    hechos = {"pares": 0, "consultados": 0, "hallados": 0}
    lock = threading.Lock()

    def _uno(par):
        if detener.is_set():
            return
        c, h = barrer_par(par[0], par[1], pausa, max_vacias, detener)
        with lock:
            hechos["pares"] += 1
            hechos["consultados"] += c
            hechos["hallados"] += h
            jm._actualizar(job_id, checkpoint=hechos["pares"],
                           total_procesadas=hechos["consultados"],
                           exitosas=hechos["hallados"])
        if c:
            log.info(f"Índice {par[0]}-{par[1]}: {c} consultas, {h} causas", job_id=job_id)

    with ThreadPoolExecutor(max_workers=hilos) as ex:
        list(ex.map(_uno, pares))
    detener.set()

    if kill_ev.is_set():
        jm._actualizar(job_id, estado="cancelado", completado_en=jm._now())
    elif pause_ev.is_set():
        jm._actualizar(job_id, estado="pausado")
    else:
        jm._actualizar(job_id, estado="completado", completado_en=jm._now())
        log.info(f"Índice terminado: {hechos}", job_id=job_id)


# ── Consultas para la API ─────────────────────────────────────────────────────

def resumen(ids_materia: list[int] | None = None) -> dict:
    init_tablas()
    with jm._conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT count(*) AS pares, count(*) FILTER (WHERE terminado) AS terminados,
                                  coalesce(sum(ultimo),0) AS consultados, coalesce(sum(hallados),0) AS hallados
                             FROM worker_indice_progreso""")
            prog = dict(cur.fetchone())
            cur.execute("""SELECT id_materia, count(*) AS causas FROM worker_indice_satje
                            GROUP BY id_materia ORDER BY causas DESC""")
            por_materia = [dict(r) for r in cur.fetchall()]
            pares_materia = []
            if ids_materia:
                cur.execute("""SELECT i.prefijo, i.anio, count(*) AS causas, bool_and(p.terminado) AS terminado
                                 FROM worker_indice_satje i
                                 JOIN worker_indice_progreso p ON p.prefijo=i.prefijo AND p.anio=i.anio
                                WHERE i.id_materia = ANY(%s)
                                GROUP BY i.prefijo, i.anio ORDER BY i.prefijo, i.anio""",
                            (list(ids_materia),))
                pares_materia = [dict(r) for r in cur.fetchall()]
    return {"progreso": prog, "por_materia": por_materia, "pares_materia": pares_materia}


def secuenciales_de_materia(prefijo: str, anio: str, ids_materia) -> list[int] | None:
    """Secuenciales indexados de la materia para (prefijo, año), ordenados.
    None si el par todavía no está indexado por completo."""
    init_tablas()
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT terminado FROM worker_indice_progreso WHERE prefijo=%s AND anio=%s",
                        (prefijo, anio))
            r = cur.fetchone()
            if not r or not r[0]:
                return None
            cur.execute("""SELECT secuencial FROM worker_indice_satje
                            WHERE prefijo=%s AND anio=%s AND id_materia = ANY(%s)
                            ORDER BY secuencial""", (prefijo, anio, list(ids_materia)))
            return [x[0] for x in cur.fetchall()]
