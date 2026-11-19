"""
Worker de BRIEFS doctrinales (gpt-4o-mini vía OpenAI).

Genera los briefs de juez, tribunal, juez de CNJ, abogado, entidad y actor.
Es el mismo trabajo que hacía `_embeddings/brief.py` en GitHub Actions; se
trajo acá porque el Action se quedó corto: pasó de tardar 35-40 minutos a no
caber en los 90 de `timeout-minutes`, y las últimas dos corridas murieron
canceladas a mitad de camino. Un job del worker no tiene ese techo, y además
se puede pausar, cancelar y mirar desde /admin/worker.

NO CONFUNDIR CON EMBEDDINGS. Esto es generación de texto con OpenAI; los
vectores los hace worker/embed_worker.py con otro proveedor y otro costo.
Los briefs solo LEEN causa_section_embeddings para agrupar criterios.

La lógica (prompts, consultas, upserts) vive sin cambios en
resolucion/briefs.py. Acá solo está la orquestación: recorrer las seis
secciones reportando progreso y atendiendo pausa/cancelación.
"""
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from resolucion import briefs as B
from worker import job_manager as jm
from worker import logger as log


def _now():
    return datetime.now(timezone.utc)


def _periodo(s) -> str:
    """'2019–2024', o un solo año si inicio y fin coinciden."""
    ini, fin = s["anio_ini"], s["anio_fin"]
    if ini is None or fin is None:
        return ""
    ini, fin = int(ini), int(fin)
    return f"{ini}–{fin}" if ini != fin else str(ini)


# Cada sección: de dónde salen los sujetos y qué se hace con cada uno.
# Mantener el MISMO orden y las MISMAS consultas que tenía brief.py::main(),
# para que migrar no cambie qué briefs se generan ni cuáles se omiten.
def _secciones():
    return [
        {
            "clave": "jueces",
            "titulo": "Jueces TDCT",
            "sql": """
                SELECT j.document_id, j.nombre
                FROM jueces j
                JOIN mv_causas_listado m ON m.juez_id = j.id
                WHERE j.published_at IS NOT NULL AND m.published_at IS NOT NULL
                GROUP BY j.document_id, j.nombre
                HAVING COUNT(*) >= %s
                ORDER BY COUNT(*) DESC
            """,
            "params": [B.MIN_CAUSAS_JUEZ],
            "datos":  lambda cur, r: B.get_juez_data(cur, r["document_id"]),
            "prompt": lambda d: (B.SYS_JUEZ, B.build_juez_prompt(d)),
            "guardar": lambda wc, r, d, per, br: B.upsert_juez_brief(
                wc, r["document_id"], r["nombre"], int(d["stats"]["total"]), per, br),
        },
        {
            "clave": "tribunales",
            "titulo": "Tribunales",
            "sql": """
                SELECT t.document_id, t.nombre
                FROM tribunales t
                WHERE t.published_at IS NOT NULL
                ORDER BY t.nombre
            """,
            "params": [],
            "datos":  lambda cur, r: B.get_tribunal_data(cur, r["document_id"]),
            "prompt": lambda d: (B.SYS_TRIBUNAL, B.build_tribunal_prompt(d)),
            "guardar": lambda wc, r, d, per, br: B.upsert_tribunal_brief(
                wc, r["document_id"], r["nombre"], int(d["stats"]["total"]), per, br),
        },
        {
            "clave": "cnj",
            "titulo": "Jueces CNJ",
            "sql": """
                SELECT j.document_id, j.nombre
                FROM jueces j
                JOIN mv_causas_listado m ON m.casacion_juez_id = j.id
                WHERE j.published_at IS NOT NULL AND m.published_at IS NOT NULL
                  AND m.casacion_decision_sigla IS NOT NULL
                GROUP BY j.document_id, j.nombre
                ORDER BY COUNT(*) DESC
            """,
            "params": [],
            "datos":  lambda cur, r: B.get_cnj_juez_data(cur, r["document_id"]),
            "prompt": lambda d: (B.SYS_CNJ, B.build_cnj_prompt(d)),
            "guardar": lambda wc, r, d, per, br: B.upsert_cnj_brief(
                wc, r["document_id"], r["nombre"], int(d["stats"]["total"]), per, br),
        },
        # Abogados va por lado: un mismo abogado puede tener perfil distinto
        # litigando como actor que como demandado.
        *[
            {
                "clave": "abogados",
                "titulo": f"Abogados ({lado})",
                "sql": f"""
                    SELECT ab.document_id, ab.nombre
                    FROM {B.abogado_link_table(lado)} lnk
                    JOIN abogados ab ON ab.id = lnk.abogado_id
                    JOIN mv_causas_listado mv ON mv.id = lnk.causa_id
                    WHERE mv.decision_sigla IS NOT NULL AND mv.published_at IS NOT NULL
                    GROUP BY ab.document_id, ab.nombre
                    HAVING COUNT(DISTINCT mv.id) >= %s
                    ORDER BY COUNT(DISTINCT mv.id) DESC
                """,
                "params": [B.MIN_CAUSAS_ABOGADO],
                "datos":  (lambda l: lambda cur, r: B.get_abogado_data(cur, r["document_id"], l))(lado),
                "prompt": lambda d: (B.SYS_ABOGADO, B.build_abogado_prompt(d)),
                "guardar": (lambda l: lambda wc, r, d, per, br: B.upsert_abogado_brief(
                    wc, r["document_id"], l, r["nombre"], int(d["stats"]["total"]), per, br))(lado),
            }
            for lado in ("actor", "demandado")
        ],
        {
            "clave": "entidades",
            "titulo": "Entidades",
            "sql": """
                SELECT e.document_id, e.nombre
                FROM entidades e
                JOIN mv_causas_listado mv ON mv.entidad_demandada_id = e.id
                WHERE mv.decision_sigla IS NOT NULL AND mv.published_at IS NOT NULL
                GROUP BY e.document_id, e.nombre
                HAVING COUNT(DISTINCT mv.id) >= %s
                ORDER BY COUNT(DISTINCT mv.id) DESC
            """,
            "params": [B.MIN_CAUSAS_ENTIDAD],
            "datos":  lambda cur, r: B.get_entidad_data(cur, r["document_id"]),
            "prompt": lambda d: (B.SYS_ENTIDAD, B.build_entidad_prompt(d)),
            "guardar": lambda wc, r, d, per, br: B.upsert_entidad_brief(
                wc, r["document_id"], r["nombre"], int(d["stats"]["total"]), per, br),
        },
        {
            "clave": "actores",
            "titulo": "Actores",
            "sql": """
                SELECT c.actor_normalizado AS nombre, MAX(c.tipo_actor) AS tipo
                FROM causas c
                JOIN mv_causas_listado mv ON mv.document_id = c.document_id
                WHERE mv.decision_sigla IS NOT NULL AND mv.published_at IS NOT NULL
                  AND c.actor_normalizado IS NOT NULL
                GROUP BY c.actor_normalizado
                HAVING COUNT(DISTINCT mv.id) >= %s
                ORDER BY COUNT(DISTINCT mv.id) DESC
            """,
            "params": [B.MIN_CAUSAS_ACTOR],
            # Actores se identifican por nombre, no por document_id.
            "datos":  lambda cur, r: B.get_actor_data(cur, r["nombre"]),
            "prompt": lambda d: (B.SYS_ACTOR, B.build_actor_prompt(d)),
            "guardar": lambda wc, r, d, per, br: B.upsert_actor_brief(
                wc, r["nombre"], r["tipo"], int(d["stats"]["total"]), per, br),
        },
    ]


def ejecutar_job_brief(job_id: int, pause_ev, kill_ev, only: str = ""):
    """Genera los briefs. `only` acota a una sección (jueces, tribunales, cnj,
    abogados, entidades, actores); vacío = todas."""
    if not B.OPENAI_KEY:
        log.error("OPENAI_API_KEY no está seteada en el worker", job_id=job_id)
        jm._actualizar(job_id, estado="error", completado_en=_now())
        return

    inicio = _now()
    only = (only or "").lower()
    secciones = [s for s in _secciones() if not only or s["clave"] == only]
    if not secciones:
        log.error(f"Sección '{only}' no existe", job_id=job_id)
        jm._actualizar(job_id, estado="error", completado_en=_now())
        return

    log.info(f"Briefs — modelo={B.MODEL} secciones={[s['titulo'] for s in secciones]}",
             job_id=job_id)

    conn = psycopg2.connect(jm.DATABASE_URL)  # conexión propia y larga, fuera del pool
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    wc = conn.cursor()
    hechos = omitidos = errores = 0

    try:
        for sec in secciones:
            cur.execute(sec["sql"], sec["params"])
            filas = cur.fetchall()
            log.info(f"{sec['titulo']}: {len(filas)} sujeto(s)", job_id=job_id)

            for fila in filas:
                if kill_ev.is_set():
                    jm._actualizar(job_id, estado="cancelado", completado_en=_now())
                    log.info(f"Cancelado — {hechos} brief(s) generados antes de parar",
                             job_id=job_id)
                    return
                if pause_ev.is_set():
                    jm._actualizar(job_id, estado="pausado")
                    log.info(f"Pausado — {hechos} brief(s) generados", job_id=job_id)
                    return

                nombre = fila.get("nombre", "?")
                try:
                    datos = sec["datos"](cur, fila)
                    if not datos:
                        omitidos += 1
                        continue
                    sys_p, user_p = sec["prompt"](datos)
                    brief = B.call_openai(sys_p, user_p)
                    sec["guardar"](wc, fila, datos, _periodo(datos["stats"]), brief)
                    conn.commit()
                    hechos += 1
                except Exception as e:
                    # Un sujeto que falla no tumba el job: se registra y sigue.
                    # El rollback es imprescindible o la transacción queda
                    # abortada y todo lo que venga después también falla.
                    conn.rollback()
                    errores += 1
                    log.error(f"{sec['titulo']} — {nombre}: {type(e).__name__}: {e}",
                              job_id=job_id)

                jm._actualizar(job_id, total_procesadas=hechos + omitidos + errores,
                               exitosas=hechos, errores=errores, omitidas=omitidos)
                jm._emitir_sse(job_id, {"tipo": "progreso", "seccion": sec["titulo"],
                                        "hechos": hechos, "errores": errores})
    finally:
        cur.close(); wc.close(); conn.close()

    estado = "completado" if errores == 0 else "completado_con_errores"
    jm._actualizar(job_id, estado=estado, completado_en=_now(),
                   total_procesadas=hechos + omitidos + errores,
                   exitosas=hechos, errores=errores, omitidas=omitidos)
    seg = (_now() - inicio).total_seconds()
    log.info(f"Briefs listo: {hechos} generados · {omitidos} omitidos · "
             f"{errores} con error · {seg/60:.1f} min", job_id=job_id)
