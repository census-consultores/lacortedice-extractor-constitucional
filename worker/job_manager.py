"""
Gestión de jobs de extracción: estado en Postgres, pool de concurrencia,
señales de pausa/kill y cola automática.
"""

from config import materia as MATERIA
import os
import queue
import threading
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from typing import Optional

DATABASE_URL     = os.environ.get("DATABASE_URL", "")
MAX_CONCURRENT   = int(os.environ.get("MAX_CONCURRENT_JOBS", "4"))

# Límites por categoría de job. Cada categoría compite en su propio pool.
# extraccion: extracciones manuales largas
# cron:       runs automáticos (scheduler)
# backfill:   workers auxiliares (duración, actores)
_MAX_CAT = {
    "extraccion": int(os.environ.get("MAX_CONCURRENT_EXTRACCION", str(MAX_CONCURRENT))),
    "cron":       int(os.environ.get("MAX_CONCURRENT_CRON",       "1")),
    "backfill":   int(os.environ.get("MAX_CONCURRENT_BACKFILL",   "1")),
    # índice rápido de SATJE (worker/indice_worker.py): un job, con hilos propios
    "indice":     int(os.environ.get("MAX_CONCURRENT_INDICE",     "1")),
    # mapeo de volumen (worker/mapeo_worker.py): un job, con hilos propios
    "mapeo":      int(os.environ.get("MAX_CONCURRENT_MAPEO",      "1")),
}

def _max_cat(categoria: str) -> int:
    return _MAX_CAT.get(categoria, MAX_CONCURRENT)

# ── Inicialización de la tabla worker_jobs ────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS worker_jobs (
    id               SERIAL PRIMARY KEY,
    categoria        VARCHAR(50)  NOT NULL DEFAULT 'extraccion',
    tribunal         VARCHAR(10)  NOT NULL,
    año              VARCHAR(4)   NOT NULL,
    desde            INTEGER      NOT NULL DEFAULT 1,
    hasta            INTEGER,
    dry_run          BOOLEAN      NOT NULL DEFAULT false,
    vista_previa     BOOLEAN      NOT NULL DEFAULT false,
    flujo_proveedores VARCHAR(20) NOT NULL DEFAULT 'alibaba',
    modelo           VARCHAR(100) NOT NULL DEFAULT 'gpt-4o',
    workers          TEXT NOT NULL DEFAULT '',
    estado           VARCHAR(20)  NOT NULL DEFAULT 'pendiente',
    checkpoint       INTEGER      NOT NULL DEFAULT 0,
    total_procesadas INTEGER      NOT NULL DEFAULT 0,
    exitosas         INTEGER      NOT NULL DEFAULT 0,
    errores          INTEGER      NOT NULL DEFAULT 0,
    omitidas         INTEGER      NOT NULL DEFAULT 0,
    subidas_fallidas INTEGER      NOT NULL DEFAULT 0,
    schedule_id      INTEGER,
    creado_en        TIMESTAMP    DEFAULT NOW(),
    iniciado_en      TIMESTAMP,
    completado_en    TIMESTAMP,
    iniciado_por     VARCHAR(100)
);
"""

CREATE_PENDING_SQL = """
CREATE TABLE IF NOT EXISTS worker_pending_uploads (
    id          SERIAL PRIMARY KEY,
    job_id      INTEGER      NOT NULL,
    numero      VARCHAR(50)  NOT NULL,
    datos_json  JSONB        NOT NULL,
    error       TEXT,
    creado_en   TIMESTAMPTZ  DEFAULT NOW(),
    resubido_en TIMESTAMPTZ
);
"""

# worker_causas: una fila por número de causa único.
# Contiene datos de identidad (SATJE) y duración — independiente del job.
CREATE_CAUSAS_SQL = """
CREATE TABLE IF NOT EXISTS worker_causas (
    numero               VARCHAR(50) PRIMARY KEY,
    tribunal_codigo      VARCHAR(10),
    tribunal_nombre      VARCHAR(150),
    año                  VARCHAR(4),
    fecha_ingreso        VARCHAR(20),
    fecha_inicio_caso    DATE,
    fecha_sentencia_caso DATE,
    duracion_dias        INTEGER,
    fecha_inicio_cnj     DATE,
    fecha_sentencia_cnj  DATE,
    duracion_dias_cnj    INTEGER,
    actor                TEXT,
    demandado            TEXT,
    primera_vista        TIMESTAMPTZ DEFAULT NOW(),
    ultima_procesada     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_causas_tribunal ON worker_causas(tribunal_codigo);
CREATE INDEX IF NOT EXISTS idx_causas_año      ON worker_causas(año);
"""

# worker_causa_datos: extracción IA canónica por causa (1:1 con worker_causas).
# Se sobreescribe (ON CONFLICT DO UPDATE) en cada re-ejecución.
CREATE_CAUSA_DATOS_SQL = """
CREATE TABLE IF NOT EXISTS worker_causa_datos (
    numero               VARCHAR(50) PRIMARY KEY,
    estado_juicio        VARCHAR(80),
    resultado            VARCHAR(400),
    resultado_cnj        VARCHAR(400),
    completitud          VARCHAR(60),
    cuantia              TEXT,
    tipo_accion          VARCHAR(80),
    objeto_controversia  TEXT,
    decision_final       VARCHAR(80),
    resumen_ejecutivo    TEXT,
    categorizacion       TEXT,
    abogado_actor        VARCHAR(255),
    abogado_demandado    VARCHAR(255),
    url_pdf_tribunal     TEXT,
    url_docx_tribunal    TEXT,
    url_pdf_cnj          TEXT,
    url_docx_cnj         TEXT,
    faltante_cuantia        BOOLEAN,
    faltante_tipo_accion    BOOLEAN,
    faltante_decision_final BOOLEAN,
    faltante_objeto         BOOLEAN,
    faltante_jurisprudencia BOOLEAN,
    faltante_categorizacion BOOLEAN,
    faltante_fecha_emision  BOOLEAN,
    faltante_otros_actos    BOOLEAN,
    faltante_pruebas        BOOLEAN,
    faltante_abogados       BOOLEAN,
    job_id_origen        INTEGER,
    actualizado_en       TIMESTAMPTZ DEFAULT NOW()
);
"""

# worker_caso_log: log de ejecución (una fila por causa por job).
# Solo guarda el resultado de esta ejecución, no datos de IA.
CREATE_CASO_LOG_SQL = """
CREATE TABLE IF NOT EXISTS worker_caso_log (
    id              SERIAL PRIMARY KEY,
    job_id          INTEGER      NOT NULL,
    numero          VARCHAR(50)  NOT NULL,
    tribunal_codigo VARCHAR(10),
    estado_juicio   VARCHAR(80),
    resultado       VARCHAR(400),
    resultado_cnj   VARCHAR(400),
    completitud     VARCHAR(60),
    observaciones   TEXT,
    procesado_en    TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_caso_log_job      ON worker_caso_log(job_id);
CREATE INDEX IF NOT EXISTS idx_caso_log_numero   ON worker_caso_log(numero);
CREATE INDEX IF NOT EXISTS idx_caso_log_resultado ON worker_caso_log(resultado);
"""

# Migración de bases existentes:
# Elimina columnas movidas a worker_causas / worker_causa_datos
MIGRATE_SQL = """
ALTER TABLE worker_caso_log
    DROP COLUMN IF EXISTS tribunal_nombre,
    DROP COLUMN IF EXISTS año,
    DROP COLUMN IF EXISTS materia,
    DROP COLUMN IF EXISTS fecha_ingreso,
    DROP COLUMN IF EXISTS fecha_inicio_caso,
    DROP COLUMN IF EXISTS fecha_sentencia_caso,
    DROP COLUMN IF EXISTS duracion_dias,
    DROP COLUMN IF EXISTS actor,
    DROP COLUMN IF EXISTS demandado,
    DROP COLUMN IF EXISTS cuantia,
    DROP COLUMN IF EXISTS tipo_accion,
    DROP COLUMN IF EXISTS objeto_controversia,
    DROP COLUMN IF EXISTS decision_final,
    DROP COLUMN IF EXISTS resumen_ejecutivo,
    DROP COLUMN IF EXISTS categorizacion,
    DROP COLUMN IF EXISTS url_pdf_tribunal,
    DROP COLUMN IF EXISTS url_docx_tribunal,
    DROP COLUMN IF EXISTS url_pdf_cnj,
    DROP COLUMN IF EXISTS url_docx_cnj,
    DROP COLUMN IF EXISTS faltante_cuantia,
    DROP COLUMN IF EXISTS faltante_tipo_accion,
    DROP COLUMN IF EXISTS faltante_decision_final,
    DROP COLUMN IF EXISTS faltante_objeto,
    DROP COLUMN IF EXISTS faltante_jurisprudencia,
    DROP COLUMN IF EXISTS faltante_categorizacion,
    DROP COLUMN IF EXISTS faltante_fecha_emision,
    DROP COLUMN IF EXISTS faltante_otros_actos,
    DROP COLUMN IF EXISTS faltante_pruebas,
    DROP COLUMN IF EXISTS faltante_abogados;
"""

CREATE_JOB_LOGS_SQL = """
CREATE TABLE IF NOT EXISTS worker_job_logs (
    id       SERIAL      PRIMARY KEY,
    job_id   INTEGER,
    nivel    VARCHAR(10) NOT NULL,
    causa    VARCHAR(50),
    mensaje  TEXT        NOT NULL,
    ts       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_job_logs_job   ON worker_job_logs(job_id);
CREATE INDEX IF NOT EXISTS idx_job_logs_nivel ON worker_job_logs(nivel);
"""

CREATE_CRON_SQL = """
CREATE TABLE IF NOT EXISTS worker_cron_schedules (
    id          SERIAL       PRIMARY KEY,
    nombre      VARCHAR(100) NOT NULL,
    tribunal    VARCHAR(10)  NOT NULL,
    año         VARCHAR(4)   NOT NULL,
    desde       INTEGER      NOT NULL DEFAULT 1,
    hasta       INTEGER,
    modelo      VARCHAR(100) NOT NULL DEFAULT 'gpt-4o',
    cron_expr   VARCHAR(50)  NOT NULL,
    activo      BOOLEAN      NOT NULL DEFAULT true,
    ultimo_run  TIMESTAMPTZ,
    creado_en   TIMESTAMPTZ  DEFAULT NOW()
);
"""

# Backfill por lote (genérico, cualquier campo de _CAMPOS_FALTANTES):
# worker_backfill_intentos evita reintentar para siempre una causa que
# nunca va a resolverse (se cachea el intento, se prioriza lo nunca
# probado). worker_backfill_stats acumula tiempo real observado para
# poder estimar cuánto tarda un lote ANTES de correrlo.
CREATE_BACKFILL_SQL = """
CREATE TABLE IF NOT EXISTS worker_backfill_intentos (
    numero            VARCHAR(50) NOT NULL,
    campo             VARCHAR(50) NOT NULL,
    intentos          INTEGER     NOT NULL DEFAULT 0,
    ultimo_intento_en TIMESTAMPTZ,
    PRIMARY KEY (numero, campo)
);
CREATE TABLE IF NOT EXISTS worker_backfill_stats (
    campo            VARCHAR(50) PRIMARY KEY,
    total_procesados INTEGER     NOT NULL DEFAULT 0,
    total_segundos   NUMERIC     NOT NULL DEFAULT 0,
    actualizado_en   TIMESTAMPTZ DEFAULT NOW()
);
"""

# worker_prompts: versionado de los prompts que usa el extractor (editables
# desde la webapp, sin tocar código ni redeployar). Una fila por versión;
# a lo sumo UNA versión "activa" por nombre — esa es la que lee el
# extractor. "Sobrescribir" = UPDATE in-place sobre la version activa;
# "nueva versión" = INSERT con version+1 y la marca de activa se mueve.
# Rollback = simplemente reactivar una version vieja.
CREATE_PROMPTS_SQL = """
CREATE TABLE IF NOT EXISTS worker_prompts (
    id         SERIAL PRIMARY KEY,
    nombre     VARCHAR(100) NOT NULL,
    version    INTEGER      NOT NULL,
    contenido  TEXT         NOT NULL,
    activo     BOOLEAN      NOT NULL DEFAULT FALSE,
    nota       VARCHAR(500),
    creado_por VARCHAR(255),
    creado_en  TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (nombre, version)
);
CREATE INDEX IF NOT EXISTS idx_prompts_nombre ON worker_prompts(nombre);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prompts_activo_unico
    ON worker_prompts(nombre) WHERE activo = true;
"""

# Clasificacion interna (prompt 7) de tipo de vicio/defecto alegado por causa,
# para el motor de atribucion de jurimetria. NO es un content-type de Strapi
# ni aparece en el CMS/UI — es una tabla plana propia del worker, igual que
# worker_prompts. Una fila por (causa, codigo) para soportar multi-etiqueta
# de forma nativa (una causa puede alegar varios vicios a la vez).
CREATE_CAUSA_TIPOS_DEFECTO_SQL = """
CREATE TABLE IF NOT EXISTS causa_tipos_defecto (
    id           SERIAL PRIMARY KEY,
    document_id  VARCHAR(255) NOT NULL,
    codigo       VARCHAR(50)  NOT NULL,
    observacion  VARCHAR(500),
    creado_en    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (document_id, codigo)
);
CREATE INDEX IF NOT EXISTS idx_causa_tipos_defecto_document_id
    ON causa_tipos_defecto(document_id);
"""

# Prompt 8: tipo de prueba. A diferencia de causa_tipos_defecto, lleva
# dimension "lado" — quien presento la prueba. Eso habilita el analisis
# asimetrico (que pasa cuando el actor trae pericial y la administracion no),
# imposible con el catalogo viejo (causa_extraccion.tipo_prueba_clave, un solo
# valor por causa y sin lado). Ver migracion 019 en lacortedice-webapp.
# Prompt 10: extracción estructurada (Nivel 2). En tributaria la tabla la creó
# una migración de lacortedice-webapp; las bases nuevas (laboral, civil) no la
# tienen, así que el worker la crea si falta. Misma forma que la de tributaria.
CREATE_CAUSA_EXTRACCION_SQL = """
CREATE TABLE IF NOT EXISTS causa_extraccion (
    document_id       TEXT PRIMARY KEY,
    eje_decision      TEXT,
    prueba_suficiente TEXT,
    tipo_prueba_clave TEXT,
    norma_clave       TEXT,
    precedente_citado TEXT,
    tesis_contraparte TEXT,
    como_superar      TEXT,
    clave_resultado   TEXT,
    cita_textual      TEXT,
    model             TEXT,
    extracted_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_CAUSA_TIPOS_PRUEBA_SQL = """
CREATE TABLE IF NOT EXISTS causa_tipos_prueba (
    document_id  TEXT NOT NULL,
    lado         TEXT NOT NULL CHECK (lado IN ('actor', 'demandado')),
    codigo       TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, lado, codigo)
);
CREATE INDEX IF NOT EXISTS idx_causa_tipos_prueba_doc
    ON causa_tipos_prueba(document_id);
CREATE INDEX IF NOT EXISTS idx_causa_tipos_prueba_codigo
    ON causa_tipos_prueba(codigo);

CREATE TABLE IF NOT EXISTS causa_prueba_analisis (
    document_id       TEXT NOT NULL PRIMARY KEY,
    por_que           TEXT,
    evidencia_ausente TEXT,
    generated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# worker_lista_items: ítems de un job "lista" (extracción de un conjunto
# personalizado de causas, cada una con flags de qué extraer).
CREATE_LISTA_SQL = """
CREATE TABLE IF NOT EXISTS worker_lista_items (
    id           SERIAL PRIMARY KEY,
    job_id       INTEGER NOT NULL,
    ord          INTEGER NOT NULL DEFAULT 0,
    juicio       VARCHAR(40) NOT NULL,
    do_tribunal  BOOLEAN NOT NULL DEFAULT false,
    do_corte     BOOLEAN NOT NULL DEFAULT false,
    estado       VARCHAR(20) NOT NULL DEFAULT 'pendiente',
    resultado    TEXT,
    procesado_en TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_lista_job ON worker_lista_items(job_id);
"""


_IDLE: "queue.LifoQueue" = queue.LifoQueue()
_MAX_IDLE = int(os.environ.get("DB_MAX_IDLE", "20"))


@contextmanager
def _conn():
    """Conexión reutilizada. Abrir una conexión nueva por consulta (varias por
    causa) saturaba la CPU de la base. Mismo uso que antes: `with _conn() as conn`
    hace commit al salir y rollback si hay excepción."""
    try:
        conn = _IDLE.get_nowait()
    except queue.Empty:
        conn = None
    if conn is None or conn.closed:
        conn = psycopg2.connect(DATABASE_URL)
    sana = False
    try:
        with conn:
            yield conn
        sana = True
    finally:
        # shortcut: una conexión que el servidor cortó estando inactiva falla una
        # vez en su siguiente uso y se descarta; si los reinicios de la base se
        # vuelven frecuentes, agregar un ping antes de entregarla.
        if sana and not conn.closed and _IDLE.qsize() < _MAX_IDLE:
            _IDLE.put(conn)
        else:
            try:
                conn.close()
            except Exception:
                pass


# causa_texto: texto íntegro de cada PDF de sentencia (una fila por causa y
# nivel) con índice de texto completo en español, para buscar frases en todo
# el corpus sin volver a abrir los PDF ("¿qué sentencias hablan de X?").
CREATE_CAUSA_TEXTO_SQL = """
CREATE TABLE IF NOT EXISTS causa_texto (
    numero         VARCHAR(50) NOT NULL,
    nivel          VARCHAR(10) NOT NULL CHECK (nivel IN ('tribunal', 'apelacion', 'corte')),
    texto          TEXT        NOT NULL,
    tsv            TSVECTOR    GENERATED ALWAYS AS (to_tsvector('spanish', texto)) STORED,
    actualizado_en TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (numero, nivel)
);
CREATE INDEX IF NOT EXISTS idx_causa_texto_tsv ON causa_texto USING GIN (tsv);
"""

NIVELES_TEXTO = (("tribunal", "_pdf_bytes_tribunal", "_url_pdf_tribunal"),
                 ("apelacion", "_pdf_bytes_apel", "_url_pdf_apel"),
                 ("corte", "_pdf_bytes_cnj", "_url_pdf_cnj"))


def guardar_texto(numero: str, nivel: str, texto: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO causa_texto (numero, nivel, texto)
                VALUES (%s, %s, %s)
                ON CONFLICT (numero, nivel) DO UPDATE
                    SET texto = EXCLUDED.texto, actualizado_en = NOW()
            """, (numero, nivel, texto))
        conn.commit()


def guardar_textos_de_fila(fila: dict) -> None:
    """Indexa el texto de los PDF que traiga la fila (los `_pdf_bytes_*` que
    el orquestador deja tras descargar) y que ya estén guardados en Strapi
    (`_url_pdf_*`), para no indexar corridas dry_run / vista_previa."""
    from resolucion.ai._cliente import extraer_texto_pdf
    numero = fila.get("Número de juicio", "")
    for nivel, clave_pdf, clave_url in NIVELES_TEXTO:
        pdf = fila.get(clave_pdf)
        if numero and pdf and fila.get(clave_url):
            texto = extraer_texto_pdf(pdf, causa=numero)
            if texto:
                guardar_texto(numero, nivel, texto)


def buscar_texto(q: str, tribunal: Optional[str] = None, anio: Optional[str] = None,
                 nivel: Optional[str] = None, limite: int = 50, offset: int = 0) -> dict:
    """Búsqueda de texto completo (websearch: comillas para frase exacta, -palabra
    para excluir). Devuelve total y filas con un fragmento resaltado."""
    sql_base = """
        FROM causa_texto t
        LEFT JOIN worker_causas c ON c.numero = t.numero
        LEFT JOIN worker_causa_datos d ON d.numero = t.numero
        WHERE t.tsv @@ websearch_to_tsquery('spanish', %s)
    """
    params: list = [q]
    if tribunal:
        sql_base += " AND c.tribunal_codigo = %s"; params.append(tribunal)
    if anio:
        sql_base += " AND c.año = %s"; params.append(anio)
    if nivel:
        sql_base += " AND t.nivel = %s"; params.append(nivel)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT count(*) AS n " + sql_base, params)
            total = cur.fetchone()["n"]
            cur.execute(f"""
                SELECT t.numero, t.nivel, c.tribunal_codigo, c.tribunal_nombre, c.año,
                       c.actor, c.demandado, c.fecha_sentencia_caso,
                       d.url_pdf_tribunal, d.url_pdf_apel, d.url_pdf_cnj,
                       ts_headline('spanish', t.texto, websearch_to_tsquery('spanish', %s),
                                   'MaxWords=40, MinWords=20, MaxFragments=2') AS fragmento
                {sql_base}
                ORDER BY ts_rank(t.tsv, websearch_to_tsquery('spanish', %s)) DESC, t.numero
                LIMIT %s OFFSET %s
            """, [q] + params + [q, int(limite), int(offset)])
            filas = [dict(r) for r in cur.fetchall()]
    return {"total": total, "resultados": filas}


def init_db():
    """Crea/migra las tablas del worker."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(CREATE_CAUSA_TEXTO_SQL)
            cur.execute(CREATE_PENDING_SQL)
            cur.execute(CREATE_CAUSAS_SQL)
            cur.execute(CREATE_CAUSA_DATOS_SQL)
            cur.execute(CREATE_CASO_LOG_SQL)
            cur.execute(CREATE_JOB_LOGS_SQL)
            cur.execute(CREATE_CRON_SQL)
            cur.execute(CREATE_BACKFILL_SQL)
            cur.execute(CREATE_PROMPTS_SQL)
            cur.execute(CREATE_CAUSA_TIPOS_DEFECTO_SQL)
            cur.execute(CREATE_CAUSA_TIPOS_PRUEBA_SQL)
            cur.execute(CREATE_CAUSA_EXTRACCION_SQL)
            cur.execute(CREATE_LISTA_SQL)
            # Strapi no indexa `causas.juicio` y el worker la busca en cada guardado.
            cur.execute("""
                DO $$ BEGIN
                  IF to_regclass('public.causas') IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM pg_indexes
                        WHERE tablename = 'causas' AND indexdef LIKE '%(juicio)%') THEN
                    CREATE INDEX idx_causas_juicio ON causas (juicio);
                  END IF;
                END $$;""")

            # Columnas opcionales en worker_jobs (backward compat)
            for col_sql in [
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS subidas_fallidas INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS omitidas INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS workers VARCHAR(100) NOT NULL DEFAULT ''",
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS categoria VARCHAR(50) NOT NULL DEFAULT 'extraccion'",
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS schedule_id INTEGER",
                "ALTER TABLE worker_caso_log ADD COLUMN IF NOT EXISTS tribunal_codigo VARCHAR(10)",
                "ALTER TABLE worker_causa_datos ADD COLUMN IF NOT EXISTS abogado_actor VARCHAR(255)",
                "ALTER TABLE worker_causa_datos ADD COLUMN IF NOT EXISTS abogado_demandado VARCHAR(255)",
                "ALTER TABLE worker_causas ADD COLUMN IF NOT EXISTS fecha_inicio_cnj DATE",
                "ALTER TABLE worker_causas ADD COLUMN IF NOT EXISTS fecha_sentencia_cnj DATE",
                "ALTER TABLE worker_causas ADD COLUMN IF NOT EXISTS duracion_dias_cnj INTEGER",
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS vista_previa BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS flujo_proveedores VARCHAR(20) NOT NULL DEFAULT 'alibaba'",
                # Latido: lo sella _actualizar en cada avance. Sin esto no hay
                # forma de distinguir un job que trabaja de uno colgado.
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS latido TIMESTAMPTZ",
                # solo_descarga: baja y persiste PDFs + metadatos sin llamar a la IA
                # (fase "corpus primero"); la IA se corre después con reextraccion.
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS solo_descarga BOOLEAN NOT NULL DEFAULT false",
                # Una revisión partida en N jobs lleva en `workers` los códigos de
                # sus judicaturas (t=09359,17371,…); con 154 judicaturas no caben
                # en 100 caracteres y crear_job fallaba.
                # Tercer nivel (apelación, Corte Provincial) en el registro canónico.
                "ALTER TABLE worker_causa_datos ADD COLUMN IF NOT EXISTS resultado_apel VARCHAR(400)",
                "ALTER TABLE worker_causa_datos ADD COLUMN IF NOT EXISTS url_pdf_apel TEXT",
                "ALTER TABLE worker_causa_datos ADD COLUMN IF NOT EXISTS url_docx_apel TEXT",
                "ALTER TABLE worker_causa_datos ADD COLUMN IF NOT EXISTS completitud_cnj VARCHAR(60)",
                "ALTER TABLE worker_causa_datos ADD COLUMN IF NOT EXISTS completitud_apel VARCHAR(60)",
                # Sin estas dos, un schedule solo podía lanzar extracciones
                # normales: no había dónde guardar QUÉ tipo de job disparar.
                # Con ellas se pueden programar embeddings, briefs y revisión.
                "ALTER TABLE worker_cron_schedules ADD COLUMN IF NOT EXISTS workers VARCHAR(100) NOT NULL DEFAULT ''",
                "ALTER TABLE worker_cron_schedules ADD COLUMN IF NOT EXISTS categoria VARCHAR(50) NOT NULL DEFAULT 'cron'",
                # Los dos ALTER ... TYPE van DESPUES de los ADD COLUMN: sobre una
                # base nueva, worker_cron_schedules se crea sin la columna
                # `workers` (no esta en su CREATE TABLE), asi que cambiarle el
                # tipo antes de agregarla aborta init_db() con UndefinedColumn y
                # el worker no arranca. Comprobado contra Postgres 18 con el
                # esquema vacio.
                "ALTER TABLE worker_jobs ALTER COLUMN workers TYPE TEXT",
                "ALTER TABLE worker_cron_schedules ALTER COLUMN workers TYPE TEXT",
                # parámetros libres (JSON) de jobs que no caben en tribunal/año/workers
                # — p. ej. el índice de SATJE (lista de prefijos y años).
                "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS parametros TEXT",
            ]:
                cur.execute(col_sql)

            # Migrar worker_caso_log existente: eliminar columnas que se movieron
            cur.execute(MIGRATE_SQL)

            # FKs con NOT VALID: aplican a filas nuevas, no validan existentes
            for fk_sql in [
                """
                DO $$ BEGIN
                    ALTER TABLE worker_caso_log
                        ADD CONSTRAINT fk_caso_log_job
                        FOREIGN KEY (job_id) REFERENCES worker_jobs(id) ON DELETE CASCADE
                        NOT VALID;
                EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                """,
                """
                DO $$ BEGIN
                    ALTER TABLE worker_pending_uploads
                        ADD CONSTRAINT fk_pending_job
                        FOREIGN KEY (job_id) REFERENCES worker_jobs(id) ON DELETE CASCADE
                        NOT VALID;
                EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                """,
                """
                DO $$ BEGIN
                    ALTER TABLE worker_job_logs
                        ADD CONSTRAINT fk_logs_job
                        FOREIGN KEY (job_id) REFERENCES worker_jobs(id) ON DELETE SET NULL
                        NOT VALID;
                EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                """,
                """
                DO $$ BEGIN
                    ALTER TABLE worker_causa_datos
                        ADD CONSTRAINT fk_causa_datos_causa
                        FOREIGN KEY (numero) REFERENCES worker_causas(numero) ON DELETE CASCADE
                        NOT VALID;
                EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                """,
            ]:
                cur.execute(fk_sql)

        conn.commit()


# ── Estado en memoria (señales de control por job) ────────────────────────────

# job_id -> {"pause": threading.Event, "kill": threading.Event, "thread": Thread}
_active: dict[int, dict] = {}
_lock = threading.Lock()

# Cola SSE: job_id -> lista de subscribers (queues asyncio)
_sse_queues: dict[int, list] = {}
_sse_lock = threading.Lock()

# Último evento "procesando" por job: se manda inmediatamente a nuevos suscriptores
# para que vean la causa activa aunque conecten después de que empezó.
_procesando_actual: dict[int, dict] = {}


# ── CRUD de jobs ──────────────────────────────────────────────────────────────

def crear_job(tribunal: str, año: str, desde: int, hasta: Optional[int],
              dry_run: bool, modelo: str, iniciado_por: str = "",
              workers: str = "", categoria: str = "extraccion",
              schedule_id: Optional[int] = None, vista_previa: bool = False,
              flujo_proveedores: str = "alibaba", arrancar: bool = True,
              solo_descarga: bool = False, parametros: Optional[str] = None) -> dict:
    sql = """
        INSERT INTO worker_jobs
            (tribunal, año, desde, hasta, dry_run, modelo, estado,
             iniciado_por, workers, categoria, schedule_id, vista_previa, flujo_proveedores,
             solo_descarga, parametros)
        VALUES (%s, %s, %s, %s, %s, %s, 'pendiente', %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (tribunal, año, desde, hasta, dry_run, modelo,
                              iniciado_por, workers, categoria, schedule_id,
                              vista_previa, flujo_proveedores, solo_descarga, parametros))
            job = dict(cur.fetchone())
        conn.commit()
    if arrancar:
        _intentar_arrancar(job["id"])
    return job


def crear_job_lista(items: list, modelo: str, iniciado_por: str = "",
                    flujo_proveedores: str = "alibaba") -> dict:
    """
    Crea un job de tipo "lista": procesa un conjunto PERSONALIZADO de causas,
    cada una con flags de qué extraer (tribunal / corte). `items` es una lista
    de tuplas (juicio, do_tribunal, do_corte). El tribunal/año del job quedan
    en "-" porque la lista puede cruzar varios; el rango real es la lista.
    """
    # arrancar=False: primero se insertan los ítems y RECIÉN se arranca el job.
    # Si se arrancara aquí (dentro de crear_job), el worker podría leer 0 ítems
    # pendientes antes de que se inserten y completar la lista vacía (race real).
    job = crear_job(
        tribunal="-", año="-", desde=1, hasta=len(items), dry_run=False,
        modelo=modelo, iniciado_por=iniciado_por, workers="lista",
        categoria="extraccion", flujo_proveedores=flujo_proveedores,
        arrancar=False,
    )
    with _conn() as conn:
        with conn.cursor() as cur:
            for i, (juicio, do_trib, do_corte) in enumerate(items):
                cur.execute(
                    "INSERT INTO worker_lista_items (job_id, ord, juicio, do_tribunal, do_corte) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (job["id"], i, juicio, bool(do_trib), bool(do_corte)),
                )
        conn.commit()
    _intentar_arrancar(job["id"])   # ya con los ítems en la base
    return job


# ── Re-extracción desde PDFs guardados (worker/reextraccion_worker.py) ────────

def obtener_causa_datos(numero: str) -> Optional[dict]:
    """Fila de worker_causa_datos (extracción canónica + URLs de PDF) o None."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM worker_causa_datos WHERE numero=%s", (numero,))
            r = cur.fetchone()
            return dict(r) if r else None


def obtener_causa(numero: str) -> Optional[dict]:
    """Fila de worker_causas (identidad SATJE: tribunal, partes, fechas) o None."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM worker_causas WHERE numero=%s", (numero,))
            r = cur.fetchone()
            return dict(r) if r else None


def listar_numeros_reextraer(tribunal: Optional[str] = None, anio: Optional[str] = None,
                             completitud: Optional[str] = None, niveles: Optional[list] = None,
                             limite: Optional[int] = None) -> list[str]:
    """
    Números de causa candidatos a re-extracción: tienen PDF guardado para al
    menos uno de los niveles pedidos. Filtros opcionales por tribunal/año
    (worker_causas), y por completitud de primera instancia (p. ej.
    "PDF sin análisis" tras un job solo_descarga, o "Parcial - revisar").
    """
    niveles = niveles or ["tribunal", "corte", "apelacion"]
    col = {"tribunal": "d.url_pdf_tribunal", "corte": "d.url_pdf_cnj", "apelacion": "d.url_pdf_apel"}
    tiene_pdf = " OR ".join(f"COALESCE({col[n]}, '') <> ''" for n in niveles if n in col)
    sql = f"""
        SELECT d.numero
        FROM worker_causa_datos d
        LEFT JOIN worker_causas c ON c.numero = d.numero
        WHERE ({tiene_pdf})
    """
    params: list = []
    if tribunal:
        sql += " AND c.tribunal_codigo = %s"; params.append(tribunal)
    if anio:
        sql += " AND c.año = %s"; params.append(anio)
    if completitud:
        sql += " AND d.completitud = %s"; params.append(completitud)
    sql += " ORDER BY d.numero"
    if limite:
        sql += " LIMIT %s"; params.append(int(limite))
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [r[0] for r in cur.fetchall()]


def crear_job_reextraccion(numeros: list, modelo: str, niveles: list, tribunal: str = "-",
                           anio: str = "-", completitud: Optional[str] = None,
                           limite: Optional[int] = None, iniciado_por: str = "",
                           flujo_proveedores: str = "alibaba") -> dict:
    """Job de re-extracción. Con `numeros` procesa esa lista; sin ella, el filtro."""
    # Niveles separados por "+": el runner parte la columna workers por comas,
    # así que "niveles=tribunal,corte" quedaba reducido a "tribunal".
    marca = "reextraccion:niveles=" + "+".join(niveles)
    if completitud:
        marca += f":completitud={completitud}"
    if limite:
        marca += f":limite={int(limite)}"
    job = crear_job(
        tribunal=tribunal or "-", año=anio or "-", desde=1, hasta=len(numeros) or None,
        dry_run=False, modelo=modelo, iniciado_por=iniciado_por, workers=marca,
        categoria="extraccion", flujo_proveedores=flujo_proveedores, arrancar=False,
    )
    if numeros:
        with _conn() as conn:
            with conn.cursor() as cur:
                for i, n in enumerate(numeros):
                    cur.execute(
                        "INSERT INTO worker_lista_items (job_id, ord, juicio, do_tribunal, do_corte) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (job["id"], i, n, "tribunal" in niveles, "corte" in niveles or "apelacion" in niveles),
                    )
            conn.commit()
    _intentar_arrancar(job["id"])
    return job


def listar_items_lista(job_id: int, solo_pendientes: bool = False) -> list[dict]:
    sql = ("SELECT * FROM worker_lista_items WHERE job_id=%s "
           + ("AND estado='pendiente' " if solo_pendientes else "")
           + "ORDER BY ord")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (job_id,))
            return [dict(r) for r in cur.fetchall()]


def actualizar_item_lista(item_id: int, estado: str, resultado: str = None):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_lista_items SET estado=%s, resultado=%s, procesado_en=%s "
                "WHERE id=%s",
                (estado, (resultado or "")[:500], _now(), item_id),
            )
        conn.commit()


_CAMPOS_TIMESTAMP_JOB = ("creado_en", "iniciado_en", "completado_en")


def _marcar_utc(row: dict) -> dict:
    """
    worker_jobs.creado_en/iniciado_en/completado_en son TIMESTAMP sin zona
    horaria, pero siempre se escriben en UTC (NOW() / _now()) — Postgres les
    borra el offset al guardar. Sin esto, el JSON sale sin 'Z'/offset y el
    navegador los interpreta como si ya fueran hora local (bug real: un job
    de las 12:25pm Ecuador se mostraba como "17:25", tal cual UTC sin
    convertir). Se marcan acá, no se migra la columna en producción.
    """
    for campo in _CAMPOS_TIMESTAMP_JOB:
        valor = row.get(campo)
        if isinstance(valor, datetime) and valor.tzinfo is None:
            row[campo] = valor.replace(tzinfo=timezone.utc)
    return row


def obtener_job(job_id: int) -> Optional[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM worker_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
    return _marcar_utc(dict(row)) if row else None


def listar_jobs() -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM worker_jobs ORDER BY id DESC LIMIT 100")
            return [_marcar_utc(dict(r)) for r in cur.fetchall()]


def _actualizar(job_id: int, **campos):
    if not campos:
        return
    sets = ", ".join(f"{k} = %s" for k in campos)
    vals = list(campos.values()) + [job_id]
    with _conn() as conn:
        with conn.cursor() as cur:
            # El latido va en el mismo UPDATE: cada avance deja constancia de
            # que el job sigue vivo, y vigilar_jobs_colgados() puede separar
            # "trabajando" de "colgado" sin depender de los logs (que desde
            # LOG_LEVEL_DB=WARN ya no registran el avance normal).
            cur.execute(f"UPDATE worker_jobs SET {sets}, latido = NOW() WHERE id = %s", vals)
        conn.commit()


# ── Progreso (llamado desde runner.py en cada causa procesada) ────────────────

def reportar_progreso(job_id: int, checkpoint: int, exitosa: bool, evento: dict,
                      strapi_fallo: bool = False, omitida: bool = False):
    """
    Actualiza contadores en Postgres y emite evento SSE a todos los suscriptores.
    Llamado desde el hilo de extracción después de cada causa.
    - strapi_fallo=True: extracción OK pero subida a Strapi falló.
    - omitida=True: caso procesado sin sentencia (abandono, en trámite, etc.) — no se sube a Strapi.
    """
    if exitosa:
        col_update = "exitosas = exitosas + 1"
    elif strapi_fallo:
        col_update = "errores = errores + 1, subidas_fallidas = subidas_fallidas + 1"
    elif omitida:
        col_update = "omitidas = omitidas + 1"
    else:
        col_update = "errores = errores + 1"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE worker_jobs
                SET checkpoint = %s,
                    total_procesadas = total_procesadas + 1,
                    latido = NOW(),
                    {col_update}
                WHERE id = %s
            """, (checkpoint, job_id))
        conn.commit()
    _emitir_sse(job_id, evento)


# ── Registro de casos (log oficial) ──────────────────────────────────────────

def registrar_caso(job_id: int, fila: dict):
    """
    Registra una causa procesada en las tres tablas canónicas:
      1. worker_causas      — datos SATJE/identidad (upsert: actualiza si ya existe)
      2. worker_causa_datos — extracción IA (upsert: solo si la causa tiene sentencia)
      3. worker_caso_log    — log de ejecución (insert siempre)
    """
    numero          = fila.get("Número de juicio", "")
    tribunal_codigo = fila.get("_tribunal_codigo", "")
    auditoria       = fila.get("_auditoria") or {}

    def _f(campo: str):
        """None=sin análisis IA; True=falta; False=presente."""
        if not auditoria:
            return None
        return not auditoria.get(campo, True)

    try:
        guardar_textos_de_fila(fila)
    except Exception as e:  # el índice de texto es secundario: nunca tumba el registro
        from worker import logger as wlog
        wlog.error(f"causa_texto — {type(e).__name__}: {e}", job_id=job_id, causa=numero)

    with _conn() as conn:
        with conn.cursor() as cur:

            # ── 1. UPSERT worker_causas ──────────────────────────────────────
            cur.execute("""
                INSERT INTO worker_causas
                    (numero, tribunal_codigo, tribunal_nombre, año, fecha_ingreso,
                     fecha_inicio_caso, fecha_sentencia_caso, duracion_dias,
                     fecha_inicio_cnj, fecha_sentencia_cnj, duracion_dias_cnj,
                     actor, demandado, primera_vista, ultima_procesada)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (numero) DO UPDATE SET
                    tribunal_codigo      = EXCLUDED.tribunal_codigo,
                    tribunal_nombre      = EXCLUDED.tribunal_nombre,
                    fecha_ingreso        = COALESCE(EXCLUDED.fecha_ingreso,        worker_causas.fecha_ingreso),
                    fecha_inicio_caso    = COALESCE(EXCLUDED.fecha_inicio_caso,    worker_causas.fecha_inicio_caso),
                    fecha_sentencia_caso = COALESCE(EXCLUDED.fecha_sentencia_caso, worker_causas.fecha_sentencia_caso),
                    duracion_dias        = COALESCE(EXCLUDED.duracion_dias,        worker_causas.duracion_dias),
                    fecha_inicio_cnj     = COALESCE(EXCLUDED.fecha_inicio_cnj,     worker_causas.fecha_inicio_cnj),
                    fecha_sentencia_cnj  = COALESCE(EXCLUDED.fecha_sentencia_cnj,  worker_causas.fecha_sentencia_cnj),
                    duracion_dias_cnj    = COALESCE(EXCLUDED.duracion_dias_cnj,    worker_causas.duracion_dias_cnj),
                    actor                = COALESCE(EXCLUDED.actor,                worker_causas.actor),
                    demandado            = COALESCE(EXCLUDED.demandado,            worker_causas.demandado),
                    ultima_procesada     = NOW()
            """, (
                numero,
                tribunal_codigo,
                str(fila.get("Tribunal", "") or "")[:150],
                str(fila.get("Año", "")),
                str(fila.get("Fecha de ingreso", "") or ""),
                fila.get("_fecha_inicio_caso") or None,
                fila.get("_fecha_sentencia_caso") or None,
                fila.get("_duracion_dias") if fila.get("_duracion_dias") is not None else None,
                fila.get("_fecha_inicio_cnj") or None,
                fila.get("_fecha_sentencia_cnj") or None,
                fila.get("_duracion_dias_cnj") if fila.get("_duracion_dias_cnj") is not None else None,
                str(fila.get("Actor", "") or "")[:500] or None,
                str(fila.get("Demandado", "") or "")[:500] or None,
            ))

            # ── 2. UPSERT worker_causa_datos ──────────────────────────────────
            # Siempre corre — estado_juicio/resultado/resultado_cnj/completitud
            # vienen de SATJE, no de IA (se calculan igual en dry_run o si la
            # IA falló). Antes esto estaba atado a `auditoria` (que solo existe
            # si corrió la extracción IA), y esos campos quedaban vacíos en
            # /admin/registros en cualquier corrida sin IA — no es que faltara
            # el dato, es que nunca se guardaba. Los campos que sí dependen de
            # IA (decision_final, cuantia, etc.) simplemente quedan vacíos
            # cuando no hay auditoria, que es lo correcto.
            cur.execute("""
                INSERT INTO worker_causa_datos
                    (numero, estado_juicio, resultado, resultado_cnj, completitud,
                     cuantia, tipo_accion, objeto_controversia, decision_final,
                     resumen_ejecutivo, categorizacion, abogado_actor, abogado_demandado,
                     url_pdf_tribunal, url_docx_tribunal, url_pdf_cnj, url_docx_cnj,
                     faltante_cuantia, faltante_tipo_accion, faltante_decision_final,
                     faltante_objeto, faltante_jurisprudencia, faltante_categorizacion,
                     faltante_fecha_emision, faltante_otros_actos, faltante_pruebas,
                     faltante_abogados, job_id_origen,
                     resultado_apel, url_pdf_apel, url_docx_apel, completitud_cnj, completitud_apel,
                     actualizado_en)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,NOW())
                ON CONFLICT (numero) DO UPDATE SET
                    estado_juicio        = EXCLUDED.estado_juicio,
                    resultado            = EXCLUDED.resultado,
                    resultado_cnj        = EXCLUDED.resultado_cnj,
                    completitud          = EXCLUDED.completitud,
                    cuantia              = EXCLUDED.cuantia,
                    tipo_accion          = EXCLUDED.tipo_accion,
                    objeto_controversia  = EXCLUDED.objeto_controversia,
                    decision_final       = EXCLUDED.decision_final,
                    resumen_ejecutivo    = EXCLUDED.resumen_ejecutivo,
                    categorizacion       = EXCLUDED.categorizacion,
                    abogado_actor        = COALESCE(EXCLUDED.abogado_actor,     worker_causa_datos.abogado_actor),
                    abogado_demandado    = COALESCE(EXCLUDED.abogado_demandado, worker_causa_datos.abogado_demandado),
                    url_pdf_tribunal     = COALESCE(EXCLUDED.url_pdf_tribunal,  worker_causa_datos.url_pdf_tribunal),
                    url_docx_tribunal    = COALESCE(EXCLUDED.url_docx_tribunal, worker_causa_datos.url_docx_tribunal),
                    url_pdf_cnj          = COALESCE(EXCLUDED.url_pdf_cnj,       worker_causa_datos.url_pdf_cnj),
                    url_docx_cnj         = COALESCE(EXCLUDED.url_docx_cnj,      worker_causa_datos.url_docx_cnj),
                    faltante_cuantia        = EXCLUDED.faltante_cuantia,
                    faltante_tipo_accion    = EXCLUDED.faltante_tipo_accion,
                    faltante_decision_final = EXCLUDED.faltante_decision_final,
                    faltante_objeto         = EXCLUDED.faltante_objeto,
                    faltante_jurisprudencia = EXCLUDED.faltante_jurisprudencia,
                    faltante_categorizacion = EXCLUDED.faltante_categorizacion,
                    faltante_fecha_emision  = EXCLUDED.faltante_fecha_emision,
                    faltante_otros_actos    = EXCLUDED.faltante_otros_actos,
                    faltante_pruebas        = EXCLUDED.faltante_pruebas,
                    faltante_abogados       = EXCLUDED.faltante_abogados,
                    job_id_origen           = EXCLUDED.job_id_origen,
                    resultado_apel          = COALESCE(EXCLUDED.resultado_apel,   worker_causa_datos.resultado_apel),
                    url_pdf_apel            = COALESCE(EXCLUDED.url_pdf_apel,     worker_causa_datos.url_pdf_apel),
                    url_docx_apel           = COALESCE(EXCLUDED.url_docx_apel,    worker_causa_datos.url_docx_apel),
                    completitud_cnj         = COALESCE(EXCLUDED.completitud_cnj,  worker_causa_datos.completitud_cnj),
                    completitud_apel        = COALESCE(EXCLUDED.completitud_apel, worker_causa_datos.completitud_apel),
                    actualizado_en          = NOW()
            """, (
                numero,
                fila.get("Estado de Juicio", ""),
                fila.get("Resultado tribunal", ""),
                fila.get("Resultado en Corte", ""),
                fila.get("Completitud", ""),
                str(fila.get("Cuantía", "") or "")[:200] or None,
                str(fila.get("Tipo de Acción", "") or "")[:80] or None,
                str(fila.get("Objeto Controversia", "") or "")[:1000] or None,
                str(fila.get("Decisión Final", "") or "")[:80] or None,
                str(fila.get("Resumen Ejecutivo", "") or "") or None,
                str(fila.get("Categorización", "") or "") or None,
                str(fila.get("Abogado del Actor", "") or "")[:255] or None,
                str(fila.get("Abogado del Demandado", "") or "")[:255] or None,
                fila.get("_url_pdf_tribunal") or None,
                fila.get("_url_docx_tribunal") or None,
                fila.get("_url_pdf_cnj") or None,
                fila.get("_url_docx_cnj") or None,
                _f("cuantia"), _f("tipo_accion"), _f("decision_final"),
                _f("objeto"), _f("jurisprudencia"), _f("categorizacion"),
                _f("fecha_emision"), _f("otros_actos"), _f("pruebas"),
                _f("abogados"),
                job_id,
                (fila.get("Resultado en Apelación", "") or None),
                fila.get("_url_pdf_apel") or None,
                fila.get("_url_docx_apel") or None,
                (fila.get("Completitud CNJ", "") or None),
                (fila.get("Completitud Apelación", "") or None),
            ))

            # ── 3. INSERT worker_caso_log (audit) ────────────────────────────
            cur.execute("""
                INSERT INTO worker_caso_log
                    (job_id, numero, tribunal_codigo,
                     estado_juicio, resultado, resultado_cnj,
                     completitud, observaciones)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                job_id,
                numero,
                tribunal_codigo,
                fila.get("Estado de Juicio", ""),
                fila.get("Resultado tribunal", ""),
                fila.get("Resultado en Corte", ""),
                fila.get("Completitud", ""),
                str(fila.get("Observaciones", "") or "")[:800],
            ))

        conn.commit()


_CAMPOS_FALTANTES = [
    "cuantia", "tipo_accion", "decision_final", "objeto",
    "jurisprudencia", "categorizacion", "fecha_emision",
    "otros_actos", "pruebas", "abogados",
]


def listar_checklist(job_id: int, campo: str = None) -> dict:
    """
    Checklist de campos faltantes para causas con sentencia de un job.
    ?campo=cuantia → filtra causas donde ese campo específico falta.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (cl.numero) cl.numero,
                       cd.faltante_cuantia, cd.faltante_tipo_accion, cd.faltante_decision_final,
                       cd.faltante_objeto, cd.faltante_jurisprudencia, cd.faltante_categorizacion,
                       cd.faltante_fecha_emision, cd.faltante_otros_actos, cd.faltante_pruebas,
                       cd.faltante_abogados
                FROM worker_caso_log cl
                JOIN worker_causa_datos cd ON cd.numero = cl.numero
                WHERE cl.job_id = %s
                  AND cl.resultado LIKE '%%Con sentencia%%'
                ORDER BY cl.numero, cl.id DESC
            """, (job_id,))
            filas = [dict(r) for r in cur.fetchall()]

    total = len(filas)
    por_campo: dict = {c: {"faltantes": 0, "presentes": 0} for c in _CAMPOS_FALTANTES}
    causas_resultado = []

    for fila in filas:
        faltantes_causa = []
        for c in _CAMPOS_FALTANTES:
            val = fila.get(f"faltante_{c}")
            if val is True:
                por_campo[c]["faltantes"] += 1
                faltantes_causa.append(c)
            elif val is False:
                por_campo[c]["presentes"] += 1

        if campo:
            if campo in faltantes_causa:
                causas_resultado.append({"numero": fila["numero"], "faltantes": faltantes_causa})
        else:
            if faltantes_causa:
                causas_resultado.append({"numero": fila["numero"], "faltantes": faltantes_causa})

    con_faltantes = len(causas_resultado)
    return {
        "job_id":              job_id,
        "total_con_sentencia": total,
        "con_faltantes":       con_faltantes,
        "sin_faltantes":       total - con_faltantes,
        "por_campo":           por_campo,
        "causas":              causas_resultado,
    }


MAX_INTENTOS_BACKFILL = 3


def _validar_campo_backfill(campo: str) -> None:
    if campo not in _CAMPOS_FALTANTES:
        raise ValueError(f"Campo inválido: {campo!r}. Opciones: {_CAMPOS_FALTANTES}")


_MODOS_BACKFILL = ("completar", "sobrescribir")


def _validar_modo_backfill(modo: str) -> None:
    if modo not in _MODOS_BACKFILL:
        raise ValueError(f"Modo inválido: {modo!r}. Opciones: {_MODOS_BACKFILL}")


def _where_backfill(campo: str, modo: str) -> str:
    """
    "completar" (default): solo causas con faltante_<campo>=true — no
    toca nada que ya tenga el dato.
    "sobrescribir": TODAS las causas con sentencia ya procesada, tengan
    o no el dato — para forzar una re-extracción completa del campo
    (ej. después de mejorar un prompt y querer parejo todo el historial,
    no solo lo que decía "No consta").
    """
    if modo == "sobrescribir":
        return "cd.resultado LIKE '%Con sentencia%'"
    return f"cd.faltante_{campo} = true"


def _where_rango(tribunal: Optional[str], año: Optional[str],
                  desde: Optional[int], hasta: Optional[int]) -> tuple:
    """
    Filtro opcional de tribunal/año/rango de número para los backfills —
    sin esto, un backfill lanzado desde /admin/worker (que sí deja elegir
    tribunal/año/desde/hasta) ignoraba esos campos y agarraba "el próximo
    lote" de TODA la base, no solo del rango pedido. tribunal="-" (el
    placeholder que usa el endpoint /backfill genérico, sin rango) se
    trata como "sin filtro", igual que None.
    """
    if not tribunal or tribunal == "-" or not año:
        return "", []
    numero_desde = f"{tribunal}-{año}-{(desde or 1):05d}"
    if hasta:
        numero_hasta = f"{tribunal}-{año}-{hasta:05d}"
        return " AND wc.tribunal_codigo = %s AND wc.año = %s AND wc.numero BETWEEN %s AND %s", \
               [tribunal, año, numero_desde, numero_hasta]
    return " AND wc.tribunal_codigo = %s AND wc.año = %s AND wc.numero >= %s", \
           [tribunal, año, numero_desde]


def contar_pendientes_backfill(campo: str, modo: str = "completar",
                               tribunal: Optional[str] = None, año: Optional[str] = None,
                               desde: Optional[int] = None, hasta: Optional[int] = None) -> int:
    """Cuántas causas son candidatas para este campo/modo (y rango, si se pasa) y no agotaron los reintentos."""
    _validar_campo_backfill(campo)
    _validar_modo_backfill(modo)
    where = _where_backfill(campo, modo)
    where_rango, params_rango = _where_rango(tribunal, año, desde, hasta)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT count(*) FROM worker_causa_datos cd
                JOIN worker_causas wc ON wc.numero = cd.numero
                LEFT JOIN worker_backfill_intentos bi
                    ON bi.numero = cd.numero AND bi.campo = %s
                WHERE {where} {where_rango}
                  AND COALESCE(bi.intentos, 0) < %s
            """, [campo] + params_rango + [MAX_INTENTOS_BACKFILL])
            return cur.fetchone()[0]


def listar_pendientes_backfill(campo: str, limite: int, modo: str = "completar",
                               tribunal: Optional[str] = None, año: Optional[str] = None,
                               desde: Optional[int] = None, hasta: Optional[int] = None) -> list[dict]:
    """
    Próximo lote de causas candidatas para este campo/modo, priorizando
    las que menos intentos tuvieron (nunca probadas primero) — así cada
    corrida agarra el "próximo lote" automático sin necesidad de un
    offset explícito, tanto para completar (las resueltas dejan de
    calificar) como para sobrescribir (todas califican siempre, pero
    las recién tocadas quedan de últimas la próxima vez).

    modo="completar" (default): solo cd.faltante_<campo>=true.
    modo="sobrescribir": todas las causas con sentencia, sin importar
    si ya tienen el dato — y sin tope de reintentos, no es un retry de
    fallos, es una pasada deliberada sobre todo el historial.

    tribunal/año/desde/hasta (opcionales): acota a ese rango de causas —
    ver _where_rango(). Sin esto (o tribunal="-"), toma de toda la base.
    """
    _validar_campo_backfill(campo)
    _validar_modo_backfill(modo)
    where = _where_backfill(campo, modo)
    where_rango, params_rango = _where_rango(tribunal, año, desde, hasta)
    tope_intentos = "AND COALESCE(bi.intentos, 0) < %s" if modo == "completar" else ""
    params = [campo] + params_rango + ([MAX_INTENTOS_BACKFILL] if modo == "completar" else []) + [limite]
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT cd.numero, wc.actor, wc.demandado, COALESCE(bi.intentos, 0) AS intentos
                FROM worker_causa_datos cd
                JOIN worker_causas wc ON wc.numero = cd.numero
                LEFT JOIN worker_backfill_intentos bi
                    ON bi.numero = cd.numero AND bi.campo = %s
                WHERE {where} {where_rango}
                {tope_intentos}
                ORDER BY COALESCE(bi.intentos, 0) ASC, cd.numero ASC
                LIMIT %s
            """, params)
            return [dict(r) for r in cur.fetchall()]


def registrar_intento_backfill(numero: str, campo: str) -> None:
    """Suma un intento para (numero, campo) — se llama tras CADA intento, exitoso o no."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO worker_backfill_intentos (numero, campo, intentos, ultimo_intento_en)
                VALUES (%s, %s, 1, NOW())
                ON CONFLICT (numero, campo) DO UPDATE SET
                    intentos = worker_backfill_intentos.intentos + 1,
                    ultimo_intento_en = NOW()
            """, (numero, campo))
        conn.commit()


def registrar_stats_backfill(campo: str, procesados: int, segundos: float) -> None:
    """Acumula tiempo real observado — alimenta estimar_tiempo_backfill()."""
    if procesados <= 0:
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO worker_backfill_stats (campo, total_procesados, total_segundos, actualizado_en)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (campo) DO UPDATE SET
                    total_procesados = worker_backfill_stats.total_procesados + EXCLUDED.total_procesados,
                    total_segundos   = worker_backfill_stats.total_segundos + EXCLUDED.total_segundos,
                    actualizado_en   = NOW()
            """, (campo, procesados, segundos))
        conn.commit()


def estimar_tiempo_backfill(campo: str, cantidad: int) -> dict:
    """
    Estima cuánto tardaría un lote de `cantidad` causas, en base al
    promedio real de corridas anteriores del mismo campo. Sin
    historial suficiente (menos de 5 causas ya procesadas), devuelve
    confiable=False — la primera corrida sirve para calibrar.
    """
    _validar_campo_backfill(campo)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT total_procesados, total_segundos FROM worker_backfill_stats WHERE campo = %s",
                (campo,),
            )
            row = cur.fetchone()

    if not row or row["total_procesados"] < 5:
        return {
            "segundos_estimados": None,
            "confiable": False,
            "nota": "Sin historial suficiente todavía (menos de 5 causas procesadas) — "
                    "la primera corrida sirve para calibrar el estimado.",
        }

    promedio = float(row["total_segundos"]) / row["total_procesados"]
    return {
        "segundos_estimados": round(promedio * cantidad),
        "segundos_promedio_por_causa": round(promedio, 1),
        "confiable": True,
        "basado_en_causas": row["total_procesados"],
    }


def actualizar_abogados_causa(numero: str, abogado_actor: str = None, abogado_demandado: str = None) -> int:
    """
    Actualiza abogado_actor/abogado_demandado y sus flags de faltante en
    worker_causa_datos para una causa YA existente (backfill — no crea
    la fila si no existe, eso lo hace registrar_caso en el flujo normal).
    """
    sets, params = [], []
    if abogado_actor:
        sets += ["abogado_actor = %s", "faltante_abogados = false"]
        params += [abogado_actor]
    if abogado_demandado:
        sets += ["abogado_demandado = %s", "faltante_abogados = false"]
        params += [abogado_demandado]
    if not sets:
        return 0
    sets.append("actualizado_en = NOW()")
    params.append(numero)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE worker_causa_datos SET {', '.join(sets)} WHERE numero = %s",
                params,
            )
            updated = cur.rowcount
        conn.commit()
    return updated


def actualizar_flag_jurisprudencia(numero: str) -> int:
    """Marca faltante_jurisprudencia = false tras un backfill exitoso."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_causa_datos SET faltante_jurisprudencia = false, "
                "actualizado_en = NOW() WHERE numero = %s",
                (numero,),
            )
            updated = cur.rowcount
        conn.commit()
    return updated


def actualizar_duracion_todos(numero: str, fecha_inicio: str, fecha_sentencia: str, duracion_dias: int) -> int:
    """Actualiza la duración en worker_causas para este número de causa."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE worker_causas
                SET fecha_inicio_caso   = %s,
                    fecha_sentencia_caso = %s,
                    duracion_dias        = %s
                WHERE numero = %s
            """, (fecha_inicio or None, fecha_sentencia or None, duracion_dias, numero))
            updated = cur.rowcount
        conn.commit()
    return updated


def actualizar_duracion_caso(caso_id: int, fecha_inicio: str, fecha_sentencia: str, duracion_dias: int):
    """Actualiza la duración en worker_causas a partir del id de worker_caso_log."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT numero FROM worker_caso_log WHERE id = %s", (caso_id,))
            row = cur.fetchone()
            if not row:
                return
            numero = row[0]
            cur.execute("""
                UPDATE worker_causas
                SET fecha_inicio_caso   = %s,
                    fecha_sentencia_caso = %s,
                    duracion_dias        = %s
                WHERE numero = %s
            """, (fecha_inicio or None, fecha_sentencia or None, duracion_dias, numero))
        conn.commit()


def listar_casos_sin_duracion(job_id: Optional[int] = None, limit: int = 500) -> list[dict]:
    """
    Causas con sentencia que aún no tienen duracion_dias.
    Si job_id se pasa, filtra por causas procesadas en ese job.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if job_id:
                cur.execute("""
                    SELECT DISTINCT ON (cl.numero) cl.id, cl.numero, c.tribunal_codigo
                    FROM worker_caso_log cl
                    JOIN worker_causas c ON c.numero = cl.numero
                    WHERE cl.job_id = %s
                      AND cl.resultado LIKE '%%Con sentencia%%'
                      AND c.duracion_dias IS NULL
                    ORDER BY cl.numero, cl.id ASC
                    LIMIT %s
                """, (job_id, limit))
            else:
                cur.execute("""
                    SELECT NULL::INTEGER AS id, c.numero, c.tribunal_codigo
                    FROM worker_causas c
                    JOIN worker_causa_datos cd ON cd.numero = c.numero
                    WHERE cd.resultado LIKE '%%Con sentencia%%'
                      AND c.duracion_dias IS NULL
                    ORDER BY c.numero
                    LIMIT %s
                """, (limit,))
            return [dict(r) for r in cur.fetchall()]


# Columnas por las que se puede ordenar /casos — whitelist explícita porque
# el nombre de columna va directo al SQL (no se puede parametrizar como un
# valor normal); cualquier "sort" que no esté acá cae al default.
_CASOS_SORT_COLUMNAS = {
    "numero":         "c.numero",
    "tribunal":       "c.tribunal_codigo",
    "año":            "c.año",
    "estado":         "cd.estado_juicio",
    "resultado":      "cd.resultado",
    "cnj":            "cd.resultado_cnj",
    "actor":          "c.actor",
    "demandado":      "c.demandado",
    "cuantia":        "cd.cuantia",
    "decision_final": "cd.decision_final",
    "duracion_dias":  "c.duracion_dias",
    "fecha_ingreso":  "c.fecha_ingreso",
    "procesado_en":   "c.ultima_procesada",
}


def listar_casos_global(page: int = 1, per_page: int = 50,
                        tribunal: str = "", año: str = "", estado: str = "",
                        actor: str = "", demandado: str = "",
                        sort: str = "procesado_en", dir: str = "desc") -> dict:
    """Devuelve causas únicas con sus datos canónicos, con paginación, filtros y orden."""
    offset = (page - 1) * per_page
    wheres = []
    params: list = []
    if tribunal:
        wheres.append("c.tribunal_codigo = %s")
        params.append(tribunal)
    if año:
        wheres.append("c.año = %s")
        params.append(año)
    if estado:
        wheres.append("cd.estado_juicio = %s")
        params.append(estado)
    if actor:
        wheres.append("c.actor ILIKE %s")
        params.append(f"%{actor}%")
    if demandado:
        wheres.append("c.demandado ILIKE %s")
        params.append(f"%{demandado}%")
    where = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    columna_orden = _CASOS_SORT_COLUMNAS.get(sort, _CASOS_SORT_COLUMNAS["procesado_en"])
    direccion = "ASC" if str(dir).lower() == "asc" else "DESC"

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT COUNT(*) as total FROM worker_causas c "
                f"LEFT JOIN worker_causa_datos cd ON cd.numero = c.numero {where}",
                params
            )
            total = cur.fetchone()["total"]
            cur.execute(f"""
                SELECT c.*, cd.estado_juicio, cd.resultado, cd.resultado_cnj,
                       cd.completitud, cd.cuantia, cd.tipo_accion,
                       cd.objeto_controversia, cd.decision_final,
                       cd.resumen_ejecutivo, cd.categorizacion,
                       cd.url_pdf_tribunal, cd.url_docx_tribunal,
                       cd.url_pdf_cnj, cd.url_docx_cnj
                FROM worker_causas c
                LEFT JOIN worker_causa_datos cd ON cd.numero = c.numero
                {where}
                ORDER BY {columna_orden} {direccion} NULLS LAST, c.numero
                LIMIT %s OFFSET %s
            """, params + [per_page, offset])
            casos = [dict(r) for r in cur.fetchall()]
    return {"total": total, "page": page, "per_page": per_page, "casos": casos}


def stats_casos() -> dict:
    """Estadísticas agregadas de todas las causas únicas."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) as total FROM worker_causas")
            total = cur.fetchone()["total"]
            cur.execute("""
                SELECT estado_juicio, COUNT(*) as cnt
                FROM worker_causa_datos
                GROUP BY estado_juicio
                ORDER BY cnt DESC
            """)
            por_estado = {(r["estado_juicio"] or "Desconocido"): r["cnt"] for r in cur.fetchall()}
            cur.execute("""
                SELECT COUNT(*) as cnt FROM worker_causa_datos
                WHERE resultado LIKE '%Con sentencia%'
            """)
            con_sentencia = cur.fetchone()["cnt"]
            cur.execute("""
                SELECT DISTINCT tribunal_codigo, tribunal_nombre
                FROM worker_causas
                WHERE tribunal_codigo IS NOT NULL AND tribunal_codigo != ''
                ORDER BY tribunal_codigo
            """)
            tribunales = [{"codigo": r["tribunal_codigo"], "nombre": r["tribunal_nombre"]}
                          for r in cur.fetchall()]
            cur.execute("""
                SELECT DISTINCT año FROM worker_causas
                WHERE año IS NOT NULL AND año != ''
                ORDER BY año DESC
            """)
            años = [r["año"] for r in cur.fetchall()]
    return {
        "total":          total,
        "por_estado":     por_estado,
        "con_sentencia":  con_sentencia,
        "tribunales":     tribunales,
        "años":           años,
    }


def listar_casos_job(job_id: int, sin_sentencia: bool = False) -> list[dict]:
    """
    Log de causas de un job, enriquecido con datos de identidad e IA.
    sin_sentencia=True → solo causas sin sentencia (omitidas).
    """
    filtro = ""
    if sin_sentencia:
        filtro = "AND (cl.resultado IS NULL OR cl.resultado NOT LIKE '%Con sentencia%')"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""
                SELECT cl.id, cl.job_id, cl.numero, cl.tribunal_codigo,
                       cl.estado_juicio, cl.resultado, cl.resultado_cnj,
                       cl.completitud, cl.observaciones, cl.procesado_en,
                       c.tribunal_nombre, c.año, c.actor, c.demandado,
                       c.fecha_ingreso, c.duracion_dias,
                       c.fecha_inicio_caso, c.fecha_sentencia_caso,
                       cd.cuantia, cd.tipo_accion, cd.objeto_controversia,
                       cd.decision_final, cd.resumen_ejecutivo, cd.categorizacion,
                       cd.url_pdf_tribunal, cd.url_docx_tribunal,
                       cd.url_pdf_cnj, cd.url_docx_cnj
                FROM worker_caso_log cl
                LEFT JOIN worker_causas c      ON c.numero  = cl.numero
                LEFT JOIN worker_causa_datos cd ON cd.numero = cl.numero
                WHERE cl.job_id = %s {filtro}
                ORDER BY cl.id ASC
                LIMIT 2000
            """, (job_id,))
            return [dict(r) for r in cur.fetchall()]


# ── Subidas pendientes ────────────────────────────────────────────────────────

def guardar_pendiente(job_id: int, numero: str, datos: dict, error: str):
    """Persiste una extracción cuya subida a Strapi falló."""
    import json as _json
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_pending_uploads (job_id, numero, datos_json, error) "
                "VALUES (%s, %s, %s, %s)",
                (job_id, numero, _json.dumps(datos, default=str), error[:800])
            )
        conn.commit()


def listar_pendientes() -> list[dict]:
    """Devuelve subidas pendientes de reintento (resubido_en IS NULL)."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, job_id, numero, error, creado_en
                FROM worker_pending_uploads
                WHERE resubido_en IS NULL
                ORDER BY id ASC
                LIMIT 500
            """)
            return [dict(r) for r in cur.fetchall()]


def reintentar_pendientes() -> dict:
    """Reintenta subir a Strapi todas las entradas pendientes."""
    import json as _json
    from worker.strapi_client import StrapiClient
    strapi = StrapiClient()

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM worker_pending_uploads
                WHERE resubido_en IS NULL
                ORDER BY id ASC
                LIMIT 500
            """)
            pendientes = [dict(r) for r in cur.fetchall()]

    ok = fail = 0
    for p in pendientes:
        datos = p["datos_json"] if isinstance(p["datos_json"], dict) \
                else _json.loads(p["datos_json"])
        try:
            strapi.guardar_causa(datos)
            with _conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE worker_pending_uploads SET resubido_en = NOW() WHERE id = %s",
                        (p["id"],)
                    )
                    cur.execute(
                        "UPDATE worker_jobs "
                        "SET subidas_fallidas = GREATEST(0, subidas_fallidas - 1), "
                        "    exitosas = exitosas + 1, "
                        "    errores  = GREATEST(0, errores - 1) "
                        "WHERE id = %s",
                        (p["job_id"],)
                    )
                conn.commit()
            ok += 1
        except Exception as e:
            with _conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE worker_pending_uploads SET error = %s WHERE id = %s",
                        (str(e)[:800], p["id"])
                    )
                conn.commit()
            fail += 1

    return {"resubidas": ok, "fallidas": fail, "total": len(pendientes)}


def _emitir_sse(job_id: int, evento: dict):
    if evento.get("tipo") == "procesando":
        _procesando_actual[job_id] = evento
    elif evento.get("tipo") in ("progreso", "estado"):
        _procesando_actual.pop(job_id, None)  # limpia cuando termina una causa
    with _sse_lock:
        queues = _sse_queues.get(job_id, [])
    for q in queues:
        try:
            q.put_nowait(evento)
        except Exception:
            pass


def get_procesando_actual(job_id: int) -> Optional[dict]:
    return _procesando_actual.get(job_id)


def suscribir_sse(job_id: int, queue):
    with _sse_lock:
        _sse_queues.setdefault(job_id, []).append(queue)


def desuscribir_sse(job_id: int, queue):
    with _sse_lock:
        lst = _sse_queues.get(job_id, [])
        if queue in lst:
            lst.remove(queue)


# ── Control de jobs ───────────────────────────────────────────────────────────

def pausar_job(job_id: int) -> bool:
    with _lock:
        entry = _active.get(job_id)
    if not entry:
        return False
    entry["pause"].set()
    _actualizar(job_id, estado="pausado")
    _emitir_sse(job_id, {"tipo": "estado", "estado": "pausado"})
    return True


def reanudar_job(job_id: int) -> bool:
    job = obtener_job(job_id)
    if not job or job["estado"] not in ("pausado", "pendiente"):
        return False
    _actualizar(job_id, estado="corriendo")
    with _lock:
        entry = _active.get(job_id)
    if entry:
        entry["pause"].clear()
        _emitir_sse(job_id, {"tipo": "estado", "estado": "corriendo"})
        return True
    # Reanudar NO puede saltarse el tope de slots. Iba directo a _lanzar_hilo,
    # que no lo comprueba: reanudando a mano varios jobs pausados se arrancaban
    # todos. Medido: 10 reanudados con el tope en 4 -> 10 hilos vivos. Es como
    # civil llego a 18 corriendo el 23-10, machacando SATJE desde una sola IP.
    cat = job.get("categoria") or "extraccion"
    if _corriendo_por_categoria(cat) >= _max_cat(cat):
        # Queda en la cola; lo toma arrancar_pendientes() o el final de otro job.
        _actualizar(job_id, estado="pendiente")
        return True
    _lanzar_hilo(job_id, cat)
    return True


def matar_job(job_id: int) -> bool:
    with _lock:
        entry = _active.get(job_id)
    if not entry:
        _actualizar(job_id, estado="cancelado", completado_en=_now())
        return True
    entry["kill"].set()
    entry["pause"].clear()
    _actualizar(job_id, estado="cancelado", completado_en=_now())
    _emitir_sse(job_id, {"tipo": "estado", "estado": "cancelado"})
    return True


# ── Pool de concurrencia ──────────────────────────────────────────────────────

def _ocupa_slot(e: dict) -> bool:
    """Un job registrado ocupa su slot desde que se registra, no desde que su
    hilo arranca: entre el registro y t.start() hay una escritura a la base, y
    en ese hueco is_alive() da False."""
    t = e["thread"]
    return t.ident is None or t.is_alive()


def _corriendo_por_categoria(categoria: str) -> int:
    """Cuenta jobs activos de una categoría específica."""
    with _lock:
        return sum(
            1 for e in _active.values()
            if _ocupa_slot(e) and e.get("categoria") == categoria
        )


def _intentar_arrancar(job_id: int):
    """Arranca el job si hay slot libre en su categoría; si no, queda en 'pendiente'."""
    job = obtener_job(job_id)
    if not job:
        return
    cat = job.get("categoria", "extraccion")
    if _corriendo_por_categoria(cat) < _max_cat(cat):
        _lanzar_hilo(job_id, cat)


def _lanzar_hilo(job_id: int, categoria: str = "extraccion"):
    from worker.runner import ejecutar_job

    pause_ev = threading.Event()
    kill_ev  = threading.Event()

    def _run():
        try:
            ejecutar_job(job_id, pause_ev, kill_ev)
        except BaseException as e:
            # Sin esto, un job que revienta deja la fila en 'corriendo' para
            # siempre, con 0 procesadas y sin una sola linea de log: el
            # traceback se va por stderr y nadie lo ve. Paso justo asi con la
            # revision del 2026-10-23 (jobs 278-281) y con los briefs 259/268/
            # 277, que quedaron dias en 'corriendo' sin haber hecho nada.
            # Se captura BaseException a proposito: un MemoryError o un
            # RuntimeError de 'can't start new thread' son candidatos reales
            # aca y son los que peor se diagnostican.
            import traceback as _tb
            detalle = _tb.format_exc()
            try:
                from worker import logger as _log
                _log.error(f"Job abortado — {type(e).__name__}: {e}\n{detalle}",
                           job_id=job_id)
            except Exception:
                pass
            try:
                _actualizar(job_id, estado="error", completado_en=_now())
            except Exception:
                pass
            try:
                from worker import notificador
                notificador.avisar_en_hilo(
                    f"job {job_id} abortado — {type(e).__name__}",
                    f"El job {job_id} murio al arrancar y no proceso nada.\n\n"
                    f"{type(e).__name__}: {e}\n\n{detalle}",
                    clave=f"abortado:{job_id}")
            except Exception:
                pass
            raise
        finally:
            with _lock:
                _active.pop(job_id, None)
            _iniciar_siguiente_pendiente()

    t = threading.Thread(target=_run, daemon=True, name=f"job-{job_id}-{categoria}")
    with _lock:
        # Un job con hilo vivo no se relanza: al arrancar, recuperar_jobs_interrumpidos
        # devolvía a 'pendiente' los jobs recién creados por ACCIONES_ARRANQUE y corrían dos veces.
        previo = _active.get(job_id)
        if previo and _ocupa_slot(previo):
            return
        # El cupo se comprueba y se toma en el mismo bloque con el lock. Antes
        # lo comprobaba quien llamaba (arrancar_pendientes, el final de cada job,
        # la cola automática) y lanzaban a la vez: el 26-oct, al reiniciar con
        # 1.117 pendientes, civil quedó con 72 jobs corriendo y tope de 10.
        ocupados = sum(1 for jid, e in _active.items()
                       if jid != job_id and e.get("categoria") == categoria and _ocupa_slot(e))
        if ocupados >= _max_cat(categoria):
            return
        _active[job_id] = {
            "thread":    t,
            "pause":     pause_ev,
            "kill":      kill_ev,
            "categoria": categoria,
        }

    _actualizar(job_id, estado="corriendo", iniciado_en=_now())
    t.start()


def _iniciar_siguiente_pendiente():
    """Cuando un job termina, intenta arrancar el siguiente pendiente por categoría."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, categoria FROM worker_jobs
                WHERE estado = 'pendiente'
                ORDER BY id ASC
                LIMIT 20
            """)
            pendientes = [dict(r) for r in cur.fetchall()]
    for p in pendientes:
        cat = p.get("categoria", "extraccion")
        if _corriendo_por_categoria(cat) < _max_cat(cat):
            _lanzar_hilo(p["id"], cat)
            break


# Umbrales de inactividad de un job con hilo VIVO. El reloj es `latido`, que
# se sella en cada UNIDAD de trabajo (_actualizar en los workers de revision,
# brief, embeddings y lista; reportar_progreso en la extraccion), no cuando se
# sube algo: un cron que revisa 2.000 numeros y no guarda ninguno late igual.
#
# Los numeros salen del backoff de SATJE en runner.py, SATJE_BACKOFF_MIN =
# (2, 5, 10, 20, 30): un job puede estar legitimamente quieto hasta 30 min, y
# la escalera entera suma 67. Por eso avisar a los 45 y matar a los 90, no a
# los 15 — a los 15 se mataria a un job que esta esperando como debe.
JOB_AVISO_MIN   = int(os.environ.get("JOB_AVISO_MIN", "45"))
JOB_TIMEOUT_MIN = int(os.environ.get("JOB_TIMEOUT_MIN", "90"))


def vigilar_jobs_estancados() -> dict:
    """Vigila jobs que siguen VIVOS pero dejaron de avanzar.

    OJO: los jobs cuyo hilo ya NO existe no se tocan aca — de esos se encarga
    rescatar_huerfanos(), que los devuelve a 'pendiente' para retomarlos desde
    su checkpoint. Marcarlos desde aca perderia ese avance.

    El hueco que cubre esto es el contrario: un hilo vivo, que por eso
    rescatar_huerfanos da por sano, pero que lleva horas sin procesar nada y
    mientras tanto ocupa un slot. Estado real de civil el 23-10: 18 jobs
    atascados reintentando SATJE, 0 avances en 90 s, 21,7 h corriendo, con
    1.524 jobs esperando detras.

    Pasado JOB_TIMEOUT_MIN se le pide al job que termine por la via normal
    (kill_ev, el mismo que usa matar_job) y se marca 'error'. No se borra la
    fila: el historial del job se conserva.
    """
    avisados, matados = [], []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, EXTRACT(epoch FROM (NOW() - COALESCE(latido, iniciado_en)))/60.0
                  FROM worker_jobs
                 WHERE estado = 'corriendo'
            """)
            filas = cur.fetchall()

    from worker import logger as log
    for job_id, quieto in filas:
        quieto = quieto or 0
        with _lock:
            entrada = _active.get(job_id)
        if not (entrada and entrada["thread"].is_alive()):
            continue                      # territorio de rescatar_huerfanos
        if quieto >= JOB_TIMEOUT_MIN:
            entrada["kill"].set()
            entrada["pause"].clear()
            _actualizar(job_id, estado="error", completado_en=_now())
            log.error(f"Job sin avanzar hace {round(quieto)} min (tope "
                      f"{JOB_TIMEOUT_MIN}). Se le pide terminar y se libera el "
                      f"slot. El hilo sale en cuanto despierte de su pausa.",
                      job_id=job_id)
            matados.append((job_id, round(quieto)))
        elif quieto >= JOB_AVISO_MIN:
            log.warn(f"Job sin avanzar hace {round(quieto)} min. Se mata a los "
                     f"{JOB_TIMEOUT_MIN}.", job_id=job_id)
            avisados.append(job_id)

    # Un solo correo por ronda con todos los matados. Con 18 jobs trabados a la
    # vez —lo que paso en civil— mandar uno por job seria 18 correos inutiles.
    if matados:
        try:
            from worker import notificador
            detalle = "\n".join(f"  job {j}: {m} min sin avanzar" for j, m in matados)
            notificador.avisar_en_hilo(
                f"{len(matados)} job(s) matados por inactividad",
                f"Llevaban mas de {JOB_TIMEOUT_MIN} min sin procesar nada y "
                f"ocupando un slot. Se les pidio terminar y quedaron en "
                f"'error'.\n\n{detalle}\n\n"
                f"Si se repite, mirar si SATJE esta respondiendo.",
                clave="matados:" + ",".join(str(j) for j, _ in matados))
        except Exception:
            pass
    return {"avisados": avisados, "matados": [j for j, _ in matados]}


def recuperar_jobs_interrumpidos():
    """
    Al arrancar el servidor, detecta jobs que quedaron en 'corriendo'
    (por un deploy/crash) y los pasa a 'pausado' para que el usuario
    los reanude o el sistema los retome automáticamente.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            # Los interrumpidos por el reinicio vuelven a la cola ('pendiente')
            # y se retoman solos desde su checkpoint (ver arrancar_pendientes).
            # Una pausa pedida por el usuario sigue siendo 'pausado' y no se toca.
            cur.execute("""
                UPDATE worker_jobs
                SET estado = 'pendiente'
                WHERE estado = 'corriendo'
            """)
        conn.commit()
    arrancar_pendientes()


def crear_jobs_arranque(spec: Optional[str] = None) -> list:
    """Encola jobs de descarga (solo_descarga) declarados en la variable de
    entorno JOBS_ARRANQUE, para arrancar un corpus sin llamar a la API.
    Formato JSON: {"tribunales": ["09332", "17230"], "desde": 2016, "hasta": 2026}
    Crea un job por (tribunal, año), del año más reciente al más antiguo, y
    se salta los pares que ya tienen un job de descarga no cancelado. Los jobs
    quedan 'pendiente' y los arranca arrancar_pendientes()."""
    import json as _json
    spec = spec if spec is not None else os.environ.get("JOBS_ARRANQUE", "")
    if not spec.strip():
        return []
    cfg = _json.loads(spec)
    return _encolar_barridos(_tribunales_de_spec(cfg.get("tribunales", [])),
                             int(cfg.get("desde", 2016)),
                             int(cfg.get("hasta", datetime.now().year)),
                             "jobs-arranque")


def _tribunales_de_spec(valor) -> list:
    """Lista de prefijos: explícita, o "especializadas" (las unidades propias de
    la materia, ver MATERIA.COLA_ESPECIALIZADAS) o "todas" (las JUDICATURAS)."""
    if isinstance(valor, str):
        if valor == "todas":
            return sorted(MATERIA.JUDICATURAS)
        if valor == "especializadas":
            claves = tuple(c.upper() for c in getattr(MATERIA, "COLA_ESPECIALIZADAS", ()))
            excluir = tuple(c.upper() for c in getattr(MATERIA, "COLA_EXCLUIR", ()))
            return sorted(k for k, v in MATERIA.JUDICATURAS.items()
                          if any(c in v.upper() for c in claves)
                          and not any(x in v.upper() for x in excluir))
        return []
    return [str(t).zfill(5) for t in valor]


def _pares_con_job_descarga() -> set:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT tribunal, año FROM worker_jobs
                            WHERE solo_descarga AND estado <> 'cancelado'""")
            return {(t, a) for t, a in cur.fetchall()}


def _encolar_barridos(tribunales: list, desde: int, hasta: int, origen: str) -> list:
    """Un job solo_descarga (barrido completo de la numeración) por cada
    (tribunal, año) que aún no tenga job de descarga. Del año más reciente al
    más antiguo. Quedan 'pendiente'."""
    ya = _pares_con_job_descarga()
    creados = []
    for anio in range(hasta, desde - 1, -1):
        for t in tribunales:
            if (t, str(anio)) in ya:
                continue
            job = crear_job(tribunal=t, año=str(anio), desde=1, hasta=None, dry_run=False,
                            modelo="gpt-4o", iniciado_por=origen,
                            solo_descarga=True, arrancar=False)
            creados.append(job["id"])
    return creados


def _encolar_desde_indice(origen: str = "cola-automatica") -> list:
    """Un job solo_descarga + desde_indice por cada (prefijo, año) que el índice
    rápido ya terminó, que tenga causas de la materia y que aún no tenga job de
    descarga (ni barrido completo ni desde índice)."""
    from worker.indice_worker import resumen
    from config.config import TRIBUNALES
    pares = resumen(sorted(MATERIA.IDS_MATERIA_SATJE))["pares_materia"]
    ya = _pares_con_job_descarga()
    creados = []
    for p in pares:
        pf, a = p["prefijo"], str(p["anio"])
        if not p["terminado"] or pf not in TRIBUNALES or (pf, a) in ya:
            continue
        job = crear_job(tribunal=pf, año=a, desde=1, hasta=None, dry_run=False,
                        modelo="gpt-4o", iniciado_por=origen, workers="desde_indice",
                        solo_descarga=True, arrancar=False)
        creados.append(job["id"])
    return creados


# ── Cola automática ──────────────────────────────────────────────────────────
# COLA_AUTOMATICA (JSON) mantiene la descarga siempre alimentada sin llamar a
# la API: cada COLA_INTERVALO_SEG encola los barridos que falten de las
# unidades indicadas, encola desde el índice los pares que ya terminó, rescata
# jobs huérfanos y llena los slots libres.
#   {"tribunales": "especializadas", "desde": 2016, "desde_indice": true}

_huerfanos_vistos: dict = {}


def rescatar_huerfanos() -> list:
    """Jobs en 'corriendo' sin hilo vivo en este proceso y sin avanzar entre dos
    revisiones (p. ej. arrancados por un proceso que se apagó en pleno deploy):
    vuelven a 'pendiente' para que se retomen desde su checkpoint."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, checkpoint FROM worker_jobs WHERE estado = 'corriendo'")
            filas = cur.fetchall()
    with _lock:
        vivos = {jid for jid, e in _active.items() if e["thread"].is_alive()}
    rescatar, vistos = [], set()
    for jid, cp in filas:
        if jid in vivos:
            continue
        vistos.add(jid)
        if jid in _huerfanos_vistos and _huerfanos_vistos[jid] == cp:
            rescatar.append(jid)
        else:
            _huerfanos_vistos[jid] = cp
    for jid in list(_huerfanos_vistos):
        if jid not in vistos or jid in rescatar:
            _huerfanos_vistos.pop(jid, None)
    if rescatar:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE worker_jobs SET estado = 'pendiente'
                                WHERE id = ANY(%s) AND estado = 'corriendo'""", (rescatar,))
            conn.commit()
    return rescatar


def _rebarrer_una_vez(r: dict) -> list:
    """Vuelve a barrer, una sola vez por "id", los (tribunal, año) indicados
    desde el número 1 hasta donde llegó su barrido anterior (p. ej. tras
    corregir la descarga). El resto lo sigue cubriendo el job original.
      {"id": "texto-satje", "tribunales": ["09359"], "años": [2016, 2017]}"""
    marca = f"rebarrido:{r['id']}"
    creados = []
    with _conn() as conn:
        with conn.cursor() as cur:
            for t in _tribunales_de_spec(r.get("tribunales", [])):
                for a in [str(x) for x in r.get("años", [])]:
                    cur.execute("""SELECT count(*) FILTER (WHERE iniciado_por = %s),
                                          max(checkpoint) FILTER (WHERE coalesce(workers,'') = '')
                                     FROM worker_jobs
                                    WHERE solo_descarga AND tribunal = %s AND año = %s
                                      AND estado <> 'cancelado'""", (marca, t, a))
                    hechos, hasta = cur.fetchone()
                    if hechos or not hasta:
                        continue
                    creados.append((t, a, int(hasta)))
    ids = []
    for t, a, hasta in creados:
        job = crear_job(tribunal=t, año=a, desde=1, hasta=hasta, dry_run=False,
                        modelo="gpt-4o", iniciado_por=marca,
                        solo_descarga=True, arrancar=False)
        ids.append(job["id"])
    return ids


def _asegurar_indice(cfg: dict, excluir: list) -> list:
    """Crea el job del índice rápido si no existe ninguno (no cancelado), sobre
    todas las judicaturas menos las que ya se barren completas."""
    import json as _json
    from config.config import TRIBUNALES
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM worker_jobs
                            WHERE workers = 'indice' AND estado <> 'cancelado'""")
            if cur.fetchone()[0]:
                return []
    prefijos = [p for p in sorted(TRIBUNALES) if p not in set(excluir)]
    años = [str(a) for a in range(int(cfg.get("desde", 2016)),
                                  int(cfg.get("hasta", datetime.now().year)) + 1)]
    job = crear_job(tribunal="-", año="-", desde=1, hasta=len(prefijos) * len(años),
                    dry_run=False, modelo="-", iniciado_por="cola-automatica",
                    workers="indice", categoria="indice", arrancar=False,
                    parametros=_json.dumps({"prefijos": prefijos, "años": años,
                                            "hilos": int(cfg.get("hilos_indice", 8)),
                                            "pausa": 0.3, "max_vacias": 30}))
    return [job["id"]]


def alimentar_cola(spec: Optional[str] = None) -> dict:
    import json as _json
    spec = spec if spec is not None else os.environ.get("COLA_AUTOMATICA", "")
    out = {"barridos": [], "rebarridos": [], "indice": [], "desde_indice": [], "rescatados": []}
    out["rescatados"] = rescatar_huerfanos()
    if spec.strip():
        cfg = _json.loads(spec)
        tribunales = _tribunales_de_spec(cfg.get("tribunales", []))
        if tribunales:
            out["barridos"] = _encolar_barridos(
                tribunales, int(cfg.get("desde", 2016)),
                int(cfg.get("hasta", datetime.now().year)), "cola-automatica")
        for r in cfg.get("rebarrer", []):
            out["rebarridos"] += _rebarrer_una_vez(r)
        if cfg.get("indice"):
            out["indice"] = _asegurar_indice(cfg, tribunales)
        if cfg.get("desde_indice"):
            out["desde_indice"] = _encolar_desde_indice()
    arrancar_pendientes()
    return out


def iniciar_cola_automatica():
    """Hilo que llama a alimentar_cola() cada COLA_INTERVALO_SEG (600 s)."""
    from worker import logger as wlog
    intervalo = int(os.environ.get("COLA_INTERVALO_SEG", "600"))

    def _bucle():
        import time as _t
        _t.sleep(60)
        while True:
            try:
                r = alimentar_cola()
                if any(r.values()):
                    wlog.sistema("INFO", f"Cola automática: {len(r['barridos'])} barridos, "
                                         f"rebarridos {r['rebarridos']}, "
                                         f"índice {r['indice']}, "
                                         f"{len(r['desde_indice'])} desde índice, "
                                         f"rescatados {r['rescatados']}")
            except Exception as e:
                wlog.sistema("ERROR", f"Cola automática: {e}")
            _t.sleep(intervalo)

    threading.Thread(target=_bucle, daemon=True, name="cola-automatica").start()


def acciones_arranque(spec: Optional[str] = None) -> list:
    """Acciones únicas declaradas en la variable de entorno ACCIONES_ARRANQUE,
    para lanzar trabajos sin llamar a la API. Cada acción lleva un "id" y se
    ejecuta una sola vez (queda marcada en iniciado_por="arranque:<id>").
    Formato JSON:
      {"reextraer": [{"id": "piloto-1", "numeros": ["09359-2024-00556"],
                      "niveles": ["tribunal", "apelacion", "corte"],
                      "modelo": "gpt-4o"}]}
    Devuelve los ids de los jobs creados."""
    import json as _json
    spec = spec if spec is not None else os.environ.get("ACCIONES_ARRANQUE", "")
    if not spec.strip():
        return []
    cfg = _json.loads(spec)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT iniciado_por FROM worker_jobs WHERE iniciado_por LIKE 'arranque:%%'")
            hechas = {r[0] for r in cur.fetchall()}
    creados = []
    for acc in cfg.get("reextraer", []):
        marca = f"arranque:{acc['id']}"
        if marca in hechas:
            continue
        niveles = [n for n in acc.get("niveles", ["tribunal", "apelacion", "corte"])
                   if n in ("tribunal", "corte", "apelacion")]
        numeros = [str(n).strip() for n in acc.get("numeros", []) if str(n).strip()]
        job = crear_job_reextraccion(
            numeros, modelo=acc.get("modelo", "gpt-4o"), niveles=niveles,
            tribunal=acc.get("tribunal", "-"), anio=acc.get("anio", "-"),
            completitud=acc.get("completitud"), limite=acc.get("limite"),
            iniciado_por=marca, flujo_proveedores=acc.get("flujo_proveedores", "alibaba"),
        )
        creados.append(job["id"])
    # {"reintentar_errores_red": [{"id": "satje-21oct", "desde": "2026-10-21T00:00:00Z"}]}
    for acc in cfg.get("reintentar_errores_red", []):
        marca = f"arranque:{acc['id']}"
        if marca not in hechas:
            creados += _reintentar_errores_red(acc["desde"], marca)
    return creados


def _numeros_a_recuperar(filas) -> list:
    """Números cuyo último registro (filas en orden cronológico: numero,
    estado_juicio, observaciones) es un error de red o de SATJE."""
    ultimo = {}
    for numero, estado, obs in filas:
        ultimo[numero] = (estado == "Error de procesamiento"
                          and (obs or "").startswith(("[SATJE_ERROR]", "[RED_TIMEOUT]")))
    return sorted(n for n, caido in ultimo.items() if caido)


def _reintentar_errores_red(desde: str, marca: str) -> list:
    """Vuelve a descargar (solo_descarga) las causas que quedaron con error de
    red/SATJE desde "desde" y no se procesaron bien después: un job por
    (tribunal, año) que recorre solo esos números (workers="reintento")."""
    import json as _json
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT numero, estado_juicio, observaciones FROM worker_caso_log
                            WHERE procesado_en >= %s AND numero IN (
                                  SELECT numero FROM worker_caso_log
                                   WHERE procesado_en >= %s
                                     AND estado_juicio = 'Error de procesamiento'
                                     AND (observaciones LIKE '[SATJE_ERROR]%%'
                                          OR observaciones LIKE '[RED_TIMEOUT]%%'))
                            ORDER BY procesado_en, id""", (desde, desde))
            filas = cur.fetchall()
    return crear_jobs_descarga_numeros(_numeros_a_recuperar(filas), marca)


def crear_jobs_descarga_numeros(numeros: list, marca: str) -> list:
    """Jobs solo_descarga (sin IA) que bajan exactamente esos números, uno por
    (tribunal, año). Lo usan el reintento de errores de red y la revisión
    diaria en modo descarga. Devuelve los ids creados."""
    import json as _json
    grupos: dict = {}
    for numero in numeros:
        partes = numero.split("-")
        if len(partes) == 3 and partes[2].isdigit():
            grupos.setdefault((partes[0], partes[1]), []).append(int(partes[2]))
    ids = []
    for (t, a), secs in sorted(grupos.items()):
        job = crear_job(tribunal=t, año=a, desde=min(secs), hasta=max(secs), dry_run=False,
                        modelo="gpt-4o", iniciado_por=marca, workers="reintento",
                        solo_descarga=True, parametros=_json.dumps({"numeros": secs}))
        ids.append(job["id"])
    return ids


def arrancar_pendientes():
    """Llena los slots libres de cada categoría con los jobs 'pendiente'
    (en orden de id). Sin esto, tras un reinicio solo arrancaba un job cuando
    terminaba otro."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT id, categoria FROM worker_jobs
                            WHERE estado = 'pendiente' ORDER BY id ASC""")
            pendientes = [dict(r) for r in cur.fetchall()]
    for p in pendientes:
        cat = p.get("categoria") or "extraccion"
        if _corriendo_por_categoria(cat) < _max_cat(cat):
            _lanzar_hilo(p["id"], cat)


def _now():
    return datetime.now(timezone.utc)


# ── Logs persistentes ────────────────────────────────────────────────────────

def insertar_log(job_id: Optional[int], nivel: str, causa: Optional[str],
                 mensaje: str, ts: Optional[datetime] = None) -> None:
    """Persiste una entrada de log en worker_job_logs (ver LOG_LEVEL_DB en worker/logger.py)."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO worker_job_logs (job_id, nivel, causa, mensaje, ts) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (job_id, nivel, causa, mensaje[:4000], ts or _now())
                )
            conn.commit()
    except Exception:
        pass  # log DB errors never interrupt the extraction flow


def listar_logs(job_id: Optional[int], nivel: Optional[str] = None,
                limit: int = 100, offset: int = 0) -> list[dict]:
    """
    Devuelve logs persistidos de un job, más recientes primero, paginados
    con limit/offset — para poder auditar el historial completo de un job
    desde la webapp por páginas sin traerlo todo de una vez.
    """
    wheres = []
    params: list = []
    if job_id is not None:
        wheres.append("job_id = %s")
        params.append(job_id)
    if nivel:
        wheres.append("nivel = %s")
        params.append(nivel.upper())
    where = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM worker_job_logs {where} ORDER BY ts DESC LIMIT %s OFFSET %s",
                params + [limit, offset]
            )
            return [dict(r) for r in cur.fetchall()]


# ── Prompts versionados ────────────────────────────────────────────────────

# nombre lógico -> ruta relativa dentro de prompts/. Mismo set que carga
# resolucion/ai/_cliente.py::cargar_prompt(). catalogo.txt queda afuera a
# propósito: no es un prompt de instrucción, es un catálogo que se parsea en
# una estructura en memoria al importar el módulo — editarlo en vivo
# requeriría reiniciar el worker igual, así que por ahora se sigue editando
# solo por archivo.
PROMPTS_EDITABLES = [
    "sistema.txt",
    "sistema_neutro.txt",
    "prompt_1_resumen.txt",
    "prompt_2_extraccion.txt",
    "prompt_3_jurisprudencia.txt",
    "prompt_4_clasificacion.txt",
    "prompt_5_cnj_extraccion.txt",
    "prompt_6_cnj_jurisprudencia.txt",
    "prompt_7_tipos_defecto.txt",
]


def bootstrap_prompts_desde_archivos(prompts_dir) -> int:
    """
    Siembra worker_prompts con el contenido actual de los archivos .txt,
    version 1 activa, para cada nombre en PROMPTS_EDITABLES que todavía no
    tenga ninguna fila. Idempotente — no pisa nada si ya hay versiones
    guardadas. Devuelve cuántos prompts sembró.
    """
    from pathlib import Path
    sembrados = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            # PROMPTS_DESDE_ARCHIVO=1: los archivos del repo son la fuente de
            # verdad (fase de afinado de una materia nueva). Si el archivo
            # difiere de la versión activa, se guarda como versión nueva y se
            # activa; las anteriores quedan en el historial.
            sincronizar = os.environ.get("PROMPTS_DESDE_ARCHIVO", "") in ("1", "true", "si", "sí")
            for nombre in PROMPTS_EDITABLES:
                cur.execute("SELECT 1 FROM worker_prompts WHERE nombre = %s LIMIT 1", (nombre,))
                if cur.fetchone():
                    ruta = Path(prompts_dir) / nombre
                    if sincronizar and ruta.exists():
                        contenido = ruta.read_text(encoding="utf-8").rstrip("\n") + "\n"
                        cur.execute("""SELECT contenido FROM worker_prompts
                                        WHERE nombre=%s AND activo LIMIT 1""", (nombre,))
                        act = cur.fetchone()
                        if not act or act[0] != contenido:
                            cur.execute("SELECT coalesce(max(version),0)+1 FROM worker_prompts WHERE nombre=%s",
                                        (nombre,))
                            ver = cur.fetchone()[0]
                            cur.execute("UPDATE worker_prompts SET activo=FALSE WHERE nombre=%s", (nombre,))
                            cur.execute(
                                "INSERT INTO worker_prompts (nombre, version, contenido, activo, nota) "
                                "VALUES (%s, %s, %s, TRUE, %s)",
                                (nombre, ver, contenido, "Sincronizada desde archivo (PROMPTS_DESDE_ARCHIVO)"),
                            )
                            sembrados += 1
                    continue
                ruta = Path(prompts_dir) / nombre
                if not ruta.exists():
                    continue
                contenido = ruta.read_text(encoding="utf-8").rstrip("\n") + "\n"
                cur.execute(
                    "INSERT INTO worker_prompts (nombre, version, contenido, activo, nota) "
                    "VALUES (%s, 1, %s, TRUE, %s)",
                    (nombre, contenido, "Versión inicial (sembrada desde archivo)"),
                )
                sembrados += 1
        conn.commit()
    return sembrados


def obtener_prompt_activo(nombre: str) -> Optional[str]:
    """Contenido de la versión activa de un prompt, o None si no está en la BD todavía."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT contenido FROM worker_prompts WHERE nombre = %s AND activo = TRUE",
                (nombre,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def listar_prompts() -> list[dict]:
    """Un renglón por nombre, con los datos de su versión activa."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT nombre, version, contenido, nota, creado_por, creado_en
                FROM worker_prompts
                WHERE activo = TRUE
                ORDER BY nombre
            """)
            return [dict(r) for r in cur.fetchall()]


def listar_versiones_prompt(nombre: str) -> list[dict]:
    """Todas las versiones de un prompt, más reciente primero."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, nombre, version, contenido, activo, nota, creado_por, creado_en
                FROM worker_prompts
                WHERE nombre = %s
                ORDER BY version DESC
            """, (nombre,))
            return [dict(r) for r in cur.fetchall()]


def guardar_prompt(nombre: str, contenido: str, modo: str,
                   nota: Optional[str] = None, creado_por: Optional[str] = None) -> dict:
    """
    Guarda un prompt editado.
    modo="sobrescribir" -> UPDATE in-place sobre la versión activa actual
      (no crea versión nueva; se pierde el contenido previo de esa versión).
    modo="nueva_version" -> INSERT con version = max(version)+1 para ese
      nombre, y esa pasa a ser la activa (la anterior deja de estarlo).
    Devuelve la fila resultante.
    """
    if modo not in ("sobrescribir", "nueva_version"):
        raise ValueError(f"modo inválido: {modo!r} (usar 'sobrescribir' o 'nueva_version')")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if modo == "sobrescribir":
                cur.execute("""
                    UPDATE worker_prompts
                    SET contenido = %s, nota = %s, creado_por = %s, creado_en = NOW()
                    WHERE nombre = %s AND activo = TRUE
                    RETURNING id, nombre, version, contenido, activo, nota, creado_por, creado_en
                """, (contenido, nota, creado_por, nombre))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"No hay versión activa de '{nombre}' para sobrescribir")
            else:
                cur.execute(
                    "SELECT COALESCE(MAX(version), 0) AS siguiente FROM worker_prompts WHERE nombre = %s",
                    (nombre,),
                )
                siguiente = cur.fetchone()["siguiente"] + 1
                cur.execute(
                    "UPDATE worker_prompts SET activo = FALSE WHERE nombre = %s AND activo = TRUE",
                    (nombre,),
                )
                cur.execute("""
                    INSERT INTO worker_prompts (nombre, version, contenido, activo, nota, creado_por)
                    VALUES (%s, %s, %s, TRUE, %s, %s)
                    RETURNING id, nombre, version, contenido, activo, nota, creado_por, creado_en
                """, (nombre, siguiente, contenido, nota, creado_por))
                row = cur.fetchone()
        conn.commit()
    return dict(row)


def activar_version_prompt(nombre: str, version: int) -> bool:
    """Rollback: reactiva una versión anterior (deja de ser activa la actual)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_prompts SET activo = FALSE WHERE nombre = %s AND activo = TRUE",
                (nombre,),
            )
            cur.execute(
                "UPDATE worker_prompts SET activo = TRUE WHERE nombre = %s AND version = %s",
                (nombre, version),
            )
            activada = cur.rowcount > 0
        conn.commit()
    return activada


_CODIGOS_TIPOS_DEFECTO_VALIDOS = set(MATERIA.CODIGOS_TIPOS_DEFECTO)


def _parsear_tipos_defecto(texto: str) -> list[tuple[str, Optional[str]]]:
    """
    Parsea la salida cruda del prompt 7 (una linea por codigo, mas una linea
    opcional "OBSERVACION: ...") en pares (codigo, observacion). Ignora
    lineas que no matcheen un codigo valido del catalogo — más estricto que
    confiar ciegamente en el formato del modelo.
    """
    pares = []
    observacion = None
    for linea in (texto or "").splitlines():
        linea = linea.strip().strip("-•* ").strip()
        if not linea:
            continue
        if linea.upper().startswith("OBSERVACION"):
            observacion = linea.split(":", 1)[1].strip() if ":" in linea else linea
            continue
        codigo = linea.upper().split()[0].rstrip(":")
        if codigo in _CODIGOS_TIPOS_DEFECTO_VALIDOS:
            pares.append(codigo)
    return [(c, observacion if c == "COACTIVA_OTRO_NUMERAL" else None) for c in pares]


def guardar_tipos_defecto(document_id: str, texto_clasificacion: str) -> int:
    """
    Guarda la clasificacion interna de tipo(s) de vicio/defecto para una
    causa (prompt 7), reemplazando cualquier clasificacion previa de ese
    document_id (idempotente ante reprocesamiento). Devuelve cuantos codigos
    se guardaron.
    """
    pares = _parsear_tipos_defecto(texto_clasificacion)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM causa_tipos_defecto WHERE document_id = %s", (document_id,))
            for codigo, observacion in pares:
                cur.execute(
                    """INSERT INTO causa_tipos_defecto (document_id, codigo, observacion)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (document_id, codigo) DO NOTHING""",
                    (document_id, codigo, observacion),
                )
        conn.commit()
    return len(pares)


# ── Prompt 8: tipo de prueba por lado ────────────────────────────────────────

_CODIGOS_TIPO_PRUEBA_VALIDOS = set(MATERIA.CODIGOS_TIPO_PRUEBA)

# Variantes que el modelo inventa ocasionalmente -> codigo canonico. Medido
# sobre 40 clasificaciones reales: ~2.5% de las causas traian un codigo fuera
# del catalogo, casi siempre un sinonimo obvio.
_ALIAS_TIPO_PRUEBA = dict(MATERIA.ALIAS_TIPO_PRUEBA)


def _parsear_tipos_prueba(texto: str) -> dict:
    """
    Parsea la salida cruda del prompt 8:
        ACTOR: COD1, COD2
        DEMANDADO: COD3
        POR_QUE: <cita textual | NO_DETERMINABLE>
        EVIDENCIA_AUSENTE: <cita textual | NINGUNA>

    Devuelve {"actor": set, "demandado": set, "por_que": str|None,
              "evidencia_ausente": str|None}. Descarta codigos fuera del
      catalogo (tras aplicar alias) en vez de confiar en el formato del modelo.
    """
    out = {"actor": set(), "demandado": set(), "por_que": None, "evidencia_ausente": None}

    def _codigos(valor: str) -> set:
        limpios = set()
        for c in valor.split(","):
            c = c.strip().upper().strip(".")
            c = _ALIAS_TIPO_PRUEBA.get(c, c)
            if c in _CODIGOS_TIPO_PRUEBA_VALIDOS:
                limpios.add(c)
        return limpios

    for linea in (texto or "").splitlines():
        s = linea.strip().strip("-•* ").strip()
        if ":" not in s:
            continue
        etiqueta, valor = s.split(":", 1)
        etiqueta, valor = etiqueta.strip().upper(), valor.strip()
        if etiqueta == "ACTOR":
            out["actor"] = _codigos(valor)
        elif etiqueta == "DEMANDADO":
            out["demandado"] = _codigos(valor)
        elif etiqueta == "POR_QUE":
            out["por_que"] = None if (not valor or valor.upper().startswith("NO_DETERMINABLE")) else valor
        elif etiqueta == "EVIDENCIA_AUSENTE":
            # Se parsea pero NO se persiste: validado contra clasificacion
            # manual, este campo da ~40% de precision (confunde "lo que SI se
            # presento" y boilerplate generico con "la parte no presento X").
            # Un campo asi es peor que no tenerlo — el LLM de estrategia le
            # citaria basura al abogado. Ver migracion 019.
            out["evidencia_ausente"] = None if (not valor or valor.upper().startswith("NINGUNA")) else valor
    return out


def guardar_tipos_prueba(document_id: str, texto_clasificacion: str) -> int:
    """
    Guarda la clasificacion de tipo de prueba por lado (prompt 8), reemplazando
    cualquier clasificacion previa de ese document_id (idempotente ante
    reprocesamiento). Devuelve cuantos codigos se guardaron entre ambos lados.

    evidencia_ausente se guarda como NULL a proposito (ver _parsear_tipos_prueba).
    """
    r = _parsear_tipos_prueba(texto_clasificacion)
    total = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM causa_tipos_prueba WHERE document_id = %s", (document_id,))
            for lado in ("actor", "demandado"):
                for codigo in sorted(r[lado]):
                    cur.execute(
                        """INSERT INTO causa_tipos_prueba (document_id, lado, codigo)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (document_id, lado, codigo) DO NOTHING""",
                        (document_id, lado, codigo),
                    )
                    total += 1
            cur.execute(
                """INSERT INTO causa_prueba_analisis (document_id, por_que, evidencia_ausente)
                   VALUES (%s, %s, NULL)
                   ON CONFLICT (document_id) DO UPDATE SET
                       por_que = EXCLUDED.por_que, generated_at = now()""",
                (document_id, r["por_que"]),
            )
        conn.commit()
    return total


def clasificar_y_guardar_tipos_prueba(document_id: str, modelo: str, numero: str = "",
                                      job_id: int = None, flujo_proveedores: str = "alibaba") -> int:
    """
    Orquesta el prompt 8 para una causa YA guardada: lee de vuelta de la base
    los 4 campos que necesita (en vez de re-parsear el texto crudo del prompt 2
    y 3, que ya fue parseado al guardar) y persiste la clasificacion.

    Leer de la base garantiza que el input sea EXACTAMENTE el mismo que uso el
    backfill del corpus historico — si se re-parseara aparte, cualquier drift
    entre los dos parseos daria clasificaciones inconsistentes entre causas
    viejas y nuevas.
    """
    from resolucion.ai.tribunal import clasificar_tipo_prueba

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT objeto_de_controversia, pruebas_del_actor,
                          pruebas_del_demandado, criterio_sala_para_resolver
                   FROM causas WHERE document_id = %s""",
                (document_id,),
            )
            fila = cur.fetchone()
    if not fila:
        return 0

    r = clasificar_tipo_prueba(
        objeto=fila[0] or "", pruebas_actor=fila[1] or "",
        pruebas_demandado=fila[2] or "", criterio_sala=fila[3] or "",
        numero=numero, modelo=modelo, job_id=job_id, flujo_proveedores=flujo_proveedores,
    )
    if r.get("error") or not r.get("tipo_prueba"):
        return 0
    return guardar_tipos_prueba(document_id, r["tipo_prueba"])


def _parsear_evidencia_ausente(texto: str) -> Optional[str]:
    """
    Parsea la salida del prompt 9 ("EVIDENCIA_AUSENTE: <cita>" o "... NINGUNA").
    Devuelve None cuando no hay hallazgo — que es el caso ESPERADO en ~90% de
    las causas (medido en validacion), no un error.
    """
    for linea in (texto or "").splitlines():
        s = linea.strip().strip("-•* ").strip()
        if ":" not in s:
            continue
        etiqueta, valor = s.split(":", 1)
        if etiqueta.strip().upper() != "EVIDENCIA_AUSENTE":
            continue
        valor = valor.strip().strip('"').strip()
        return None if (not valor or valor.upper().startswith("NINGUNA")) else valor
    return None


def guardar_evidencia_ausente(document_id: str, texto_clasificacion: str) -> bool:
    """
    Persiste evidencia_ausente en causa_prueba_analisis (misma fila que usa el
    prompt 8). Hace UPSERT parcial: NO pisa por_que, que lo escribe el prompt 8.
    Devuelve True si quedo un valor no nulo.
    """
    valor = _parsear_evidencia_ausente(texto_clasificacion)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO causa_prueba_analisis (document_id, evidencia_ausente)
                   VALUES (%s, %s)
                   ON CONFLICT (document_id) DO UPDATE SET
                       evidencia_ausente = EXCLUDED.evidencia_ausente""",
                (document_id, valor),
            )
        conn.commit()
    return valor is not None


# ── Prompt 10: extraccion estructurada (causa_extraccion) ────────────────────

_EJES_VALIDOS = set(MATERIA.EJES_DECISION)
_PRUEBA_SUF_VALIDOS = {"si", "no", "parcial", "no_aplica"}
_TIPO_PRUEBA_CLAVE_VALIDOS = set(MATERIA.TIPO_PRUEBA_CLAVE)


def _limpiar_campo_extraccion(valor, largo: int = 400) -> Optional[str]:
    if valor is None:
        return None
    s = str(valor).strip()
    return s[:largo] if s else None


_SQL_P10_COLUMNAS = """c.objeto_de_controversia, c.argumentos_del_demandado,
                          c.pruebas_del_actor, c.pruebas_del_demandado,
                          c.jurisprudencia_mencionada, c.criterio_sala_para_resolver"""


def _entrada_prompt10(document_id: str):
    """Campos de entrada del prompt 10 + (favorable, demandado).

    Tributaria los toma de la vista materializada mv_causas_listado; las bases
    nuevas (laboral, civil) no la tienen, así que se leen de las tablas de
    relación de Strapi 5. 'Favorable' = Con lugar o Parcialmente con lugar
    (siglas CL, P —tributaria— o PCL —laboral/civil—)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('mv_causas_listado') IS NOT NULL")
            hay_mv = cur.fetchone()[0]
            if hay_mv:
                cur.execute(
                    f"""SELECT {_SQL_P10_COLUMNAS},
                              (mv.decision_sigla IN ('CL','P','PCL')) AS favorable,
                              coalesce(mv.entidad_nombre, '')
                       FROM causas c
                       LEFT JOIN mv_causas_listado mv ON mv.document_id = c.document_id
                       WHERE c.document_id = %s""",
                    (document_id,),
                )
            else:
                cur.execute(
                    f"""SELECT {_SQL_P10_COLUMNAS},
                              (d.sigla IN ('CL','P','PCL')) AS favorable,
                              coalesce(e.nombre, '')
                       FROM causas c
                       LEFT JOIN causas_decision_final_lnk dl ON dl.causa_id = c.id
                       LEFT JOIN decision_finals d ON d.id = dl.decision_final_id
                       LEFT JOIN causas_entidad_demandada_lnk el ON el.causa_id = c.id
                       LEFT JOIN entidades e ON e.id = el.entidad_id
                       WHERE c.document_id = %s
                       ORDER BY (d.sigla IS NULL), c.id DESC
                       LIMIT 1""",
                    (document_id,),
                )
            return cur.fetchone()


def clasificar_y_guardar_extraccion(document_id: str, modelo: str, numero: str = "",
                                    job_id: int = None, flujo_proveedores: str = "alibaba") -> bool:
    """
    Orquesta el prompt 10 para una causa YA guardada y persiste en
    `causa_extraccion`. Lee los campos de entrada de vuelta de la base (mismo
    criterio que clasificar_y_guardar_tipos_prueba: garantiza que una causa
    nueva use exactamente el mismo input que uso el backfill historico).

    Idempotente: ON CONFLICT DO UPDATE, se puede reprocesar sin duplicar.
    """
    from resolucion.ai.tribunal import extraccion_estructurada

    fila = _entrada_prompt10(document_id)
    if not fila:
        return False

    r = extraccion_estructurada(
        objeto=fila[0] or "", arg_demandado=fila[1] or "", pruebas_actor=fila[2] or "",
        pruebas_demandado=fila[3] or "", jurisprudencia=fila[4] or "",
        criterio_sala=fila[5] or "", favorable=bool(fila[6]), entidad=fila[7] or "",
        numero=numero, modelo=modelo, job_id=job_id, flujo_proveedores=flujo_proveedores,
    )
    campos = r.get("campos")
    if not campos:
        return False

    # Los tres campos de lista cerrada se validan contra el catalogo; si el
    # modelo devuelve algo fuera de la lista se guarda NULL en vez de
    # contaminar las estadisticas con un valor inventado.
    eje = _limpiar_campo_extraccion(campos.get("eje_decision"))
    eje = eje.lower().split("|")[0].strip() if eje else None
    if eje not in _EJES_VALIDOS:
        eje = None
    psuf = _limpiar_campo_extraccion(campos.get("prueba_suficiente"))
    psuf = psuf.lower().split("|")[0].strip() if psuf else None
    if psuf not in _PRUEBA_SUF_VALIDOS:
        psuf = None
    tpc = _limpiar_campo_extraccion(campos.get("tipo_prueba_clave"))
    tpc = tpc.lower().split("|")[0].strip() if tpc else None
    if tpc not in _TIPO_PRUEBA_CLAVE_VALIDOS:
        tpc = None

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO causa_extraccion
                     (document_id, eje_decision, prueba_suficiente, tipo_prueba_clave,
                      norma_clave, precedente_citado, tesis_contraparte, como_superar,
                      clave_resultado, cita_textual, model)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (document_id) DO UPDATE SET
                     eje_decision=EXCLUDED.eje_decision,
                     prueba_suficiente=EXCLUDED.prueba_suficiente,
                     tipo_prueba_clave=EXCLUDED.tipo_prueba_clave,
                     norma_clave=EXCLUDED.norma_clave,
                     precedente_citado=EXCLUDED.precedente_citado,
                     tesis_contraparte=EXCLUDED.tesis_contraparte,
                     como_superar=EXCLUDED.como_superar,
                     clave_resultado=EXCLUDED.clave_resultado,
                     cita_textual=EXCLUDED.cita_textual,
                     model=EXCLUDED.model, extracted_at=now()""",
                (document_id, eje, psuf, tpc,
                 _limpiar_campo_extraccion(campos.get("norma_clave")),
                 _limpiar_campo_extraccion(campos.get("precedente_citado")),
                 _limpiar_campo_extraccion(campos.get("tesis_contraparte")),
                 _limpiar_campo_extraccion(campos.get("como_superar")),
                 _limpiar_campo_extraccion(campos.get("clave_resultado")),
                 _limpiar_campo_extraccion(campos.get("cita_textual"), 900),
                 modelo),
            )
        conn.commit()
    return True
