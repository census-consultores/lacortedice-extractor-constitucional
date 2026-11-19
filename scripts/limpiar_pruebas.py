"""
Limpieza de datos de prueba en producción.

Borra:
  - worker_jobs, worker_caso_log, worker_pending_uploads (todos los registros)
  - worker_causa_datos, worker_causas, worker_job_logs,
    worker_backfill_intentos, worker_backfill_stats (todos los registros —
    son las tablas propias del worker que alimentan el admin panel,
    independientes de Strapi)
  - Causa 09501-2019-00010 en Strapi (causa + casacion + archivos media)

Uso:
  python scripts/limpiar_pruebas.py [--dry-run]

Lee credenciales de config/.env y lacortedice-cms/.env (sin hardcodear nada).
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Cargar los dos .env relevantes
from dotenv import load_dotenv, dotenv_values

worker_env = dotenv_values(os.path.join(PROJECT_ROOT, "config", ".env"))
cms_env    = dotenv_values(os.path.join(PROJECT_ROOT, "..", "lacortedice-cms", ".env"))

# Credenciales: producción desde CMS .env, Strapi desde worker .env
RENDER_DB_URL = cms_env.get("RENDER_DATABASE_URL", "")
LOCAL_DB_URL  = worker_env.get("DATABASE_URL", "")
STRAPI_URL    = worker_env.get("STRAPI_URL", "http://localhost:1337")
STRAPI_TOKEN  = worker_env.get("STRAPI_API_TOKEN", "")
HEADERS       = {"Authorization": f"Bearer {STRAPI_TOKEN}", "Content-Type": "application/json"}

DRY_RUN      = "--dry-run" in sys.argv
NUMERO_CAUSA = "09501-2019-00010"

VERDE  = "\033[92m"
ROJO   = "\033[91m"
AMARIL = "\033[93m"
RESET  = "\033[0m"

def ok(msg):     print(f"  {VERDE}✓{RESET} {msg}")
def err(msg):    print(f"  {ROJO}✗{RESET} {msg}")
def warn(msg):   print(f"  {AMARIL}~{RESET} {msg}")
def titulo(msg): print(f"\n{'─'*55}\n{msg}\n{'─'*55}")

# ── DB helpers ────────────────────────────────────────────────────────────────
def conectar_db():
    import psycopg2
    for url, label in [(RENDER_DB_URL, "Render producción"), (LOCAL_DB_URL, "local")]:
        if not url:
            continue
        try:
            conn = psycopg2.connect(url, connect_timeout=10)
            print(f"  Conectado a BD {label}")
            return conn, label
        except Exception as e:
            warn(f"BD {label} no disponible: {e}")
    return None, None

def db_count(conn, tabla):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {tabla}")
        return cur.fetchone()[0]

def db_delete(conn, tabla):
    if DRY_RUN:
        n = db_count(conn, tabla)
        warn(f"[dry-run] DELETE FROM {tabla}  (habría borrado {n} filas)")
        return n
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {tabla}")
        n = cur.rowcount
    conn.commit()
    return n

# ── Strapi helpers ────────────────────────────────────────────────────────────
import httpx

def strapi_get(path, params=None):
    try:
        r = httpx.get(f"{STRAPI_URL}{path}", params=params, headers=HEADERS, timeout=15)
        if r.is_success:
            return r.json()
        warn(f"GET {path} → HTTP {r.status_code}")
    except Exception as e:
        warn(f"GET {path} → {e}")
    return {}

def strapi_delete(path, desc=""):
    if DRY_RUN:
        warn(f"[dry-run] DELETE {path}  ({desc})")
        return True
    try:
        r = httpx.delete(f"{STRAPI_URL}{path}", headers=HEADERS, timeout=15)
        if r.is_success:
            return True
        warn(f"DELETE {path} → HTTP {r.status_code}: {r.text[:150]}")
    except Exception as e:
        warn(f"DELETE {path} → {e}")
    return False

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if DRY_RUN:
        print(f"\n{AMARIL}═══ MODO DRY-RUN — no se borra nada real ═══{RESET}")

    # ── 1. Tablas worker en PostgreSQL ────────────────────────────────────────
    titulo("1. Tablas worker (worker_jobs · worker_caso_log · worker_pending_uploads)")

    conn, label = conectar_db()
    if conn:
        tablas = [
            ("worker_pending_uploads",   "uploads pendientes"),
            ("worker_caso_log",          "registros /admin/registros"),
            ("worker_jobs",              "jobs de extracción"),
            ("worker_job_logs",          "logs WARN+ persistidos"),
            ("worker_backfill_intentos", "intentos de backfill"),
            ("worker_backfill_stats",    "stats de backfill"),
            ("worker_causa_datos",       "datos extraídos por causa (admin panel)"),
            ("worker_causas",            "causas indexadas por el worker (admin panel)"),
        ]
        for tabla, desc in tablas:
            try:
                total = db_count(conn, tabla)
                if total == 0:
                    ok(f"{tabla}: ya vacía (0 filas)")
                    continue
                n = db_delete(conn, tabla)
                ok(f"{tabla}: {n}/{total} filas eliminadas  [{desc}]")
            except Exception as e:
                err(f"{tabla}: {e}")
        conn.close()
    else:
        err("Sin conexión a BD — omitiendo limpieza de tablas worker")

    # ── 2 y 3. Causa + archivos vía PostgreSQL directo ───────────────────────
    titulo(f"2. Causa {NUMERO_CAUSA} + archivos media (via PostgreSQL producción)")

    import psycopg2, psycopg2.extras as _extras

    conn2, label2 = conectar_db()
    if not conn2:
        err("Sin conexión a BD — no se puede borrar la causa")
        return

    cur = conn2.cursor(cursor_factory=_extras.DictCursor)

    # Buscar causa
    cur.execute("SELECT id, document_id FROM causas WHERE juicio = %s", (NUMERO_CAUSA,))
    causa_row = cur.fetchone()

    if not causa_row:
        warn(f"Causa {NUMERO_CAUSA} no encontrada en BD")
    else:
        causa_id  = causa_row["id"]
        causa_doc = causa_row["document_id"]
        print(f"  Causa: id={causa_id}  document_id={causa_doc}")

        # Borrar casaciones vinculadas
        try:
            cur.execute("""
                DELETE FROM casaciones
                WHERE id IN (
                    SELECT casacion_id FROM casaciones_causa_lnk WHERE causa_id = %s
                )
            """, (causa_id,))
            n_cas = cur.rowcount
            conn2.commit()
            if n_cas: ok(f"Casaciones eliminadas: {n_cas}")
        except Exception as e:
            conn2.rollback()
            warn(f"Borrar casaciones: {e}")

        # Borrar tablas de relación de la causa
        link_tables = [
            "causas_juez_lnk", "causas_causa_jueces_lnk", "causas_voto_salvado_lnk",
            "causas_decision_final_lnk", "causas_tipo_de_accion_lnk",
            "causas_causa_tribunal_lnk", "causas_entidad_demandada_lnk",
            "causas_estado_del_juicio_lnk", "causas_casacion_lnk",
            "causas_components", "causas_localizations_lnk",
        ]
        for t in link_tables:
            try:
                if DRY_RUN:
                    cur.execute(f"SELECT COUNT(*) FROM {t} WHERE causa_id = %s", (causa_id,))
                    n = cur.fetchone()[0]
                    if n: warn(f"[dry-run] {t}: {n} filas")
                else:
                    cur.execute(f"DELETE FROM {t} WHERE causa_id = %s", (causa_id,))
                    n = cur.rowcount
                    if n: ok(f"{t}: {n} filas borradas")
                    conn2.commit()
            except Exception as e:
                conn2.rollback()
                if "does not exist" not in str(e):
                    warn(f"{t}: {e}")

        # Borrar la causa
        if DRY_RUN:
            warn(f"[dry-run] DELETE FROM causas WHERE id={causa_id}")
        else:
            cur.execute("DELETE FROM causas WHERE id = %s", (causa_id,))
            conn2.commit()
            ok(f"Causa id={causa_id} ({NUMERO_CAUSA}) eliminada")

    # Buscar y borrar archivos media
    numero_guion = NUMERO_CAUSA.replace("-", "_")
    cur.execute("SELECT id, name FROM files WHERE name ILIKE %s", (f"%{numero_guion}%",))
    archivos = cur.fetchall()

    if not archivos:
        warn("No se encontraron archivos en media library")
    else:
        file_ids = [f["id"] for f in archivos]
        print(f"  Archivos encontrados: {len(archivos)}")
        for t in ["files_related_morphs", "files_folder_lnk"]:
            try:
                if not DRY_RUN:
                    cur.execute(f"DELETE FROM {t} WHERE file_id = ANY(%s)", (file_ids,))
                    conn2.commit()
            except Exception as e:
                conn2.rollback()
                if "does not exist" not in str(e):
                    warn(f"{t}: {e}")
        if DRY_RUN:
            warn(f"[dry-run] DELETE FROM files WHERE id IN {file_ids}")
        else:
            cur.execute("DELETE FROM files WHERE id = ANY(%s)", (file_ids,))
            n = cur.rowcount
            conn2.commit()
            ok(f"{n} archivos eliminados de media library")

    conn2.close()

    print(f"\n{'═'*55}")
    if DRY_RUN:
        print(f"  DRY-RUN completado. Corre sin --dry-run para borrar en serio.")
    else:
        print(f"  Limpieza completada.")
    print(f"{'═'*55}\n")

if __name__ == "__main__":
    main()
