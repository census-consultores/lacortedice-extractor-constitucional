"""
Re-consulta los números que el índice de SATJE anotó como de OTRA materia,
para encontrar los juicios de esta materia escondidos detrás.

Por qué: un mismo número puede ser varios juicios en SATJE (36% de una
muestra). El índice guardaba solo el primer resultado, y SATJE ordena del más
nuevo al más viejo: si el juicio de la materia era más viejo que otro con el
mismo número, quedaba anotado como de otra materia y la barrida "desde índice"
nunca lo visitaba. Muestra al azar de 400 números de otra materia: 31 (7,8%)
esconden un juicio civil (~45 mil proyectado).

Qué hace, por número (una consulta de cabecera, sin PDF ni actuaciones):
  - si algún resultado es de la materia → corrige worker_indice_satje;
  - lo anota en worker_indice_rerevision (se puede cortar y retomar).
Al final de cada lote, para los números encontrados crea jobs de descarga
("reintento", solo esos números) SOLO si el job "desde índice" de su
(juzgado, año) ya arrancó o terminó; si sigue pendiente, lo leerá del índice
corregido al arrancar.

Corre como job del worker (workers="reindexar_otra_materia", categoría
"indice", cupo propio): se crea en pendiente y lo lanza la cola.
Prueba local con el script (DATABASE_URL y BDATA_DATACENTER_PROXY_URL):
  python scripts/reindexar_otra_materia.py --prefijo 14305 --anio 2016 [--sin-jobs]
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2

import api
from config import materia as MATERIA
from worker import job_manager as jm
from worker import logger as log

LOTE = 2000


def _tabla():
    with jm._conn() as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS worker_indice_rerevision (
                         numero TEXT PRIMARY KEY,
                         encontrado BOOLEAN NOT NULL,
                         encolado BOOLEAN NOT NULL DEFAULT false,
                         revisado_en TIMESTAMPTZ NOT NULL DEFAULT now())""")


def _todos(numero: str) -> list | None:
    body = {"numeroCausa": api.normalizar_numero(numero),
            "actor": {"cedulaActor": "", "nombreActor": ""},
            "demandado": {"cedulaDemandado": "", "nombreDemandado": ""},
            "provincia": "", "canton": None, "materia": None, "dependencia": None,
            "numeroFiscalia": "", "first": 1, "pageSize": 10, "recaptcha": "verdad"}
    for intento in (1, 2, 3):
        try:
            r = api._post(f"{api.BASE_CAUSAS}/informacion/buscarCausas?page=1&size=10",
                          headers=api.HEADERS, json=body, timeout=(8, 20))
            r.raise_for_status()
            return r.json() or []
        except api.SatjeNoDisponible:
            raise
        except Exception:
            time.sleep(3 * intento)
    return None          # sin respuesta: queda sin anotar, se reintenta en otra corrida


def _pendientes(prefijo, anio, limite):
    ids = list(MATERIA.IDS_MATERIA_SATJE)
    sql = """SELECT i.numero FROM worker_indice_satje i
              WHERE NOT (coalesce(i.id_materia, -1) = ANY(%s))
                AND EXISTS (SELECT 1 FROM worker_indice_satje m
                             WHERE m.prefijo = i.prefijo AND m.id_materia = ANY(%s))
                AND NOT EXISTS (SELECT 1 FROM worker_indice_rerevision r WHERE r.numero = i.numero)"""
    args = [ids, ids]
    if prefijo:
        sql += " AND i.prefijo = %s"; args.append(prefijo)
    if anio:
        sql += " AND i.anio = %s"; args.append(anio)
    sql += " ORDER BY i.prefijo, i.anio, i.secuencial"
    if limite:
        sql += f" LIMIT {int(limite)}"
    with jm._conn() as c, c.cursor() as cur:
        cur.execute(sql, args)
        return [r[0] for r in cur.fetchall()]


def _encolar(marca: str) -> int:
    """Jobs para los encontrados cuyo job desde_indice ya arrancó o terminó."""
    with jm._conn() as c, c.cursor() as cur:
        cur.execute("""SELECT r.numero FROM worker_indice_rerevision r
                        WHERE r.encontrado AND NOT r.encolado
                          AND NOT EXISTS (SELECT 1 FROM worker_jobs j
                                           WHERE j.workers = 'desde_indice'
                                             AND j.tribunal = split_part(r.numero,'-',1)
                                             AND j.año = split_part(r.numero,'-',2)
                                             AND j.estado IN ('pendiente','pausado'))""")
        nums = [x[0] for x in cur.fetchall()]
    if not nums:
        return 0
    # crear_job con arrancar=False: los lanza la cola del worker, no este proceso.
    import json
    grupos = {}
    for n in nums:
        t, a, s = n.split("-")
        grupos.setdefault((t, a), []).append(int(s))
    for (t, a), secs in sorted(grupos.items()):
        jm.crear_job(tribunal=t, año=a, desde=min(secs), hasta=max(secs), dry_run=False,
                     modelo="gpt-4o", iniciado_por=marca, workers="reintento",
                     solo_descarga=True, parametros=json.dumps({"numeros": sorted(secs)}),
                     arrancar=False)
    with jm._conn() as c, c.cursor() as cur:
        cur.execute("UPDATE worker_indice_rerevision SET encolado = true WHERE numero = ANY(%s)", (nums,))
    return len(nums)


def correr(prefijo=None, anio=None, limite=0, paralelo=8, crear_jobs=True,
           detener: threading.Event | None = None, al_avanzar=None) -> dict:
    """Recorre los pendientes por lotes. `detener` corta entre números (pausa o
    kill del job); `al_avanzar(tot)` se llama tras cada lote."""
    _tabla()
    ids = MATERIA.IDS_MATERIA_SATJE
    detener = detener or threading.Event()
    tot = {"revisados": 0, "encontrados": 0, "sin_respuesta": 0, "encolados": 0}
    lock = threading.Lock()

    def uno(numero):
        # Con SATJE caído o el freno de emergencia activo se ESPERA, como la
        # barrida; antes la excepción subía y abortaba el job entero (26-oct
        # 04:28: SATJE sin respuesta unos minutos, el re-índice quedó en error).
        while True:
            if detener.is_set():
                return
            try:
                res = _todos(numero)
                break
            except api.SatjeNoDisponible:
                detener.wait(30)
        if res is None:
            with lock: tot["sin_respuesta"] += 1
            return
        propio = next((x for x in res if x.get("idMateria") in ids), None)
        # Una conexión que la base cortó estando inactiva falla una vez y se
        # descarta (jm._conn); se reintenta con otra. El 27-oct 14:32 un
        # "connection already closed" abortó el job entero con 472 mil revisados.
        for intento in range(3):
            try:
                with jm._conn() as c, c.cursor() as cur:
                    if propio:
                        cur.execute("""UPDATE worker_indice_satje SET id_materia = %s, materia = %s
                                        WHERE numero = %s""",
                                    (propio["idMateria"], (propio.get("nombreDelito") or "")[:200], numero))
                    cur.execute("""INSERT INTO worker_indice_rerevision (numero, encontrado)
                                   VALUES (%s, %s) ON CONFLICT (numero) DO NOTHING""", (numero, bool(propio)))
                break
            except (psycopg2.InterfaceError, psycopg2.OperationalError):
                if intento == 2:
                    with lock: tot["sin_respuesta"] += 1
                    return              # queda sin anotar: lo toma la próxima corrida
                detener.wait(2)
        with lock:
            tot["revisados"] += 1
            tot["encontrados"] += bool(propio)

    while not detener.is_set():
        lote = _pendientes(prefijo, anio, min(LOTE, limite or LOTE))
        if not lote:
            break
        with ThreadPoolExecutor(paralelo) as ex:
            list(ex.map(uno, lote))
        if crear_jobs:
            tot["encolados"] += _encolar("reindexar-otra-materia")
        if al_avanzar:
            al_avanzar(tot)
        if limite and tot["revisados"] >= limite:
            break
    return tot


def ejecutar_job_reindexar(job_id: int, pause_ev, kill_ev):
    p = json.loads((jm.obtener_job(job_id) or {}).get("parametros") or "{}")
    paralelo = max(1, min(int(p.get("hilos", 10)), 16))
    detener = threading.Event()

    def _vigilar():
        while not detener.is_set():
            if kill_ev.is_set() or pause_ev.is_set():
                detener.set()
            time.sleep(1)
    threading.Thread(target=_vigilar, daemon=True).start()

    def _avance(tot):
        jm._actualizar(job_id, total_procesadas=tot["revisados"], exitosas=tot["encontrados"],
                       errores=tot["sin_respuesta"], checkpoint=tot["encolados"])
        log.info(f"Re-índice: {tot}", job_id=job_id)

    log.info(f"Re-índice de números de otra materia — {paralelo} hilos", job_id=job_id)
    tot = correr(prefijo=p.get("prefijo"), anio=p.get("anio"), paralelo=paralelo,
                 crear_jobs=not p.get("sin_jobs"), detener=detener, al_avanzar=_avance)
    detener.set()
    if kill_ev.is_set():
        jm._actualizar(job_id, estado="cancelado", completado_en=jm._now())
    elif pause_ev.is_set():
        jm._actualizar(job_id, estado="pausado")
    else:
        jm._actualizar(job_id, estado="completado", completado_en=jm._now())
        log.info(f"Re-índice terminado: {tot}", job_id=job_id)
