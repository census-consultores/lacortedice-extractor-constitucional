"""
Worker de EMBEDDINGS por sección (bge-m3 vía OpenRouter → DeepInfra).

Llena causa_section_embeddings. Idempotente: solo (re)embebe secciones nuevas
o cuyo contenido cambió (content_hash). Es el mismo trabajo que hacía
`_embeddings/backfill.py` en GitHub Actions, traído acá por tres razones:

  - El Action moría a los 60 minutos; un re-embebido completo no entra.
  - El worker ya tiene OPENROUTER_API_KEY y ya escribe en esta misma base.
  - Una causa recién extraída nacía SIN vectores y se quedaba así hasta las
    4 AM: no aparecía en "ver similares" ni en la jurimetría semántica. Acá se
    puede encadenar al final de la extracción y nace completa.

MODELO — ojo al elegirlo
  `BAAI/bge-m3-multi` NO es un bge-m3 mejor: es el mismo modelo con un tope de
  entrada de 512 tokens. Se le mandaban 16.000 caracteres y cortaba en ~2.000
  sin avisar. `baai/bge-m3` acepta los 8.192 completos. Cambiar de uno a otro
  exige correr una vez con modo="todo" para que el corpus quede bajo un solo
  régimen; si no, quedan mezclados vectores truncados con vectores completos.

LOTEO — por qué no se mandan 32 textos de una
  OpenRouter aplica el límite de contexto del modelo a la SUMA del lote, no a
  cada texto por separado (DeepInfra directo lo hacía por texto). Un lote que
  pase de ~8.192 tokens hace que descarte todos los endpoints y responda un
  404 "No endpoints found", que no dice nada de la causa real. Por eso los
  lotes se arman por presupuesto de caracteres, no por cantidad fija.
"""
import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

import psycopg2.extras
from psycopg2.extras import execute_values

from worker import job_manager as jm
from worker import logger as log

API_TOKEN = os.environ.get("OPENROUTER_API_KEY", "") or os.environ.get("DEEPINFRA_API_KEY", "")
API_URL   = os.environ.get("EMBED_API_URL", "https://openrouter.ai/api/v1/embeddings")
MODEL     = os.environ.get("EMBED_MODEL", "baai/bge-m3")
# Proveedor fijo: el corpus se construyó contra DeepInfra. Otro backend podría
# devolver vectores de un espacio distinto, y eso no da error — degrada la
# búsqueda en silencio. (Medido: DeepInfra y Parasail dan coseno 0,99999 entre
# sí, así que el riesgo es bajo, pero fijarlo no cuesta nada.)
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "DeepInfra")

MAX_CHARS         = 16000   # ~4.000 tokens por texto — entra en los 8.192
CHARS_POR_REQUEST = 24000   # ~6.000 tokens por lote, con margen bajo el tope
BATCH             = 32      # tope duro de textos por request
CHUNK             = 200     # guardar cada N secciones → resumible
MIN_CHARS         = 30
EMBEDDING_DIM     = 1024
MAX_RETRIES       = 4
RETRY_DELAY       = 8

SECTIONS = {
    "resumen":                    "c.resumen_del_caso",
    "antecedentes":               "c.antecedentes",
    "objeto_controversia":        "c.objeto_de_controversia",
    "acto_impugnado":             "c.acto_impugnado",
    "otros_actos":                "c.otros_actos_administrativos",
    "argumentos_actor":           "c.argumentos_del_actor",
    "argumentos_demandado":       "c.argumentos_del_demandado",
    "criterio_sala":              "c.criterio_sala_para_resolver",
    "jurisprudencia":             "c.jurisprudencia_mencionada",
    "pruebas_actor":              "c.pruebas_del_actor",
    "pruebas_demandado":          "c.pruebas_del_demandado",
    # Solo hay contenido real en votos salvados genuinos (~10% de las causas) —
    # el resto es "" o "No consta en la sentencia." (26 chars), ambos bajo
    # MIN_CHARS, así que el filtro de longitud ya existente los excluye solo.
    "voto_salvado":               "c.argumento_juez_salvado",
    "cas_antecedentes":           "ca.antecedentes",
    "cas_argumentos_recurrente":  "ca.argumentos_del_recurrente",
    "cas_argumentos_contraparte": "ca.argumentos_contraparte",
    "cas_criterio_sala":          "ca.criterio_sala_para_resolver",
    "cas_jurisprudencia":         "ca.jurisprudencia_mencionada",
    "cas_voto_salvado":           "ca.argumento_juez_salvado",
}


def _now():
    return datetime.now(timezone.utc)


def _hash(texto: str) -> str:
    # sha256 COMPLETO: debe coincidir byte a byte con el content_hash que
    # guardó el backfill original, o ninguna sección matchea y el modo
    # "faltantes" re-embebe el corpus entero creyendo que falta todo.
    return hashlib.sha256(texto.encode()).hexdigest()


def _vec_a_str(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def armar_lotes(items: list):
    """Agrupa items en lotes que no excedan CHARS_POR_REQUEST ni BATCH textos.

    Un texto que solo ya supere el presupuesto igual va en su propio lote:
    recortarlo acá escondería el problema, y es preferible que falle a guardar
    el vector de un texto mutilado sin que nadie se entere.
    """
    lote, chars = [], 0
    for it in items:
        largo = len(it[2])
        if lote and (chars + largo > CHARS_POR_REQUEST or len(lote) >= BATCH):
            yield lote
            lote, chars = [], 0
        lote.append(it)
        chars += largo
    if lote:
        yield lote


def embed_batch(texts: list) -> list:
    """Devuelve un vector de EMBEDDING_DIM floats por cada texto del lote."""
    if not API_TOKEN:
        raise RuntimeError("OPENROUTER_API_KEY no está seteada en el worker")
    cuerpo = {"model": MODEL, "input": texts, "encoding_format": "float"}
    if "openrouter.ai" in API_URL and EMBED_PROVIDER:
        cuerpo["provider"] = {"order": [EMBED_PROVIDER], "allow_fallbacks": False}
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(cuerpo).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_TOKEN}"},
    )
    for intento in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read())
            vecs = [item["embedding"] for item in data["data"]]
            malas = [len(v) for v in vecs if len(v) != EMBEDDING_DIM]
            if malas:
                raise ValueError(f"dim inesperada {malas[0]}, esperado {EMBEDDING_DIM}")
            return vecs
        except Exception as e:
            detalle = ""
            if hasattr(e, "read"):
                try:
                    detalle = " | " + e.read().decode()[:160]
                except Exception:
                    pass
            if intento < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (intento + 1))
                continue
            raise RuntimeError(f"embeddings: {e}{detalle}") from e


def _pendientes(conn, modo: str, juicios: list | None) -> list:
    """Devuelve [(document_id, section, texto, hash)] que hay que (re)embeber.

    modo="faltantes" (default) → solo secciones nuevas o con contenido cambiado.
    modo="todo"                → todas, ignorando el hash (al cambiar de modelo).
    juicios                    → acota a esas causas (lo usa el encadenado tras
                                 una extracción: embeber solo lo recién creado).
    """
    cols = ",\n            ".join(f"{expr} AS {name}" for name, expr in SECTIONS.items())
    filtro, params = "", []
    if juicios:
        filtro = "AND c.juicio = ANY(%s)"
        params.append(juicios)
    # DISTINCT ON + el LEFT JOIN de casaciones: igual que el backfill que
    # construyó el corpus. Sin el join faltarían las 7 secciones "cas_*".
    sql = f"""
        SELECT DISTINCT ON (c.document_id) c.document_id,
            {cols}
        FROM causas c
        LEFT JOIN casaciones_causa_lnk lnk ON lnk.causa_id = c.id
        LEFT JOIN casaciones ca ON ca.id = lnk.casacion_id AND ca.published_at IS NOT NULL
        WHERE c.published_at IS NOT NULL
        {filtro}
        ORDER BY c.document_id, c.published_at DESC NULLS LAST
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        filas = cur.fetchall()

    vigentes = {}
    if modo != "todo":
        with conn.cursor() as cur:
            cur.execute("SELECT document_id, section, content_hash FROM causa_section_embeddings")
            vigentes = {(d, s): h for d, s, h in cur.fetchall()}

    # El orden importa y debe calcar al del backfill que construyó el corpus:
    # colapsar espacios PRIMERO, después medir MIN_CHARS, después recortar y
    # recién ahí hashear. Si acá se usara .strip() en vez de colapsar, el hash
    # de cualquier sección con saltos de línea no coincidiría con el guardado
    # y el modo "faltantes" re-embebería medio corpus sin necesidad.
    unidades = []
    for f in filas:
        doc = f["document_id"]
        for sec in SECTIONS:
            texto = " ".join((f.get(sec) or "").split())
            if len(texto) >= MIN_CHARS:
                unidades.append((doc, sec, texto[:MAX_CHARS]))

    todo = [(d, sec, t, _hash(t)) for (d, sec, t) in unidades
            if modo == "todo" or vigentes.get((d, sec)) != _hash(t)]

    # Huérfanos: secciones que alguna vez tuvieron texto y ya no. Si no se
    # borran, queda un vector viejo apuntando a contenido que ya no existe y
    # sigue apareciendo en "ver similares".
    # SOLO en una pasada completa. Con `juicios` acotado, `vigentes` trae TODO
    # el corpus pero `unidades` solo las causas pedidas: todo lo demás parecería
    # huérfano y se borraría. Esto ya pasó una vez y se llevó 60.455 vectores.
    if modo != "todo" and vigentes and not juicios:
        actuales = {(d, sec) for (d, sec, _) in unidades}
        huerfanos = [k for k in vigentes if k not in actuales]
        if huerfanos:
            # execute_values y no un IN con tupla: con decenas de miles de
            # pares, el IN plano hace que Postgres aborte con
            # "stack depth limit exceeded".
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    "DELETE FROM causa_section_embeddings WHERE (document_id, section) IN (VALUES %s)",
                    huerfanos,
                )
            conn.commit()
            log.info(f"huérfanos borrados: {len(huerfanos)}")
    return todo


def ejecutar_job_embeddings(job_id: int, pause_ev, kill_ev,
                            modo: str = "faltantes", juicios: list | None = None):
    """Job de embeddings. Comparte los mismos slots que la extracción: si hay
    4 corriendo queda 'pendiente' y arranca cuando se libere uno."""
    inicio = _now()
    log.info(f"Embeddings — modo={modo} modelo={MODEL} "
             f"acotado={'sí (' + str(len(juicios)) + ' causas)' if juicios else 'no'}",
             job_id=job_id)

    with jm._conn() as conn:
        todo = _pendientes(conn, modo, juicios)

    total = len(todo)
    if total == 0:
        jm._actualizar(job_id, estado="completado", completado_en=_now(),
                       total_procesadas=0, exitosas=0)
        log.info("Nada nuevo que embeber ✓", job_id=job_id)
        return

    log.info(f"{total} sección(es) por embeber", job_id=job_id)
    hechas, errores = 0, 0

    for ini in range(0, total, CHUNK):
        if kill_ev.is_set():
            jm._actualizar(job_id, estado="cancelado", completado_en=_now())
            log.info(f"Cancelado — {hechas}/{total} guardadas antes de parar", job_id=job_id)
            return
        if pause_ev.is_set():
            jm._actualizar(job_id, estado="pausado")
            log.info(f"Pausado — {hechas}/{total} guardadas", job_id=job_id)
            return

        tramo = todo[ini: ini + CHUNK]
        filas = []
        for lote in armar_lotes(tramo):
            # Un lote que falla no tumba el job: se registra y se sigue. Dejar
            # 200 secciones sin guardar por un error transitorio es peor que
            # perder ese lote, que el próximo run vuelve a tomar por hash.
            try:
                vecs = embed_batch([t for (_, _, t, _) in lote])
            except Exception as e:
                errores += len(lote)
                log.error(f"Lote de {len(lote)} secciones falló: {e}", job_id=job_id)
                continue
            for i, (doc, sec, _, h) in enumerate(lote):
                filas.append((doc, sec, _vec_a_str(vecs[i]), h))

        if filas:
            with jm._conn() as conn:
                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO causa_section_embeddings
                            (document_id, section, embedding, content_hash, updated_at)
                        VALUES %s
                        ON CONFLICT (document_id, section) DO UPDATE
                            SET embedding    = EXCLUDED.embedding,
                                content_hash = EXCLUDED.content_hash,
                                updated_at   = NOW()
                    """, [(d, s, v, h, datetime.now(timezone.utc)) for d, s, v, h in filas])
                conn.commit()
            hechas += len(filas)

        jm._actualizar(job_id, total_procesadas=hechas + errores, exitosas=hechas,
                       errores=errores)
        jm._emitir_sse(job_id, {"tipo": "progreso", "hechas": hechas,
                                "total": total, "errores": errores})
        log.info(f"guardadas {hechas}/{total}", job_id=job_id)

    estado = "completado" if errores == 0 else "completado_con_errores"
    jm._actualizar(job_id, estado=estado, completado_en=_now(),
                   total_procesadas=hechas + errores, exitosas=hechas, errores=errores)
    seg = (_now() - inicio).total_seconds()
    log.info(f"Embeddings listo: {hechas}/{total} en {seg/60:.1f} min"
             + (f" · {errores} con error" if errores else ""), job_id=job_id)
