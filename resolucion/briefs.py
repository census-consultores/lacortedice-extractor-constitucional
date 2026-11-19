"""
Briefs doctrinales — BIBLIOTECA (sin orquestación).

Copia literal de `_embeddings/brief.py` salvo por el `main()`, que acá no va:
la orquestación la hace `worker/brief_worker.py`, que necesita reportar
progreso y poder pausarse o cancelarse como cualquier otro job.

OJO: esto NO genera embeddings. Es generación de texto con OpenAI
(gpt-4o-mini). Solo LEE causa_section_embeddings para agrupar criterios de un
juez. El embebido es otra cosa y vive en worker/embed_worker.py.

Al tocar este archivo, acordate de que el original en la webapp quedó como
referencia histórica: la fuente de verdad es esta.
"""

from config import materia as MATERIA
import json
import os
import time
import urllib.request
import psycopg2
import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from psycopg2.extras import RealDictCursor, Json

DSN = os.environ.get("EMBED_DSN", "postgres://strapi:strapi@localhost:5433/strapi")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY") or ""
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
# Minimo de causas para clusterizar criterio (no solo para generar brief).
# Validado en analisis ad-hoc con Ferrin de la Torre (279 causas, K=3-6 con
# silhouette razonable). Con menos de esto, K=3 dejaria clusters de <15
# causas cada uno — demasiado chico para ser un grupo real, no ruido — asi
# que se usa el fallback de muestra cronologica (comportamiento anterior).
MIN_CAUSAS_CLUSTER = int(os.environ.get("BRIEF_MIN_CAUSAS_CLUSTER", "40"))
# Qwen por OpenRouter — el mismo modelo que la extracción. Se dejó
# BRIEF_MODEL solo para que el log diga qué se usó.
MODEL = os.environ.get("BRIEF_MODEL", "qwen/qwen3-235b-a22b-2507")
# Flujo de proveedores: "alibaba" (default, mejor uptime medido), "novita" o
# "gmicloud" (~42% más barato y con cache de prompt).
BRIEF_FLUJO = os.environ.get("BRIEF_FLUJO", "alibaba")
# 3000 era el tope con gpt-4o-mini; se mantiene. Si un brief se trunca, el
# error lo dice explícitamente en vez de guardar JSON a medias.
BRIEF_MAX_TOKENS = int(os.environ.get("BRIEF_MAX_TOKENS", "3000"))
MIN_CAUSAS_JUEZ = int(os.environ.get("BRIEF_MIN_CAUSAS", "15"))
BRIEF_ONLY = os.environ.get("BRIEF_ONLY", "")  # "jueces" | "tribunales" | "cnj" | "abogados" | "entidades" | ""
MAX_RETRIES = 3
RETRY_DELAY = 10
TEMPERATURE = 0.2


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def _json_de_texto(raw: str) -> dict:
    """Saca el JSON de la respuesta de Qwen.

    gpt-4o-mini se llamaba con response_format=json_object, que garantiza JSON
    válido. Qwen vía OpenRouter no acepta ese parámetro, así que a veces
    devuelve el objeto envuelto en un bloque markdown. Mismo desenvuelto que
    usa el pipeline de extracción (resolucion/ai/tribunal.py).
    """
    raw = (raw or "").strip()
    if "```" in raw:
        for parte in raw.split("```"):
            parte = parte.strip()
            if parte.lower().startswith("json"):
                parte = parte[4:].strip()
            if parte.startswith("{"):
                try:
                    return json.loads(parte)
                except json.JSONDecodeError:
                    continue
    # Último recurso: recortar desde la primera llave hasta la última.
    if not raw.startswith("{") and "{" in raw and "}" in raw:
        raw = raw[raw.index("{"): raw.rindex("}") + 1]
    return json.loads(raw)


def call_openai(sys_prompt: str, user_prompt: str) -> dict:
    """Pide el brief al modelo. El nombre quedó por compatibilidad: desde que
    la cuenta de OpenAI se quedó sin saldo, esto habla con Qwen por OpenRouter
    — el MISMO modelo y el mismo flujo de proveedores que usa la extracción,
    así que hay un solo saldo que vigilar en vez de tres.

    Precio: prácticamente igual a gpt-4o-mini por Alibaba ($0,150/$0,598 por
    millón contra $0,150/$0,600). El cambio no fue por costo sino por saldo.
    """
    from resolucion.ai._cliente import (
        llamar_qwen, cargar_api_key_openrouter, resolver_orden_proveedores,
        OPENROUTER_MODELO_QWEN,
    )
    texto, finish = llamar_qwen(
        prompt=user_prompt,
        sistema=sys_prompt,
        max_tokens=BRIEF_MAX_TOKENS,
        api_key=cargar_api_key_openrouter(),
        modelo=OPENROUTER_MODELO_QWEN,
        orden_proveedores=resolver_orden_proveedores(BRIEF_FLUJO),
        temperatura=TEMPERATURE,
    )
    # llamar_qwen NO levanta excepción cuando la API falla: devuelve
    # finish="error" y el mensaje de error COMO TEXTO. Sin este chequeo ese
    # texto llega a _json_de_texto y se reporta como "JSON inválido",
    # escondiendo la causa real (visto en vivo: un 402 de OpenRouter por
    # saldo reservado se leyó como error de parseo).
    if finish == "error":
        raise RuntimeError(f"la API falló: {texto[:300]}")
    if finish == "length":
        # Un brief truncado es un JSON roto: mejor fallar y que el job lo
        # registre, que guardar medio brief como si estuviera completo.
        raise RuntimeError(
            f"brief truncado: se alcanzó max_tokens={BRIEF_MAX_TOKENS}"
        )
    return _json_de_texto(texto)


SYS_JUEZ = f"""Eres un analista jurídico especializado en derecho {MATERIA.NOMBRE.lower()} ecuatoriano.
Generás briefs de inteligencia judicial objetivos y accionables sobre jueces de primera instancia ({MATERIA.NOMBRE_PRIMERA_INSTANCIA}).
Basáte SOLO en los datos del prompt. No inventés casos ni argumentos.
Respondé en español formal.

REGLA DE ORO: cada afirmación debe ser CONCRETA Y VERIFICABLE contra los datos del prompt.
Nunca una generalidad vacía: "argumentación bien fundamentada", "normativa vigente", "pruebas
sólidas", "enfoque meticuloso", "adecuada justificación". Si no tenés un hecho concreto detrás
—un juicio, una prueba, una norma, una cifra del prompt— no escribas el punto. Preferí tres
afirmaciones específicas a ocho vagas.

NO REPITAS LA ESTADÍSTICA que ya está en el prompt: el lector la tiene arriba. Tu trabajo es
decir QUÉ hacer con ella, no volver a contarla.

EXTENSIÓN: máximo 120 palabras por sección, en 1 o 2 párrafos. Esto no es un resumen que se
pueda estirar: si no te alcanza, sacá adjetivos, muletillas y frases de relleno — nunca hechos
ni citas. Una sección corta y específica es mejor que una larga y genérica.
CITÁ LAS SENTENCIAS. Cuando afirmes un criterio que viene de un cluster o de un
ejemplo del prompt, poné su número de juicio entre corchetes al final de la frase,
así: [09501-2021-00559]. Usá SOLO números que aparezcan literalmente en el prompt
— no inventes ni completes ninguno. Una afirmación con cita vale más que tres sin
ella: el lector tiene que poder abrir la sentencia y verificarla.
Devolvé SOLO el JSON pedido."""

SYS_TRIBUNAL = f"""Eres un analista jurídico especializado en derecho {MATERIA.NOMBRE.lower()} ecuatoriano.
Generás briefs de inteligencia judicial sobre los órganos de primera instancia ({MATERIA.NOMBRE_PRIMERA_INSTANCIA}) como institución.

REGLA DE ORO: cada afirmación que hagas debe ser CONCRETA Y VERIFICABLE contra los datos del
prompt — nunca una generalidad vacía ("argumentación bien fundamentada", "normativa vigente",
"enfoque meticuloso", "pruebas sólidas" sin decir cuáles). Si no tenés un hecho concreto
(un juicio real, una prueba específica, una norma específica) que sustente un punto, no lo
escribas.

Para "argumentos_exitosos" y "argumentos_fallidos": cada punto debe nombrar el TIPO DE PRUEBA
(tipo_prueba_clave) y/o la NORMA (norma_clave) que se repite en las extracciones que te paso —
no "presentan pruebas sólidas" sino qué prueba concreta (facturas, comprobantes de pago,
certificados, etc.) y qué norma. Ancla cada punto a un `document_id` real de la lista de
extracciones. Priorizá diversidad de eje_decision: si cinco causas ganadas comparten el mismo
eje, elegí la más representativa y usá las otras para cubrir ejes distintos.

Para "jurisprudencia": tenés una muestra de `criterio_sala_para_resolver` real, cada una
identificada con [decisión · N.º de juicio · año]. Tu respuesta DEBE incluir al menos una cita
TEXTUAL entre comillas (copiada literalmente, no parafraseada) de esa muestra, señalando de qué
juicio viene. Si el criterio menciona una norma, decreto o artículo específico, nombralo
explícitamente. No resumas en abstracto — citá.

Para "convergencias": basáte en las tasas por juez que te paso — si varios jueces coinciden en
un umbral similar de % favorable o en el mismo eje_decision dominante, decilo con los nombres y
números. Para "divergencias_int": lo mismo pero para los puntos donde los jueces se apartan
entre sí (dispersión de tasas, ejes distintos ante hechos similares).

Vas directo al grano — sin introducción genérica sobre qué es el tribunal.
Basáte SOLO en los datos del prompt. No inventés casos, cifras, normas ni argumentos.
Respondé en español formal. EXTENSIÓN: máximo 120 palabras por sección, en 1 o 2 párrafos. Si no te alcanza, sacá
adjetivos y relleno — nunca hechos ni citas. Corta y específica le gana a larga y genérica.

CITÁ LAS SENTENCIAS. Cuando afirmes un criterio que viene de un cluster o de un
ejemplo del prompt, poné su número de juicio entre corchetes al final de la frase,
así: [09501-2021-00559]. Usá SOLO números que aparezcan literalmente en el prompt
— no inventes ni completes ninguno. Una afirmación con cita vale más que tres sin
ella: el lector tiene que poder abrir la sentencia y verificarla.
Devolvé SOLO el JSON pedido."""

SYS_CNJ = f"""Eres un analista jurídico especializado en casación ecuatoriana en materia {MATERIA.NOMBRE.lower()}.
Generás briefs sobre {MATERIA.DESCRIPCION_CNJ_BRIEF}.
Basáte SOLO en los datos del prompt. No inventés casos ni argumentos.
Respondé en español formal.

REGLA DE ORO: cada afirmación debe ser CONCRETA Y VERIFICABLE contra los datos del prompt.
Nunca una generalidad vacía: "argumentación bien fundamentada", "normativa vigente", "pruebas
sólidas", "enfoque meticuloso", "adecuada justificación". Si no tenés un hecho concreto detrás
—un juicio, una prueba, una norma, una cifra del prompt— no escribas el punto. Preferí tres
afirmaciones específicas a ocho vagas.

NO REPITAS LA ESTADÍSTICA que ya está en el prompt: el lector la tiene arriba. Tu trabajo es
decir QUÉ hacer con ella, no volver a contarla.

EXTENSIÓN: máximo 120 palabras por sección, en 1 o 2 párrafos. Esto no es un resumen que se
pueda estirar: si no te alcanza, sacá adjetivos, muletillas y frases de relleno — nunca hechos
ni citas. Una sección corta y específica es mejor que una larga y genérica.
CITÁ LAS SENTENCIAS. Cuando afirmes un criterio que viene de un cluster o de un
ejemplo del prompt, poné su número de juicio entre corchetes al final de la frase,
así: [09501-2021-00559]. Usá SOLO números que aparezcan literalmente en el prompt
— no inventes ni completes ninguno. Una afirmación con cita vale más que tres sin
ella: el lector tiene que poder abrir la sentencia y verificarla.
Devolvé SOLO el JSON pedido."""

SYS_ABOGADO = f"""Eres un analista jurídico especializado en litigación ecuatoriana en materia {MATERIA.NOMBRE.lower()}.
Generás briefs de inteligencia táctica sobre abogados que litigan en {MATERIA.NOMBRE_PRIMERA_INSTANCIA} — ya sea
representando al actor (demandante) o a la entidad demandada (defensa). El prompt te dirá
explícitamente qué rol cumple el abogado en las causas analizadas; adaptá el análisis a ese rol
(p.ej. si es del lado de la defensa, "ganar" significa que se confirmó el acto administrativo, SL).

TRAMPA A EVITAR: los campos "clave_resultado" y "Estrategia" de cada extracción SIEMPRE están
redactados desde la óptica del ACTOR ("ganó porque...", "perdió porque...", "el actor
demostró..."), sin importar el rol del abogado de este brief. Si el abogado es de la DEFENSA,
un "ganó" en ese texto puede significar que SU cliente PERDIÓ. NUNCA uses esa redacción para
decidir si una extracción es un punto a favor o en contra — cada extracción ya trae la etiqueta
"RESULTADO PARA ESTE ABOGADO: GANÓ/PERDIÓ", que es la única fuente de verdad sobre si fue
favorable o no. Un punto en "estrategias_argumentativas" SOLO puede citar una causa marcada
GANÓ; un punto en "debilidades" SOLO puede citar una causa marcada PERDIÓ.

REGLA DE ORO: cada punto que generes debe ser una afirmación CONCRETA Y VERIFICABLE contra los
datos del prompt, nunca una generalidad vacía ("tiene un enfoque meticuloso", "utiliza normativa
vigente"). Si no tenés un hecho concreto que sustente un punto, no lo escribas.

Para "estrategias_argumentativas" y "debilidades": cada punto debe venir acompañado del
document_id de UNA causa real (de las extracciones que te paso, cada una empieza con
"[document_id]") que lo demuestre. Si un punto no tiene una causa concreta detrás, no lo
incluyas. Priorizá DIVERSIDAD: no repitas el mismo eje_decision en más de un punto si hay
variedad disponible en los datos — si cinco causas ganadas comparten el mismo eje, elegí la
más representativa y usá las otras cuatro para cubrir ejes distintos.

En "debilidades", cuando esté disponible, anclá el punto a la TESIS DE LA CONTRAPARTE (el
argumento central de la parte demandada en esa causa) en vez de solo describir el resultado — el
objetivo es que el punto sirva para anticipar cómo lo puede vencer un abogado rival: "no logra
rebatir cuando la entidad argumenta X" es mejor que "pierde por falta de pruebas".

Para "alertas": basáte ÚNICAMENTE en la tendencia numérica año a año que te paso (win rate por
año). No espécules sobre causas externas (cambios de jurisprudencia, etc.) que no estén en los
datos. Si no hay una tendencia clara, devolvé una lista vacía.

Vas directo al grano — sin introducción, sin describir quién es el abogado ni su trayectoria.
Máximo 5 puntos en "estrategias_argumentativas" y "debilidades" (2 en "alertas"). Cada punto:
UNA frase corta y concreta, sin relleno.
Basáte SOLO en los datos del prompt. No inventés causas, cifras ni argumentos.
Respondé en español formal. EXTENSIÓN: máximo 120 palabras por sección, en 1 o 2 párrafos. Si no te alcanza, sacá
adjetivos y relleno — nunca hechos ni citas. Corta y específica le gana a larga y genérica.

CITÁ LAS SENTENCIAS. Cuando afirmes un criterio que viene de un cluster o de un
ejemplo del prompt, poné su número de juicio entre corchetes al final de la frase,
así: [09501-2021-00559]. Usá SOLO números que aparezcan literalmente en el prompt
— no inventes ni completes ninguno. Una afirmación con cita vale más que tres sin
ella: el lector tiene que poder abrir la sentencia y verificarla.
Devolvé SOLO el JSON pedido."""

SYS_ENTIDAD = f"""Eres un analista jurídico especializado en derecho {MATERIA.NOMBRE.lower()} ecuatoriano.
Generás briefs de inteligencia litigiosa sobre {MATERIA.DESCRIPCION_ENTIDADES}.
Tu objetivo: revelar el patrón de comportamiento de la entidad como demandada — en qué materias pierde, cómo defiende, qué estrategias usa.
Basáte SOLO en los datos del prompt. No inventés causas ni argumentos.
Respondé en español formal.

REGLA DE ORO: cada afirmación debe ser CONCRETA Y VERIFICABLE contra los datos del prompt.
Nunca una generalidad vacía: "argumentación bien fundamentada", "normativa vigente", "pruebas
sólidas", "enfoque meticuloso", "adecuada justificación". Si no tenés un hecho concreto detrás
—un juicio, una prueba, una norma, una cifra del prompt— no escribas el punto. Preferí tres
afirmaciones específicas a ocho vagas.

NO REPITAS LA ESTADÍSTICA que ya está en el prompt: el lector la tiene arriba. Tu trabajo es
decir QUÉ hacer con ella, no volver a contarla.

EXTENSIÓN: máximo 120 palabras por sección, en 1 o 2 párrafos. Esto no es un resumen que se
pueda estirar: si no te alcanza, sacá adjetivos, muletillas y frases de relleno — nunca hechos
ni citas. Una sección corta y específica es mejor que una larga y genérica.
CITÁ LAS SENTENCIAS. Cuando afirmes un criterio que viene de un cluster o de un
ejemplo del prompt, poné su número de juicio entre corchetes al final de la frase,
así: [09501-2021-00559]. Usá SOLO números que aparezcan literalmente en el prompt
— no inventes ni completes ninguno. Una afirmación con cita vale más que tres sin
ella: el lector tiene que poder abrir la sentencia y verificarla.
Devolvé SOLO el JSON pedido."""

SYS_ACTOR = f"""Eres un analista jurídico especializado en litigación ecuatoriana en materia {MATERIA.NOMBRE.lower()}.
Generás briefs de inteligencia histórica sobre actores demandantes (empresas o personas naturales)
en el TDCT — como si fuera un reporte de cómo ha operado este actor a través de los tribunales.
Tu objetivo: revelar a qué entidades suele demandar y por qué, con qué abogados suele trabajar,
qué pruebas y argumentos usa habitualmente, qué jueces y tribunales se repiten en su historial,
y cómo le va cuando escala a la Corte Nacional de Justicia (CNJ) — no solo en instancia.
Basáte SOLO en los datos del prompt. No inventés causas ni argumentos.
Respondé en español formal.

REGLA DE ORO: cada afirmación debe ser CONCRETA Y VERIFICABLE contra los datos del prompt.
Nunca una generalidad vacía: "argumentación bien fundamentada", "normativa vigente", "pruebas
sólidas", "enfoque meticuloso", "adecuada justificación". Si no tenés un hecho concreto detrás
—un juicio, una prueba, una norma, una cifra del prompt— no escribas el punto. Preferí tres
afirmaciones específicas a ocho vagas.

NO REPITAS LA ESTADÍSTICA que ya está en el prompt: el lector la tiene arriba. Tu trabajo es
decir QUÉ hacer con ella, no volver a contarla.

EXTENSIÓN: máximo 120 palabras por sección, en 1 o 2 párrafos. Esto no es un resumen que se
pueda estirar: si no te alcanza, sacá adjetivos, muletillas y frases de relleno — nunca hechos
ni citas. Una sección corta y específica es mejor que una larga y genérica.
CITÁ LAS SENTENCIAS. Cuando afirmes un criterio que viene de un cluster o de un
ejemplo del prompt, poné su número de juicio entre corchetes al final de la frase,
así: [09501-2021-00559]. Usá SOLO números que aparezcan literalmente en el prompt
— no inventes ni completes ninguno. Una afirmación con cita vale más que tres sin
ella: el lector tiene que poder abrir la sentencia y verificarla.
Devolvé SOLO el JSON pedido."""


def coerce_text(v) -> str:
    """El JSON de OpenAI a veces ignora la instrucción de 'texto corrido' y
    devuelve una lista/objeto (p.ej. citando extracciones como array de
    dicts) en vez de un string — psycopg2 no puede adaptar eso a una
    columna text. Lo serializamos a prosa legible en vez de fallar."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                texto = item.get("razon") or item.get("punto") or item.get("texto") or ""
                ref = item.get("document_id") or item.get("juicio") or ""
                ref = str(ref).strip("[]")
                parts.append(f"{texto} ({ref})" if ref else texto)
        return " ".join(p for p in parts if p)
    if isinstance(v, dict):
        return v.get("razon") or v.get("punto") or v.get("texto") or json.dumps(v, ensure_ascii=False)
    return str(v)


# ──────────────────────────────────────────────
# DATA LOADERS
# ──────────────────────────────────────────────

def fmt_pct(v):
    return f"{round(v, 1)}%" if v is not None else "—"


def vec_from_pg(s):
    """pgvector vuelve como texto '[v1,v2,...]'. Lo parseamos a np.float32."""
    return np.fromstring(s[1:-1], sep=",", dtype=np.float32)


def cluster_criterio_juez(cur, doc_id: str) -> list[dict] | None:
    """
    Clusteriza TODA la poblacion de criterio_sala_para_resolver de un juez
    (no una muestra) usando los embeddings ya existentes en
    causa_section_embeddings (section='criterio_sala') — no genera
    embeddings nuevos. Para cada K en 3..6 mide silhouette y se queda con
    el mejor; guarda la asignacion en causa_criterio_cluster (idempotente,
    upsert) y devuelve un resumen por cluster con distribucion real de
    decision_sigla y el criterio real mas cercano al centroide como
    ejemplo (nunca texto inventado — mismo principio que el resto del
    sistema). Devuelve None si el juez no tiene poblacion suficiente
    (ver MIN_CAUSAS_CLUSTER) para que K=3..6 tenga sentido.
    """
    cur.execute("""
        SELECT c.document_id, m.juicio, m.decision_sigla, cse.embedding,
               LEFT(c.criterio_sala_para_resolver, 800) AS criterio
        FROM causa_section_embeddings cse
        JOIN causas c ON c.document_id = cse.document_id
        JOIN mv_causas_listado m ON m.document_id = c.document_id
        JOIN jueces j ON j.id = m.juez_id
        WHERE cse.section = 'criterio_sala' AND j.document_id = %s
          AND m.published_at IS NOT NULL
    """, [doc_id])
    rows = cur.fetchall()
    if len(rows) < MIN_CAUSAS_CLUSTER:
        return None

    X = np.stack([vec_from_pg(r["embedding"]) for r in rows])

    mejores = []
    for k in (3, 4, 5, 6):
        km = KMeans(n_clusters=k, random_state=42, n_init=10).fit(X)
        sil = silhouette_score(X, km.labels_)
        mejores.append((sil, k, km))
    mejores.sort(key=lambda t: -t[0])
    silhouette_ganador, K, km = mejores[0]
    labels = km.labels_

    # Guardar asignacion — idempotente, se sobreescribe si se re-corre.
    cur.execute("DELETE FROM causa_criterio_cluster WHERE juez_doc_id = %s", [doc_id])
    payload = []
    for i, r in enumerate(rows):
        centro = km.cluster_centers_[labels[i]]
        dist = float(np.linalg.norm(X[i] - centro))
        payload.append((r["document_id"], doc_id, int(labels[i]), K, dist, float(silhouette_ganador)))
    from psycopg2.extras import execute_values
    execute_values(
        cur,
        """INSERT INTO causa_criterio_cluster
               (document_id, juez_doc_id, cluster_id, k_total, distancia_centroide, silhouette_k)
           VALUES %s
           ON CONFLICT (document_id) DO UPDATE SET
               juez_doc_id = EXCLUDED.juez_doc_id, cluster_id = EXCLUDED.cluster_id,
               k_total = EXCLUDED.k_total, distancia_centroide = EXCLUDED.distancia_centroide,
               silhouette_k = EXCLUDED.silhouette_k, generated_at = now()""",
        payload,
    )

    resumen = []
    for c in range(K):
        idx = [i for i in range(len(rows)) if labels[i] == c]
        n = len(idx)
        cl = sum(1 for i in idx if rows[i]["decision_sigla"] == "CL")
        p = sum(1 for i in idx if rows[i]["decision_sigla"] == "P")
        sl = sum(1 for i in idx if rows[i]["decision_sigla"] == "SL")
        centro = km.cluster_centers_[c]
        cercano = min(idx, key=lambda i: np.linalg.norm(X[i] - centro))
        resumen.append(dict(
            cluster=c + 1, n=n, cl=cl, p=p, sl=sl,
            juicio_ejemplo=rows[cercano]["juicio"],
            criterio_ejemplo=rows[cercano]["criterio"],
        ))
    return resumen


def get_juez_data(cur, doc_id: str) -> dict | None:
    """Carga todos los datos necesarios para el brief de un juez TDCT."""
    cur.execute("""
        SELECT j.nombre,
               COUNT(m.document_id) AS total,
               COUNT(CASE WHEN m.decision_sigla IN ('CL','P') THEN 1 END)::float /
                   NULLIF(COUNT(CASE WHEN m.decision_sigla IS NOT NULL THEN 1 END),0)*100 AS tasa,
               MIN(EXTRACT(YEAR FROM m.fecha_de_emision)::int) AS anio_ini,
               MAX(EXTRACT(YEAR FROM m.fecha_de_emision)::int) AS anio_fin,
               (SELECT t.nombre FROM tribunales t
                JOIN mv_causas_listado m2 ON m2.causa_tribunal_id = t.id
                WHERE m2.juez_id = j.id GROUP BY t.nombre ORDER BY COUNT(*) DESC LIMIT 1) AS tribunal
        FROM jueces j
        JOIN mv_causas_listado m ON m.juez_id = j.id
        WHERE j.document_id = %s AND m.published_at IS NOT NULL
        GROUP BY j.id, j.nombre
    """, [doc_id])
    row = cur.fetchone()
    if not row or row["total"] < MIN_CAUSAS_JUEZ:
        return None

    cur.execute("""
        SELECT cat AS categoria_nombre, COUNT(*) AS n,
               COUNT(CASE WHEN m.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa
        FROM mv_causas_listado m
        JOIN jueces j ON j.id = m.juez_id,
        LATERAL jsonb_array_elements_text(m.categorias_nombres) AS cat
        WHERE j.document_id = %s AND m.published_at IS NOT NULL
        GROUP BY cat HAVING COUNT(*) >= 5
        ORDER BY COUNT(*) DESC LIMIT 10
    """, [doc_id])
    categorias = cur.fetchall()

    cur.execute("""
        SELECT EXTRACT(YEAR FROM m.fecha_de_emision)::int AS anio, COUNT(*) AS n,
               COUNT(CASE WHEN m.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa
        FROM mv_causas_listado m JOIN jueces j ON j.id = m.juez_id
        WHERE j.document_id = %s AND m.published_at IS NOT NULL
        GROUP BY anio ORDER BY anio
    """, [doc_id])
    evolucion = cur.fetchall()

    cur.execute("""
        SELECT ce.eje_decision, ce.tipo_prueba_clave, ce.norma_clave,
               ce.precedente_citado, ce.como_superar, ce.clave_resultado,
               m.decision_sigla
        FROM causa_extraccion ce
        JOIN mv_causas_listado m ON m.document_id = ce.document_id
        JOIN jueces j ON j.id = m.juez_id
        WHERE j.document_id = %s AND m.published_at IS NOT NULL
        ORDER BY ce.extracted_at DESC LIMIT 60
    """, [doc_id])
    extracciones = cur.fetchall()

    cur.execute("""
        SELECT LEFT(c.criterio_sala_para_resolver, 1500) AS criterio,
               m.decision_sigla, m.juicio,
               EXTRACT(YEAR FROM m.fecha_de_emision)::int AS anio_emision
        FROM causas c
        JOIN mv_causas_listado m ON m.document_id = c.document_id
        JOIN jueces j ON j.id = m.juez_id
        WHERE j.document_id = %s AND c.published_at IS NOT NULL
          AND c.criterio_sala_para_resolver IS NOT NULL
          AND LENGTH(c.criterio_sala_para_resolver) > 100
        ORDER BY m.fecha_de_emision DESC LIMIT 20
    """, [doc_id])
    criterios = cur.fetchall()

    clusters = cluster_criterio_juez(cur, doc_id)

    return dict(stats=row, categorias=categorias, evolucion=evolucion,
                extracciones=extracciones, criterios=criterios, clusters=clusters)


def build_juez_prompt(d: dict) -> str:
    s = d["stats"]
    periodo = f"{s['anio_ini']}–{s['anio_fin']}" if s["anio_ini"] != s["anio_fin"] else str(s["anio_ini"])

    cats = "\n".join(
        f"  {r['categoria_nombre']}: {r['n']} causas, {fmt_pct(r['tasa'])} favorable"
        for r in d["categorias"]
    ) or "  (sin datos suficientes por materia)"

    evol = "\n".join(
        f"  {r['anio']}: {r['n']} causas, {fmt_pct(r['tasa'])} favorable"
        for r in d["evolucion"]
    )

    # Agregar extracciones por resultado
    gana = [e for e in d["extracciones"] if e["decision_sigla"] in ("CL", "P")]
    pierde = [e for e in d["extracciones"] if e["decision_sigla"] == "SL"]

    def fmt_ext(lst, n=20):
        lines = []
        for e in lst[:n]:
            parts = [f"• Eje: {e['eje_decision'] or '—'}"]
            if e["clave_resultado"]: parts.append(f"  → {e['clave_resultado']}")
            if e["norma_clave"]:     parts.append(f"  Norma: {e['norma_clave']}")
            if e["como_superar"]:    parts.append(f"  Cómo superó: {e['como_superar']}")
            lines.append("\n".join(parts))
        return "\n\n".join(lines) or "(sin datos)"

    criterios_txt = "\n\n---\n".join(
        f"[{r['decision_sigla']} · {r['juicio']} · {r['anio_emision']}]\n{r['criterio']}"
        for r in d["criterios"]
    )

    # Si hay suficiente poblacion (ver MIN_CAUSAS_CLUSTER), reemplazamos la
    # muestra cronologica de 20 por los clusters de TODA la poblacion,
    # agrupada por similitud de razonamiento (no de hechos) — cada cluster
    # trae su distribucion real de resultado y un ejemplo real (el mas
    # cercano al centroide, nunca inventado). Con menos causas de las
    # necesarias para que K=3..6 tenga sentido, se usa la muestra de
    # siempre como fallback.
    if d.get("clusters"):
        bloques = []
        for c in d["clusters"]:
            tasa_fav = round(100 * (c["cl"] + c["p"]) / c["n"]) if c["n"] else 0
            bloques.append(
                f"Cluster {c['cluster']} ({c['n']} causas, {tasa_fav}% favorable — "
                f"CL={c['cl']} P={c['p']} SL={c['sl']}):\n"
                f"Ejemplo real más representativo [{c['juicio_ejemplo']}]:\n{c['criterio_ejemplo']}"
            )
        criterio_seccion_titulo = (
            f"## Clusters de razonamiento (agrupados por similitud semántica, "
            f"TODA la población de {s['total']} causas, no una muestra):"
        )
        criterio_seccion_cuerpo = "\n\n---\n".join(bloques)
    else:
        criterio_seccion_titulo = "## Muestra de criterio_sala (últimas sentencias):"
        criterio_seccion_cuerpo = criterios_txt

    patrones_instruccion = (
        "Describí cada patrón de razonamiento identificado arriba, con su tasa real y qué lo "
        "distingue de los demás — no repitas criterio_materias (que es por tema legal), esto es "
        "por FORMA de razonar."
        if d.get("clusters")
        else "(vacío — no hay población suficiente para agrupar por patrón de razonamiento)"
    )

    return f"""## Juez: {s['nombre']}
Tribunal principal: {s['tribunal'] or '—'} · Período: {periodo}
Causas analizadas: {s['total']} · Tasa favorable promedio: {fmt_pct(s['tasa'])}

## Por materia:
{cats}

## Evolución anual:
{evol}

## Extracciones de causas FAVORABLES (ganó el contribuyente):
{fmt_ext(gana)}

## Extracciones de causas DESFAVORABLES (perdió el contribuyente):
{fmt_ext(pierde)}

{criterio_seccion_titulo}
{criterio_seccion_cuerpo}

---
Generá este JSON (en español formal, cada sección 2-4 párrafos sustanciales):
{{
  "perfil_general": "...",
  "criterio_materias": "...",
  "patrones_criterio": "{patrones_instruccion}",
  "argumentos_exitosos": "...",
  "argumentos_fallidos": "...",
  "jurisprudencia": "...",
  "evolucion": "...",
  "alertas": "..."
}}"""


def get_tribunal_data(cur, doc_id: str) -> dict | None:
    cur.execute("""
        SELECT t.nombre, COUNT(m.document_id) AS total,
               COUNT(CASE WHEN m.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(CASE WHEN m.decision_sigla IS NOT NULL THEN 1 END),0)*100 AS tasa,
               MIN(EXTRACT(YEAR FROM m.fecha_de_emision)::int) AS anio_ini,
               MAX(EXTRACT(YEAR FROM m.fecha_de_emision)::int) AS anio_fin,
               COUNT(DISTINCT m.juez_id) AS jueces
        FROM tribunales t
        JOIN mv_causas_listado m ON m.causa_tribunal_id = t.id
        WHERE t.document_id = %s AND m.published_at IS NOT NULL
        GROUP BY t.id, t.nombre
    """, [doc_id])
    row = cur.fetchone()
    if not row or row["total"] == 0:
        return None

    cur.execute("""
        SELECT cat AS categoria_nombre, COUNT(*) AS n,
               COUNT(CASE WHEN m.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa
        FROM mv_causas_listado m
        JOIN tribunales t ON m.causa_tribunal_id = t.id,
        LATERAL jsonb_array_elements_text(m.categorias_nombres) AS cat
        WHERE t.document_id = %s AND m.published_at IS NOT NULL
        GROUP BY cat HAVING COUNT(*) >= 10
        ORDER BY COUNT(*) DESC LIMIT 10
    """, [doc_id])
    categorias = cur.fetchall()

    cur.execute("""
        SELECT EXTRACT(YEAR FROM m.fecha_de_emision)::int AS anio, COUNT(*) AS n,
               COUNT(CASE WHEN m.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa
        FROM mv_causas_listado m
        JOIN tribunales t ON m.causa_tribunal_id = t.id
        WHERE t.document_id = %s AND m.published_at IS NOT NULL
        GROUP BY anio ORDER BY anio
    """, [doc_id])
    evolucion = cur.fetchall()

    cur.execute("""
        SELECT j.nombre, COUNT(*) AS n,
               COUNT(CASE WHEN m.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa
        FROM mv_causas_listado m
        JOIN jueces j ON j.id = m.juez_id
        JOIN tribunales t ON m.causa_tribunal_id = t.id
        WHERE t.document_id = %s AND m.published_at IS NOT NULL
        GROUP BY j.id, j.nombre HAVING COUNT(*) >= 10
        ORDER BY COUNT(*) DESC LIMIT 15
    """, [doc_id])
    jueces = cur.fetchall()

    cur.execute("""
        SELECT ce.document_id, ce.eje_decision, ce.norma_clave, ce.tipo_prueba_clave,
               ce.como_superar, ce.clave_resultado, m.decision_sigla
        FROM causa_extraccion ce
        JOIN mv_causas_listado m ON m.document_id = ce.document_id
        JOIN tribunales t ON m.causa_tribunal_id = t.id
        WHERE t.document_id = %s AND m.published_at IS NOT NULL
        ORDER BY ce.extracted_at DESC LIMIT 60
    """, [doc_id])
    extracciones = cur.fetchall()

    cur.execute("""
        SELECT m.document_id,
               LEFT(c.criterio_sala_para_resolver, 1200) AS criterio,
               m.decision_sigla, m.juicio,
               EXTRACT(YEAR FROM m.fecha_de_emision)::int AS anio_emision
        FROM causas c
        JOIN mv_causas_listado m ON m.document_id = c.document_id
        JOIN tribunales t ON m.causa_tribunal_id = t.id
        WHERE t.document_id = %s AND c.published_at IS NOT NULL
          AND c.criterio_sala_para_resolver IS NOT NULL
          AND LENGTH(c.criterio_sala_para_resolver) > 100
        ORDER BY m.fecha_de_emision DESC LIMIT 15
    """, [doc_id])
    criterios = cur.fetchall()

    return dict(stats=row, categorias=categorias, evolucion=evolucion,
                jueces=jueces, extracciones=extracciones, criterios=criterios)


def build_tribunal_prompt(d: dict) -> str:
    s = d["stats"]
    periodo = f"{s['anio_ini']}–{s['anio_fin']}" if s["anio_ini"] != s["anio_fin"] else str(s["anio_ini"])

    cats = "\n".join(
        f"  {r['categoria_nombre']}: {r['n']} causas, {fmt_pct(r['tasa'])} favorable"
        for r in d["categorias"]
    ) or "  (sin datos)"

    jueces_txt = "\n".join(
        f"  {r['nombre']}: {r['n']} causas, {fmt_pct(r['tasa'])} favorable"
        for r in d["jueces"]
    ) or "  (sin datos)"

    gana = [e for e in d["extracciones"] if e["decision_sigla"] in ("CL", "P")]
    pierde = [e for e in d["extracciones"] if e["decision_sigla"] == "SL"]

    def fmt_ext(lst, n=15):
        lines = []
        for e in lst[:n]:
            parts = [f"• [{e['document_id']}] Eje: {e['eje_decision'] or '—'}"]
            if e["tipo_prueba_clave"]: parts.append(f"  Prueba: {e['tipo_prueba_clave']}")
            if e["norma_clave"]:       parts.append(f"  Norma: {e['norma_clave']}")
            r = e["clave_resultado"] or e["como_superar"]
            if r: parts.append(f"  → {r}")
            lines.append("\n".join(parts))
        return "\n\n".join(lines) or "(sin datos)"

    criterios_txt = "\n\n---\n".join(
        f"[{r['document_id']} · {r['decision_sigla']} · Juicio N.º {r['juicio']} · {r['anio_emision']}]\n{r['criterio']}"
        for r in d["criterios"]
    )

    return f"""## Tribunal: {s['nombre']}
Período: {periodo} · Causas: {s['total']} · Jueces: {s['jueces']}
Tasa favorable promedio: {fmt_pct(s['tasa'])}

## Por materia:
{cats}

## Jueces y sus tasas:
{jueces_txt}

## Patrones en causas FAVORABLES:
{fmt_ext(gana)}

## Patrones en causas DESFAVORABLES:
{fmt_ext(pierde)}

## Muestra de criterio_sala:
{criterios_txt}

---
Generá este JSON (en español formal). TODOS los valores son STRINGS de texto corrido en prosa —
NUNCA un array ni un objeto anidado, ni siquiera para "citar" extracciones individuales. No te
limites a re-listar 2-3 extracciones sueltas: identificá el patrón que se repite entre VARIAS
(p.ej. "la prueba documental aparece en la mayoría de las causas ganadas por...") y citá 2-3
ejemplos representativos DENTRO del párrafo, con su N.º de juicio o document_id entre paréntesis
(p.ej. "...(juicio N.º 17510-2020-00223)"). "perfil_general", "criterio_materias" y "evolucion"
no se muestran al usuario — dejalos como cadena vacía "". Cada sección restante: 2-4 párrafos
sustanciales:
{{
  "perfil_general": "",
  "criterio_materias": "",
  "convergencias": "texto corrido, no lista",
  "divergencias_int": "texto corrido, no lista",
  "argumentos_exitosos": "texto corrido, no lista",
  "argumentos_fallidos": "texto corrido, no lista",
  "jurisprudencia": "texto corrido con al menos una cita textual entre comillas, no lista",
  "evolucion": ""
}}"""


def get_cnj_juez_data(cur, doc_id: str) -> dict | None:
    cur.execute("""
        SELECT j.nombre,
               COUNT(m.document_id) AS total,
               COUNT(CASE WHEN m.casacion_decision_sigla IN ('C','CP') THEN 1 END) AS acepta,
               COUNT(CASE WHEN m.casacion_decision_sigla = 'NC' THEN 1 END) AS rechaza,
               MIN(EXTRACT(YEAR FROM m.casacion_fecha_emision)) AS anio_ini,
               MAX(EXTRACT(YEAR FROM m.casacion_fecha_emision)) AS anio_fin
        FROM jueces j
        JOIN mv_causas_listado m ON m.casacion_juez_id = j.id
        WHERE j.document_id = %s AND m.published_at IS NOT NULL
          AND m.casacion_decision_sigla IS NOT NULL
        GROUP BY j.id, j.nombre
    """, [doc_id])
    row = cur.fetchone()
    if not row or row["total"] == 0:
        return None

    cur.execute("""
        SELECT cat AS categoria_nombre, COUNT(*) AS n,
               COUNT(CASE WHEN m.casacion_decision_sigla IN ('C','CP') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa_acepta
        FROM mv_causas_listado m
        JOIN jueces j ON j.id = m.casacion_juez_id,
        LATERAL jsonb_array_elements_text(m.categorias_nombres) AS cat
        WHERE j.document_id = %s AND m.published_at IS NOT NULL
          AND m.casacion_decision_sigla IS NOT NULL
        GROUP BY cat HAVING COUNT(*) >= 3
        ORDER BY COUNT(*) DESC LIMIT 10
    """, [doc_id])
    categorias = cur.fetchall()

    # Causales más frecuentes
    cur.execute("""
        SELECT ci_nombre AS causal, COUNT(*) AS n,
               COUNT(CASE WHEN m.casacion_decision_sigla IN ('C','CP') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa_acepta
        FROM mv_causas_listado m
        JOIN jueces j ON j.id = m.casacion_juez_id,
        LATERAL jsonb_array_elements_text(COALESCE(m.causal_invocadas_nombres, '[]'::jsonb)) AS ci_nombre
        WHERE j.document_id = %s AND m.published_at IS NOT NULL
          AND m.casacion_decision_sigla IS NOT NULL
        GROUP BY ci_nombre HAVING COUNT(*) >= 3
        ORDER BY COUNT(*) DESC LIMIT 10
    """, [doc_id])
    causales = cur.fetchall()

    cur.execute("""
        SELECT LEFT(cas.criterio_sala_para_resolver, 1500) AS criterio,
               m.casacion_decision_sigla AS sigla, m.juicio,
               EXTRACT(YEAR FROM m.casacion_fecha_emision)::int AS anio
        FROM casaciones cas
        JOIN mv_causas_listado m ON m.casacion_id = cas.id
        JOIN jueces j ON j.id = m.casacion_juez_id
        WHERE j.document_id = %s AND cas.published_at IS NOT NULL
          AND cas.criterio_sala_para_resolver IS NOT NULL
          AND LENGTH(cas.criterio_sala_para_resolver) > 100
        ORDER BY m.casacion_fecha_emision DESC LIMIT 20
    """, [doc_id])
    criterios = cur.fetchall()

    return dict(stats=row, categorias=categorias, causales=causales, criterios=criterios)


def build_cnj_prompt(d: dict) -> str:
    s = d["stats"]
    total = s["total"]
    periodo = f"{int(s['anio_ini'])}–{int(s['anio_fin'])}" if s["anio_ini"] != s["anio_fin"] else str(int(s["anio_ini"]))
    tasa = round(s["acepta"] / total * 100, 1) if total > 0 else 0

    cats = "\n".join(
        f"  {r['categoria_nombre']}: {r['n']} casaciones, {fmt_pct(r['tasa_acepta'])} aceptadas"
        for r in d["categorias"]
    ) or "  (sin datos)"

    caus = "\n".join(
        f"  {r['causal']}: {r['n']} veces, {fmt_pct(r['tasa_acepta'])} prosperan"
        for r in d["causales"]
    ) or "  (sin datos)"

    criterios_txt = "\n\n---\n".join(
        f"[{r['sigla']} · {r['juicio']} · {r['anio']}]\n{r['criterio']}"
        for r in d["criterios"]
    )

    return f"""## Juez CNJ: {s['nombre']}
Período: {periodo} · Casaciones: {total}
Aceptadas (C+CP): {s['acepta']} ({tasa}%) · Rechazadas (NC): {s['rechaza']}

## Por materia:
{cats}

## Causales más invocadas:
{caus}

## Muestra de criterio de casación:
{criterios_txt}

---
Generá este JSON (en español formal, cada sección 2-4 párrafos sustanciales):
{{
  "perfil_general": "...",
  "criterio_causales": "...",
  "posicion_materias": "...",
  "votos_salvados": "...",
  "evolucion": "...",
  "alertas": "..."
}}"""


# ──────────────────────────────────────────────
# ABOGADO
# ──────────────────────────────────────────────

MIN_CAUSAS_ABOGADO = int(os.environ.get("BRIEF_MIN_CAUSAS_ABO", "5"))
MIN_CAUSAS_ENTIDAD = int(os.environ.get("BRIEF_MIN_CAUSAS_ENT", "10"))
MIN_CAUSAS_ACTOR = int(os.environ.get("BRIEF_MIN_CAUSAS_ACTOR", "3"))


def abogado_link_table(lado: str) -> str:
    return "causas_abogado_actor_lnk" if lado == "actor" else "causas_abogado_demandado_lnk"


def get_abogado_data(cur, doc_id: str, lado: str = "actor") -> dict | None:
    """Carga datos para el brief de un abogado — lado actor o lado demandado.

    `decision_sigla` siempre describe el resultado desde la óptica del actor
    (CL = ganó el actor). Para lado='demandado' lo que cuenta como "ganado"
    para el sujeto del brief es SL — por eso `tasa_fav`/gana/pierde se arman
    con esa inversión en vez de leer CL directo.
    """
    tbl = abogado_link_table(lado)
    fav_sigla = "SL" if lado == "demandado" else "CL"
    cur.execute(f"""
        SELECT ab.nombre,
               COUNT(mv.id) AS total,
               COUNT(CASE WHEN mv.decision_sigla = %s THEN 1 END)::float/
                   NULLIF(COUNT(CASE WHEN mv.decision_sigla IS NOT NULL THEN 1 END),0)*100 AS tasa_fav,
               MIN(EXTRACT(YEAR FROM mv.fecha_de_emision)::int) AS anio_ini,
               MAX(EXTRACT(YEAR FROM mv.fecha_de_emision)::int) AS anio_fin
        FROM {tbl} lnk
        JOIN abogados ab ON ab.id = lnk.abogado_id
        JOIN mv_causas_listado mv ON mv.id = lnk.causa_id
        WHERE ab.document_id = %s
          AND mv.decision_sigla IS NOT NULL
          AND mv.published_at IS NOT NULL
        GROUP BY ab.nombre
    """, [fav_sigla, doc_id])
    row = cur.fetchone()
    if not row or row["total"] < MIN_CAUSAS_ABOGADO:
        return None

    cur.execute(f"""
        SELECT cat AS categoria, COUNT(*) AS n,
               COUNT(CASE WHEN mv.decision_sigla = %s THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa_fav
        FROM {tbl} lnk
        JOIN abogados ab ON ab.id = lnk.abogado_id
        JOIN mv_causas_listado mv ON mv.id = lnk.causa_id,
        LATERAL jsonb_array_elements_text(COALESCE(mv.categorias_nombres,'[]'::jsonb)) AS cat
        WHERE ab.document_id = %s
          AND mv.decision_sigla IS NOT NULL
          AND mv.published_at IS NOT NULL
        GROUP BY cat HAVING COUNT(*) >= 2
        ORDER BY COUNT(*) DESC LIMIT 10
    """, [fav_sigla, doc_id])
    categorias = cur.fetchall()

    cur.execute(f"""
        SELECT EXTRACT(YEAR FROM mv.fecha_de_emision)::int AS anio, COUNT(*) AS n,
               COUNT(CASE WHEN mv.decision_sigla = %s THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa_fav
        FROM {tbl} lnk
        JOIN abogados ab ON ab.id = lnk.abogado_id
        JOIN mv_causas_listado mv ON mv.id = lnk.causa_id
        WHERE ab.document_id = %s
          AND mv.decision_sigla IS NOT NULL
          AND mv.published_at IS NOT NULL
          AND mv.fecha_de_emision IS NOT NULL
        GROUP BY anio ORDER BY anio
    """, [fav_sigla, doc_id])
    evolucion = cur.fetchall()

    cur.execute(f"""
        SELECT ce.document_id, ce.eje_decision, ce.tipo_prueba_clave, ce.norma_clave,
               ce.como_superar, ce.clave_resultado, ce.tesis_contraparte, mv.decision_sigla
        FROM causa_extraccion ce
        JOIN mv_causas_listado mv ON mv.document_id = ce.document_id
        JOIN {tbl} lnk ON lnk.causa_id = mv.id
        JOIN abogados ab ON ab.id = lnk.abogado_id
        WHERE ab.document_id = %s AND mv.published_at IS NOT NULL
        ORDER BY ce.extracted_at DESC LIMIT 50
    """, [doc_id])
    extracciones = cur.fetchall()

    cur.execute(f"""
        SELECT LEFT(c.argumentos_del_actor, 1200) AS argumentos,
               LEFT(c.criterio_sala_para_resolver, 800) AS criterio,
               mv.decision_sigla, mv.juicio,
               EXTRACT(YEAR FROM mv.fecha_de_emision)::int AS anio
        FROM causas c
        JOIN mv_causas_listado mv ON mv.document_id = c.document_id
        JOIN {tbl} lnk ON lnk.causa_id = mv.id
        JOIN abogados ab ON ab.id = lnk.abogado_id
        WHERE ab.document_id = %s AND c.published_at IS NOT NULL
          AND c.argumentos_del_actor IS NOT NULL
          AND LENGTH(c.argumentos_del_actor) > 100
        ORDER BY mv.fecha_de_emision DESC LIMIT 15
    """, [doc_id])
    argumentos = cur.fetchall()

    return dict(stats=row, categorias=categorias, evolucion=evolucion,
                extracciones=extracciones, argumentos=argumentos, lado=lado,
                fav_sigla=fav_sigla)


def build_abogado_prompt(d: dict) -> str:
    s = d["stats"]
    lado = d["lado"]
    fav = d["fav_sigla"]
    rol_txt = (
        "representando a la ENTIDAD DEMANDADA (defensa) — ganar para él/ella es que se confirme "
        "el acto administrativo (SL); perder es que se lo revoque (CL)"
        if lado == "demandado" else
        "representando al ACTOR (demandante) — ganar para él/ella es CL; perder es SL"
    )
    periodo = (f"{s['anio_ini']}–{s['anio_fin']}"
               if s["anio_ini"] != s["anio_fin"] else str(s["anio_ini"]))

    cats = "\n".join(
        f"  {r['categoria']}: {r['n']} causas, {fmt_pct(r['tasa_fav'])} favorables"
        for r in d["categorias"]
    ) or "  (sin datos suficientes)"

    evol = "\n".join(
        f"  {r['anio']}: {r['n']} causas, {fmt_pct(r['tasa_fav'])} favorables"
        for r in d["evolucion"]
    )

    gana = [e for e in d["extracciones"] if e["decision_sigla"] == fav]
    pierde = [e for e in d["extracciones"] if e["decision_sigla"] and e["decision_sigla"] != fav]

    def fmt_ext(lst, resultado_label, n=15):
        # `clave_resultado`/`como_superar` siempre están redactados desde la
        # óptica del ACTOR ("ganó porque...", "perdió porque..."), sin
        # importar el rol del abogado de este brief. Para el lado defensa
        # eso es ENGAÑOSO (un "ganó" ahí puede ser una derrota para ella) —
        # por eso cada extracción trae su propia etiqueta explícita de
        # resultado, para que el modelo no tenga que reinterpretar el texto.
        lines = []
        for e in lst[:n]:
            parts = [
                f"• [{e['document_id']}] RESULTADO PARA ESTE ABOGADO: {resultado_label}",
                f"  Eje: {e['eje_decision'] or '—'}"
            ]
            if e["tipo_prueba_clave"]:   parts.append(f"  Prueba: {e['tipo_prueba_clave']}")
            if e["clave_resultado"]:     parts.append(f"  → {e['clave_resultado']}")
            if e["norma_clave"]:         parts.append(f"  Norma: {e['norma_clave']}")
            if e["tesis_contraparte"]:   parts.append(f"  Tesis contraparte: {e['tesis_contraparte']}")
            if e["como_superar"]:        parts.append(f"  Estrategia: {e['como_superar']}")
            lines.append("\n".join(parts))
        return "\n\n".join(lines) or "(sin datos)"

    args_txt = "\n\n---\n".join(
        f"[{r['decision_sigla']} · {r['juicio']} · {r['anio']}]\n"
        f"ARGUMENTOS ACTOR: {r['argumentos'] or '—'}\nCRITERIO SALA: {r['criterio'] or '—'}"
        for r in d["argumentos"][:10]
    )

    return f"""## Abogado: {s['nombre']}
Rol en estas causas: {rol_txt}
Período: {periodo} · Causas totales: {s['total']} · Win rate: {fmt_pct(s['tasa_fav'])}

## Por materia:
{cats}

## Evolución anual:
{evol}

## Extracciones de causas GANADAS (para este abogado):
{fmt_ext(gana, "GANÓ")}

## Extracciones de causas PERDIDAS (para este abogado):
{fmt_ext(pierde, "PERDIÓ")}

## Muestra de argumentos del actor vs criterio sala (nota: "argumentos del actor" es
## literal — son los del demandante en el expediente, sin importar de qué lado litiga
## el abogado de este brief):
{args_txt}

---
Generá este JSON (español formal, directo al grano, sin introducción):
{{
  "estrategias_argumentativas": [
    {{"punto": "frase corta y concreta", "document_id": "id de la causa que lo demuestra"}}
  ],
  "debilidades": [
    {{"punto": "frase corta y concreta — dónde falla, con qué frecuencia", "document_id": "id de la causa que lo demuestra"}}
  ],
  "alertas": ["frase corta anclada al dato de tendencia, o lista vacía si no hay tendencia clara"]
}}"""


def upsert_abogado_brief(cur, doc_id: str, lado: str, nombre: str, causas: int, periodo: str, b: dict):
    cur.execute("""
        INSERT INTO abogado_brief
            (document_id, lado, nombre, estrategias_argumentativas, debilidades, alertas,
             causas_analizadas, periodo, model, generated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (document_id, lado) DO UPDATE SET
            nombre=EXCLUDED.nombre,
            estrategias_argumentativas=EXCLUDED.estrategias_argumentativas,
            debilidades=EXCLUDED.debilidades,
            alertas=EXCLUDED.alertas,
            causas_analizadas=EXCLUDED.causas_analizadas,
            periodo=EXCLUDED.periodo, model=EXCLUDED.model, generated_at=now()
    """, [doc_id, lado, nombre,
          Json(b.get("estrategias_argumentativas", [])),
          Json(b.get("debilidades", [])),
          Json(b.get("alertas", [])),
          causas, periodo, MODEL])


def get_entidad_data(cur, doc_id: str) -> dict | None:
    """Carga datos para el brief de una entidad demandada."""
    cur.execute("""
        SELECT e.nombre,
               COUNT(mv.id) AS total,
               COUNT(CASE WHEN mv.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(CASE WHEN mv.decision_sigla IS NOT NULL THEN 1 END),0)*100 AS tasa_cl,
               MIN(EXTRACT(YEAR FROM mv.fecha_de_emision)::int) AS anio_ini,
               MAX(EXTRACT(YEAR FROM mv.fecha_de_emision)::int) AS anio_fin
        FROM entidades e
        JOIN mv_causas_listado mv ON mv.entidad_demandada_id = e.id
        WHERE e.document_id = %s
          AND mv.decision_sigla IS NOT NULL
          AND mv.published_at IS NOT NULL
        GROUP BY e.nombre
    """, [doc_id])
    row = cur.fetchone()
    if not row or row["total"] < MIN_CAUSAS_ENTIDAD:
        return None

    cur.execute("""
        SELECT cat AS categoria, COUNT(*) AS n,
               COUNT(CASE WHEN mv.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa_cl
        FROM mv_causas_listado mv
        JOIN entidades e ON e.id = mv.entidad_demandada_id,
        LATERAL jsonb_array_elements_text(COALESCE(mv.categorias_nombres,'[]'::jsonb)) AS cat
        WHERE e.document_id = %s
          AND mv.decision_sigla IS NOT NULL
          AND mv.published_at IS NOT NULL
        GROUP BY cat HAVING COUNT(*) >= 2
        ORDER BY COUNT(*) DESC LIMIT 10
    """, [doc_id])
    categorias = cur.fetchall()

    cur.execute("""
        SELECT EXTRACT(YEAR FROM mv.fecha_de_emision)::int AS anio, COUNT(*) AS n,
               COUNT(CASE WHEN mv.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa_cl
        FROM mv_causas_listado mv
        JOIN entidades e ON e.id = mv.entidad_demandada_id
        WHERE e.document_id = %s
          AND mv.decision_sigla IS NOT NULL
          AND mv.published_at IS NOT NULL
          AND mv.fecha_de_emision IS NOT NULL
        GROUP BY anio ORDER BY anio
    """, [doc_id])
    evolucion = cur.fetchall()

    cur.execute("""
        SELECT ce.eje_decision, ce.tipo_prueba_clave, ce.norma_clave,
               ce.como_superar, ce.clave_resultado, mv.decision_sigla
        FROM causa_extraccion ce
        JOIN mv_causas_listado mv ON mv.document_id = ce.document_id
        JOIN entidades e ON e.id = mv.entidad_demandada_id
        WHERE e.document_id = %s AND mv.published_at IS NOT NULL
        ORDER BY ce.extracted_at DESC LIMIT 40
    """, [doc_id])
    extracciones = cur.fetchall()

    return dict(stats=row, categorias=categorias, evolucion=evolucion,
                extracciones=extracciones)


def build_entidad_prompt(d: dict) -> str:
    s = d["stats"]
    periodo = (f"{s['anio_ini']}–{s['anio_fin']}"
               if s["anio_ini"] != s["anio_fin"] else str(s["anio_ini"]))

    cats = "\n".join(
        f"  {r['categoria']}: {r['n']} causas, {fmt_pct(r['tasa_cl'])} CL"
        for r in d["categorias"]
    ) or "  (sin datos suficientes)"

    evol = "\n".join(
        f"  {r['anio']}: {r['n']} causas, {fmt_pct(r['tasa_cl'])} CL"
        for r in d["evolucion"]
    )

    gana = [e for e in d["extracciones"] if e["decision_sigla"] in ("CL", "P")]
    pierde = [e for e in d["extracciones"] if e["decision_sigla"] == "SL"]

    def fmt_ext(lst, n=15):
        lines = []
        for e in lst[:n]:
            parts = [f"• Eje: {e['eje_decision'] or '—'}"]
            if e["tipo_prueba_clave"]: parts.append(f"  Prueba: {e['tipo_prueba_clave']}")
            if e["clave_resultado"]:   parts.append(f"  → {e['clave_resultado']}")
            if e["norma_clave"]:       parts.append(f"  Norma: {e['norma_clave']}")
            if e["como_superar"]:      parts.append(f"  Estrategia: {e['como_superar']}")
            lines.append("\n".join(parts))
        return "\n\n".join(lines) or "(sin datos)"

    return f"""## Entidad demandada: {s['nombre']}
Período: {periodo} · Causas totales: {s['total']} · {MATERIA.FRASE_PARTE_FAVORECIDA}: {fmt_pct(s['tasa_cl'])}

## Por materia:
{cats}

## Evolución anual:
{evol}

## Extracciones de causas PERDIDAS por la entidad (CL):
{fmt_ext(gana)}

## Extracciones de causas GANADAS por la entidad (SL):
{fmt_ext(pierde)}

---
Generá este JSON (en español formal, cada sección 2-4 párrafos sustanciales):
{{
  "perfil_general": "...",
  "criterio_materias": "...",
  "fortalezas": "...",
  "debilidades": "...",
  "estrategia_procesal": "...",
  "evolucion": "...",
  "alertas": "..."
}}"""


def upsert_entidad_brief(cur, doc_id: str, nombre: str, causas: int, periodo: str, b: dict):
    cur.execute("""
        INSERT INTO entidad_brief
            (document_id, nombre, perfil_general, criterio_materias,
             fortalezas, debilidades, estrategia_procesal, evolucion, alertas,
             causas_analizadas, periodo, model, generated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (document_id) DO UPDATE SET
            nombre=EXCLUDED.nombre,
            perfil_general=EXCLUDED.perfil_general,
            criterio_materias=EXCLUDED.criterio_materias,
            fortalezas=EXCLUDED.fortalezas,
            debilidades=EXCLUDED.debilidades,
            estrategia_procesal=EXCLUDED.estrategia_procesal,
            evolucion=EXCLUDED.evolucion,
            alertas=EXCLUDED.alertas,
            causas_analizadas=EXCLUDED.causas_analizadas,
            periodo=EXCLUDED.periodo, model=EXCLUDED.model, generated_at=now()
    """, [doc_id, nombre,
          b.get("perfil_general", ""), b.get("criterio_materias", ""),
          b.get("fortalezas", ""), b.get("debilidades", ""),
          b.get("estrategia_procesal"), b.get("evolucion"),
          b.get("alertas"), causas, periodo, MODEL])


def get_actor_data(cur, nombre: str) -> dict | None:
    """Carga datos para el brief de un actor demandante (empresa o persona natural)."""
    cur.execute("""
        SELECT c.actor_normalizado AS nombre, MAX(c.tipo_actor) AS tipo,
               COUNT(mv.id) AS total,
               COUNT(CASE WHEN mv.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(CASE WHEN mv.decision_sigla IS NOT NULL THEN 1 END),0)*100 AS tasa_cl,
               MIN(EXTRACT(YEAR FROM mv.fecha_de_emision)::int) AS anio_ini,
               MAX(EXTRACT(YEAR FROM mv.fecha_de_emision)::int) AS anio_fin
        FROM causas c
        JOIN mv_causas_listado mv ON mv.document_id = c.document_id
        WHERE c.actor_normalizado = %s
          AND mv.decision_sigla IS NOT NULL
          AND mv.published_at IS NOT NULL
        GROUP BY c.actor_normalizado
    """, [nombre])
    row = cur.fetchone()
    if not row or row["total"] < MIN_CAUSAS_ACTOR:
        return None

    cur.execute("""
        SELECT mv.entidad_nombre AS entidad, COUNT(*) AS n,
               COUNT(CASE WHEN mv.decision_sigla IN ('CL','P') THEN 1 END)::float/
                   NULLIF(COUNT(*),0)*100 AS tasa_cl
        FROM causas c
        JOIN mv_causas_listado mv ON mv.document_id = c.document_id
        WHERE c.actor_normalizado = %s
          AND mv.decision_sigla IS NOT NULL AND mv.published_at IS NOT NULL
          AND mv.entidad_nombre IS NOT NULL
        GROUP BY mv.entidad_nombre
        ORDER BY COUNT(*) DESC LIMIT 8
    """, [nombre])
    entidades = cur.fetchall()

    cur.execute("""
        SELECT ab.nombre AS abogado, COUNT(*) AS n
        FROM causas c
        JOIN mv_causas_listado mv ON mv.document_id = c.document_id
        JOIN causas_abogado_actor_lnk lnk ON lnk.causa_id = mv.id
        JOIN abogados ab ON ab.id = lnk.abogado_id
        WHERE c.actor_normalizado = %s AND mv.published_at IS NOT NULL
        GROUP BY ab.nombre
        ORDER BY COUNT(*) DESC LIMIT 6
    """, [nombre])
    abogados = cur.fetchall()

    cur.execute("""
        SELECT mv.tribunal_nombre AS tribunal, mv.juez_ponente_nombre AS juez, COUNT(*) AS n
        FROM causas c
        JOIN mv_causas_listado mv ON mv.document_id = c.document_id
        WHERE c.actor_normalizado = %s AND mv.published_at IS NOT NULL
          AND mv.tribunal_nombre IS NOT NULL
        GROUP BY mv.tribunal_nombre, mv.juez_ponente_nombre
        ORDER BY COUNT(*) DESC LIMIT 8
    """, [nombre])
    judicial = cur.fetchall()

    cur.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN mv.casacion_decision_sigla IN ('C','CP') THEN 1 ELSE 0 END) AS aceptadas
        FROM causas c
        JOIN mv_causas_listado mv ON mv.document_id = c.document_id
        WHERE c.actor_normalizado = %s AND mv.casacion_id IS NOT NULL
          AND mv.casacion_decision_sigla IS NOT NULL
    """, [nombre])
    cnj = cur.fetchone()

    cur.execute("""
        SELECT ce.eje_decision, ce.tipo_prueba_clave, ce.norma_clave,
               ce.como_superar, ce.clave_resultado, mv.decision_sigla
        FROM causa_extraccion ce
        JOIN mv_causas_listado mv ON mv.document_id = ce.document_id
        JOIN causas c ON c.document_id = mv.document_id
        WHERE c.actor_normalizado = %s AND mv.published_at IS NOT NULL
        ORDER BY ce.extracted_at DESC LIMIT 40
    """, [nombre])
    extracciones = cur.fetchall()

    return dict(stats=row, entidades=entidades, abogados=abogados,
                judicial=judicial, cnj=cnj, extracciones=extracciones)


def build_actor_prompt(d: dict) -> str:
    s = d["stats"]
    periodo = (f"{s['anio_ini']}–{s['anio_fin']}"
               if s["anio_ini"] != s["anio_fin"] else str(s["anio_ini"]))
    tipo_txt = "empresa" if s["tipo"] == "empresa" else "persona natural"

    ents = "\n".join(
        f"  {r['entidad']}: {r['n']} causas, {fmt_pct(r['tasa_cl'])} CL"
        for r in d["entidades"]
    ) or "  (sin datos suficientes)"

    abos = "\n".join(f"  {r['abogado']}: {r['n']} causas" for r in d["abogados"]) or "  (sin datos)"

    jud = "\n".join(
        f"  {r['tribunal']} — juez {r['juez'] or '—'}: {r['n']} causas"
        for r in d["judicial"]
    ) or "  (sin datos)"

    cnj = d["cnj"]
    cnj_txt = (
        f"{cnj['total']} casaciones, {fmt_pct((cnj['aceptadas'] or 0) / cnj['total'] * 100 if cnj['total'] else None)} aceptadas (C+CP)"
        if cnj and cnj["total"] else "no ha escalado casos a la CNJ en el repositorio"
    )

    gana = [e for e in d["extracciones"] if e["decision_sigla"] in ("CL", "P")]
    pierde = [e for e in d["extracciones"] if e["decision_sigla"] == "SL"]

    def fmt_ext(lst, n=15):
        lines = []
        for e in lst[:n]:
            parts = [f"• Eje: {e['eje_decision'] or '—'}"]
            if e["tipo_prueba_clave"]: parts.append(f"  Prueba: {e['tipo_prueba_clave']}")
            if e["clave_resultado"]:   parts.append(f"  → {e['clave_resultado']}")
            if e["norma_clave"]:       parts.append(f"  Norma: {e['norma_clave']}")
            if e["como_superar"]:      parts.append(f"  Estrategia: {e['como_superar']}")
            lines.append("\n".join(parts))
        return "\n\n".join(lines) or "(sin datos)"

    return f"""## Actor demandante: {s['nombre']} ({tipo_txt})
Período: {periodo} · Causas totales: {s['total']} · Tasa favorable (CL): {fmt_pct(s['tasa_cl'])}

## Entidades que suele demandar:
{ents}

## Abogados con los que ha trabajado:
{abos}

## Tribunales y jueces que se repiten:
{jud}

## Comportamiento en la Corte Nacional (CNJ):
{cnj_txt}

## Extracciones de causas GANADAS (CL/P):
{fmt_ext(gana)}

## Extracciones de causas PERDIDAS (SL):
{fmt_ext(pierde)}

---
Generá este JSON (en español formal, cada sección 2-4 párrafos sustanciales, como un reporte histórico
de cómo ha operado este actor a través de los tribunales):
{{
  "perfil_general": "...",
  "entidades_demandadas": "...",
  "abogados_colaboradores": "...",
  "pruebas_argumentos": "...",
  "patron_judicial": "...",
  "comportamiento_cnj": "...",
  "alertas": "..."
}}"""


def upsert_actor_brief(cur, nombre: str, tipo: str, causas: int, periodo: str, b: dict):
    cur.execute("""
        INSERT INTO actor_brief
            (nombre_normalizado, nombre, tipo, perfil_general, entidades_demandadas,
             abogados_colaboradores, pruebas_argumentos, patron_judicial,
             comportamiento_cnj, alertas, causas_analizadas, periodo, model, generated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (nombre_normalizado) DO UPDATE SET
            nombre=EXCLUDED.nombre,
            tipo=EXCLUDED.tipo,
            perfil_general=EXCLUDED.perfil_general,
            entidades_demandadas=EXCLUDED.entidades_demandadas,
            abogados_colaboradores=EXCLUDED.abogados_colaboradores,
            pruebas_argumentos=EXCLUDED.pruebas_argumentos,
            patron_judicial=EXCLUDED.patron_judicial,
            comportamiento_cnj=EXCLUDED.comportamiento_cnj,
            alertas=EXCLUDED.alertas,
            causas_analizadas=EXCLUDED.causas_analizadas,
            periodo=EXCLUDED.periodo, model=EXCLUDED.model, generated_at=now()
    """, [nombre, nombre, tipo,
          b.get("perfil_general", ""), b.get("entidades_demandadas", ""),
          b.get("abogados_colaboradores", ""), b.get("pruebas_argumentos", ""),
          b.get("patron_judicial", ""), b.get("comportamiento_cnj"),
          b.get("alertas"), causas, periodo, MODEL])


# ──────────────────────────────────────────────
# UPSERT
# ──────────────────────────────────────────────

def upsert_juez_brief(cur, doc_id: str, nombre: str, causas: int, periodo: str, b: dict):
    cur.execute("""
        INSERT INTO juez_brief
            (document_id, nombre, perfil_general, criterio_materias, patrones_criterio,
             argumentos_exitosos, argumentos_fallidos, jurisprudencia,
             evolucion, alertas, causas_analizadas, periodo, model, generated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (document_id) DO UPDATE SET
            nombre=EXCLUDED.nombre, perfil_general=EXCLUDED.perfil_general,
            criterio_materias=EXCLUDED.criterio_materias,
            patrones_criterio=EXCLUDED.patrones_criterio,
            argumentos_exitosos=EXCLUDED.argumentos_exitosos,
            argumentos_fallidos=EXCLUDED.argumentos_fallidos,
            jurisprudencia=EXCLUDED.jurisprudencia,
            evolucion=EXCLUDED.evolucion, alertas=EXCLUDED.alertas,
            causas_analizadas=EXCLUDED.causas_analizadas,
            periodo=EXCLUDED.periodo, model=EXCLUDED.model, generated_at=now()
    """, [doc_id, nombre,
          b.get("perfil_general",""), b.get("criterio_materias",""), b.get("patrones_criterio",""),
          b.get("argumentos_exitosos",""), b.get("argumentos_fallidos",""),
          b.get("jurisprudencia"), b.get("evolucion"), b.get("alertas"),
          causas, periodo, MODEL])


def upsert_tribunal_brief(cur, doc_id: str, nombre: str, causas: int, periodo: str, b: dict):
    cur.execute("""
        INSERT INTO tribunal_brief
            (document_id, nombre, perfil_general, criterio_materias,
             convergencias, divergencias_int, argumentos_exitosos, argumentos_fallidos,
             jurisprudencia, evolucion, causas_analizadas, periodo, model, generated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (document_id) DO UPDATE SET
            nombre=EXCLUDED.nombre, perfil_general=EXCLUDED.perfil_general,
            criterio_materias=EXCLUDED.criterio_materias,
            convergencias=EXCLUDED.convergencias,
            divergencias_int=EXCLUDED.divergencias_int,
            argumentos_exitosos=EXCLUDED.argumentos_exitosos,
            argumentos_fallidos=EXCLUDED.argumentos_fallidos,
            jurisprudencia=EXCLUDED.jurisprudencia, evolucion=EXCLUDED.evolucion,
            causas_analizadas=EXCLUDED.causas_analizadas,
            periodo=EXCLUDED.periodo, model=EXCLUDED.model, generated_at=now()
    """, [doc_id, nombre,
          coerce_text(b.get("perfil_general","")), coerce_text(b.get("criterio_materias","")),
          coerce_text(b.get("convergencias")), coerce_text(b.get("divergencias_int")),
          coerce_text(b.get("argumentos_exitosos","")), coerce_text(b.get("argumentos_fallidos","")),
          coerce_text(b.get("jurisprudencia")), coerce_text(b.get("evolucion")),
          causas, periodo, MODEL])


def upsert_cnj_brief(cur, doc_id: str, nombre: str, causas: int, periodo: str, b: dict):
    cur.execute("""
        INSERT INTO juez_cnj_brief
            (document_id, nombre, perfil_general, criterio_causales,
             posicion_materias, votos_salvados, evolucion, alertas,
             causas_analizadas, periodo, model, generated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
        ON CONFLICT (document_id) DO UPDATE SET
            nombre=EXCLUDED.nombre, perfil_general=EXCLUDED.perfil_general,
            criterio_causales=EXCLUDED.criterio_causales,
            posicion_materias=EXCLUDED.posicion_materias,
            votos_salvados=EXCLUDED.votos_salvados,
            evolucion=EXCLUDED.evolucion, alertas=EXCLUDED.alertas,
            causas_analizadas=EXCLUDED.causas_analizadas,
            periodo=EXCLUDED.periodo, model=EXCLUDED.model, generated_at=now()
    """, [doc_id, nombre,
          b.get("perfil_general",""), b.get("criterio_causales",""),
          b.get("posicion_materias",""), b.get("votos_salvados"),
          b.get("evolucion"), b.get("alertas"),
          causas, periodo, MODEL])


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
