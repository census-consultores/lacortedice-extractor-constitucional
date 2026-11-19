"""
Tribunal Worker — API de control de extracciones.
Expone REST + SSE para que lacortedice-webapp pueda lanzar, monitorear
y controlar jobs de extracción de sentencias.

Endpoints:
  POST   /jobs                    lanzar corrida
  GET    /jobs                    listar todos los jobs
  GET    /jobs/{id}               estado + progreso
  POST   /jobs/{id}/pause         pausar
  POST   /jobs/{id}/resume        reanudar
  POST   /jobs/{id}/kill          cancelar
  GET    /jobs/{id}/events        SSE progreso en tiempo real
  GET    /health                  ping
"""

import os
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

# Cargar .env si existe (desarrollo local)
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path="config/.env")
except ImportError:
    pass

from worker import job_manager as jm

WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")
# Clave de solo lectura para el buscador de texto: se entrega a abogados sin
# darles la clave del worker (que crea jobs y gasta IA).
BUSCADOR_API_KEY = os.environ.get("BUSCADOR_API_KEY", "")


# ── Startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from worker import logger as wlog
    from worker import scheduler as sched
    wlog.sistema("INFO", "Worker arrancando — inicializando BD y recuperando jobs")
    # Antes que nada: por dónde sale a SATJE. Al arrancar se encolan y lanzan
    # jobs solos (JOBS_ARRANQUE, cola automática), así que esto va primero.
    import api as satje
    activo, detalle = satje.activar_proxy_desde_entorno()
    wlog.sistema("INFO" if activo else "WARN", detalle)
    if activo:
        ok, msg = satje.verificar_proxy()
        wlog.sistema("INFO" if ok else "ERROR", f"Chequeo de salida — {msg}")
    jm.init_db()
    from config.config import PROMPTS_DIR
    jm.bootstrap_prompts_desde_archivos(PROMPTS_DIR)
    try:
        nuevos = jm.crear_jobs_arranque()
        if nuevos:
            wlog.sistema("INFO", f"JOBS_ARRANQUE: {len(nuevos)} jobs de descarga encolados")
    except Exception as e:
        wlog.sistema("ERROR", f"JOBS_ARRANQUE inválido: {e}")
    try:
        nuevos = jm.acciones_arranque()
        if nuevos:
            wlog.sistema("INFO", f"ACCIONES_ARRANQUE: jobs creados {nuevos}")
    except Exception as e:
        wlog.sistema("ERROR", f"ACCIONES_ARRANQUE inválido: {e}")
    jm.recuperar_jobs_interrumpidos()
    jm.iniciar_cola_automatica()
    sched.init_schedules()
    wlog.sistema("INFO", "Worker listo")
    yield
    sched.apagar_scheduler()
    wlog.sistema("INFO", "Worker apagándose")

app = FastAPI(title="Tribunal Worker", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restringir a lacortedice en producción
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth ──────────────────────────────────────────────────────────────────────

_SESIONES_ADMIN: dict = {}   # sha256(token) -> vence (epoch); caché de 5 minutos


def _sesion_admin_valida(authorization: Optional[str]) -> bool:
    """Acepta la sesión de un administrador del CMS de esta materia
    ("Authorization: Bearer <jwt del panel /admin>"), validada contra
    STRAPI_URL/admin/users/me. Así se lanzan trabajos desde el navegador del
    administrador sin escribir la clave del worker en ningún lado."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    import hashlib, time
    import requests
    from worker.strapi_client import STRAPI_URL
    token = authorization.split(" ", 1)[1].strip()
    h = hashlib.sha256(token.encode()).hexdigest()
    if _SESIONES_ADMIN.get(h, 0) > time.time():
        return True
    try:
        r = requests.get(STRAPI_URL.rstrip("/") + "/admin/users/me",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except requests.RequestException:
        return False
    if r.status_code != 200 or not (r.json().get("data") or {}).get("isActive"):
        return False
    _SESIONES_ADMIN[h] = time.time() + 300
    return True


def verificar_key(x_worker_key: Optional[str] = Header(None),
                  authorization: Optional[str] = Header(None)):
    if WORKER_API_KEY and x_worker_key == WORKER_API_KEY:
        return
    if _sesion_admin_valida(authorization):
        return
    raise HTTPException(status_code=401, detail="API key inválida")


def verificar_key_buscador(x_worker_key: str = Header(...)):
    """Acepta la clave del buscador o la del worker."""
    if x_worker_key not in {k for k in (BUSCADOR_API_KEY, WORKER_API_KEY) if k}:
        raise HTTPException(status_code=401, detail="API key inválida")


# ── Schemas ───────────────────────────────────────────────────────────────────

class NuevoJob(BaseModel):
    tribunal:     str
    año:          str
    desde:        int        = 1
    hasta:        Optional[int] = None
    dry_run:      bool       = False
    # Corre la IA igual que una corrida completa, pero no escribe nada en
    # Strapi (ni archivos ni el registro de causa/casación) — para revisar
    # en /admin/registros qué extrae el extractor sin tocar lo publicado.
    # Sin efecto si dry_run=True (ahí no corre IA de todos modos).
    vista_previa: bool       = False
    # Baja y persiste PDFs y metadatos (SATJE, COGEP, duración) en Strapi y en
    # el registro canónico SIN llamar a la IA. Es la fase "corpus primero": la
    # extracción se corre después, desde los PDFs guardados, con POST /reextraer.
    solo_descarga: bool      = False
    # Solo los números que el índice de SATJE marcó como de la materia
    # (POST /indice). Requiere el par tribunal/año indexado por completo.
    desde_indice: bool       = False
    # "alibaba" (default), "novita" o "gmicloud" — cuál proveedor de OpenRouter se
    # intenta primero para Qwen (mismo modelo/pesas en ambos, solo cambia
    # el orden de la infraestructura de respaldo). Sin efecto en GPT.
    flujo_proveedores: str   = "alibaba"
    modelo:       str        = "gpt-4o"
    iniciado_por: str        = ""
    categoria:    str        = "extraccion"


class NuevoBackfill(BaseModel):
    campo:        str
    lote:         int = 100
    modo:         str = "completar"  # "completar" (solo faltantes) | "sobrescribir" (todo el historial)
    iniciado_por: str = ""


class NuevoEmbedding(BaseModel):
    # "faltantes" = solo secciones nuevas o con texto cambiado (lo de todos los
    # días). "todo" = rehace el corpus entero ignorando el hash; se usa al
    # cambiar de modelo, porque mezclar vectores de dos modelos degrada la
    # búsqueda sin dar ningún error.
    modo:         str = "faltantes"
    iniciado_por: str = ""



class NuevoBrief(BaseModel):
    # Vacío = todas las secciones. Acotar sirve para rehacer solo una cuando
    # se toca su prompt, sin pagar las otras cinco de nuevo.
    seccion:      str = ""   # jueces | tribunales | cnj | abogados | entidades | actores
    iniciado_por: str = ""



class NuevaRevision(BaseModel):
    # año vacío = el corriente.
    año:          str = ""
    # "diaria" vigila lo que puede moverse; "completa" barre desde el 1 (línea
    # base de un año nunca revisado).
    modo:         str = "diaria"
    # En cuántos jobs partir el trabajo. Medido en Render: ~9 números/minuto,
    # así que los ~5.500 de una revisión diaria en un solo job son ~10 h. Con
    # 4 bajan a ~4 h (manda el grupo más cargado, Pichincha). Hay 4 slots.
    particiones:  int = 4
    # True = lo que falte se baja sin IA (jobs solo_descarga, tres niveles);
    # es el modo de las materias nuevas mientras el corpus está en
    # "PDF sin análisis". False = job de lista con IA (tributario).
    descarga:     bool = False
    iniciado_por: str = ""



class GuardarPrompt(BaseModel):
    contenido:    str
    modo:         str  # "sobrescribir" | "nueva_version"
    nota:         Optional[str] = None
    creado_por:   str = ""


class NuevoSchedule(BaseModel):
    nombre:    str
    tribunal:  str           = "-"
    año:       str           = "0000"
    desde:     int           = 1
    hasta:     Optional[int] = None
    modelo:    str           = "gpt-4o"
    # OJO: el scheduler corre en UTC y Ecuador es UTC-5. Medianoche local se
    # escribe "0 5 * * *", no "0 0 * * *".
    cron_expr: str           = "0 3 * * *"
    # Qué job dispara. Vacío = extracción normal (y ahí sí hacen falta
    # tribunal y año reales). También: "embeddings[:modo]", "brief[:seccion]",
    # "revision[:año][:diaria|completa][:descarga][:p=N]".
    workers:   str           = ""
    # "cron" = cupo propio de 1. "extraccion" = comparte los 4 slots.
    categoria: str           = "cron"


class ItemLista(BaseModel):
    juicio:   str
    tribunal: bool = False   # extraer/refrescar la sentencia de tribunal
    corte:    bool = False   # extraer/agregar la casación (corte)


class NuevaLista(BaseModel):
    """Conjunto personalizado de causas a extraer, cada una con sus flags.
    El motor procesa solo lo marcado por causa (ver worker/lista_worker.py)."""
    items:             list[ItemLista]
    modelo:            str = "Qwen/Qwen3-235B-A22B-Instruct-2507"
    flujo_proveedores: str = "alibaba"
    iniciado_por:      str = ""


# ── Ingesta de lista por Excel ────────────────────────────────────────────────

def _es_marca(v) -> bool:
    """Una celda cuenta como 'marcada' (x) si no está vacía ni es un 'no' claro."""
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in ("", "0", "false", "no", "n", "-", "false.")


def _parsear_excel_lista(contenido: bytes) -> list:
    """Lee un Excel de 3 columnas (sentencia | tribunal | corte) y devuelve
    [(juicio, do_tribunal, do_corte)]. Detecta las columnas por encabezado; si
    no hay encabezado reconocible, usa posición (0=sentencia, 1=tribunal, 2=corte).
    Una celda con cualquier marca (x, sí, 1…) activa el flag."""
    import io, openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
    ws = wb.active
    filas = [f for f in ws.iter_rows(values_only=True) if f and any(c is not None for c in f)]
    if not filas:
        return []
    enc = [str(c).strip().lower() if c is not None else "" for c in filas[0]]

    def _idx(claves, default):
        for i, h in enumerate(enc):
            if any(k in h for k in claves):
                return i
        return default

    hay_cabecera = any(k in " ".join(enc) for k in
                       ("sentencia", "juicio", "causa", "tribunal", "corte", "casacion"))
    if hay_cabecera:
        c_j = _idx(("sentencia", "juicio", "causa"), 0)
        c_t = _idx(("tribunal",), 1)
        c_c = _idx(("corte", "casacion"), 2)
        cuerpo = filas[1:]
    else:
        c_j, c_t, c_c, cuerpo = 0, 1, 2, filas

    items = []
    for f in cuerpo:
        if len(f) <= c_j or f[c_j] is None:
            continue
        juicio = str(f[c_j]).strip()
        if not juicio or juicio.lower() in ("sentencia", "juicio", "causa"):
            continue
        do_t = _es_marca(f[c_t]) if len(f) > c_t else False
        do_c = _es_marca(f[c_c]) if len(f) > c_c else False
        if do_t or do_c:
            items.append((juicio, do_t, do_c))
    return items


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        import psutil
        mem = psutil.virtual_memory()
        recursos = {
            "cpu_pct":    psutil.cpu_percent(interval=0.1),
            "ram_used_mb": mem.used    // (1024 * 1024),
            "ram_total_mb": mem.total  // (1024 * 1024),
            "ram_pct":    mem.percent,
        }
    except Exception:
        recursos = {}
    return {"status": "ok", "recursos": recursos}


@app.get("/buscar", dependencies=[Depends(verificar_key_buscador)])
def buscar(q: str, tribunal: Optional[str] = None, anio: Optional[str] = None,
           nivel: Optional[str] = None, limite: int = 50, offset: int = 0):
    """Busca una frase en el texto de las sentencias indexadas (causa_texto).
    Sintaxis websearch de Postgres: "frase exacta", palabra -excluida."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="q vacío")
    from worker.strapi_client import STRAPI_URL
    r = jm.buscar_texto(q, tribunal=tribunal, anio=anio, nivel=nivel,
                        limite=min(int(limite), 200), offset=int(offset))
    for f in r["resultados"]:
        for k in ("url_pdf_tribunal", "url_pdf_apel", "url_pdf_cnj"):
            if f.get(k) and f[k].startswith("/"):
                f[k] = STRAPI_URL.rstrip("/") + f[k]
    return r


def _directorio(fn):
    """Corre fn(cursor) sobre directorio_abogados; 404 si aún no existe."""
    import psycopg2.errors
    try:
        with jm._conn() as conn, conn.cursor() as cur:
            return fn(cur)
    except psycopg2.errors.UndefinedTable:
        raise HTTPException(status_code=404, detail="El directorio aún no tiene datos")


@app.get("/abogados/datos", dependencies=[Depends(verificar_key)])
def abogados_datos(q: str = "", materia: str = "", provincia: str = "", con_correo: bool = False,
                   limite: int = 50, offset: int = 0):
    """Directorio consolidado de abogados (las cuatro materias). Solo con la clave
    del worker: son datos personales de uso interno, no van al buscador ni al CMS."""
    from scripts.directorio_abogados import consultar, resumen
    limite, offset = max(1, min(limite, 200)), max(0, offset)
    return _directorio(lambda cur: {
        "resumen": resumen(cur),
        **consultar(cur, q, materia, provincia, con_correo, limite, offset)})


@app.get("/abogados", dependencies=[Depends(verificar_key)])
def abogados_csv(q: str = "", materia: str = "", provincia: str = "", con_correo: bool = False):
    """Mismo directorio consolidado, completo, en CSV (UTF-8 con BOM para Excel).
    Se envía por partes: civil puede pasar de 100.000 abogados y el worker tiene 512 MB."""
    import csv
    import io
    from scripts.directorio_abogados import CONSULTA_SQL, _filtros, fusionar
    where, params = _filtros(q, materia, provincia, con_correo)
    _directorio(lambda cur: cur.execute("SELECT 1 FROM directorio_abogados LIMIT 1"))  # 404 antes de empezar

    def filas_csv():
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["nombre", "correo", "otros_correos", "materias", "n_causas", "provincias",
                    "anio_min", "anio_max", "matriculas", "telefonos", "causas_ejemplo"])
        yield "\ufeff" + out.getvalue()
        with jm._conn() as conn, conn.cursor(name="abogados_csv") as cur:
            cur.itersize = 1000
            cur.execute(CONSULTA_SQL.format(where=where), params)
            for r in cur:
                f = fusionar(*r)
                out.seek(0)
                out.truncate()
                w.writerow([f["nombre"], f["correo"], "; ".join(f["otros_correos"]),
                            "; ".join(f"{m} ({n})" for m, n in f["materias"].items()), f["n_causas"],
                            "; ".join(f["provincias"]), f["anio_min"], f["anio_max"],
                            "; ".join(f["matriculas"]), "; ".join(f["telefonos"]), "; ".join(f["causas"])])
                yield out.getvalue()

    return StreamingResponse(filas_csv(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=directorio_abogados.csv"})


@app.get("/abogados/ver", response_class=HTMLResponse)
def abogados_ver():
    """Página del directorio; pide la clave del worker y consulta /abogados/datos."""
    return (Path(__file__).parent / "static" / "abogados.html").read_text(encoding="utf-8")


@app.get("/buscador", response_class=HTMLResponse)
def buscador():
    """Página mínima para que un abogado use GET /buscar sin herramientas."""
    from config import materia as MATERIA
    html = (Path(__file__).parent / "static" / "buscador.html").read_text(encoding="utf-8")
    return html.replace("{{MATERIA}}", MATERIA.NOMBRE)


@app.post("/jobs", dependencies=[Depends(verificar_key)])
def crear_job(body: NuevoJob):
    _validar_tribunal(body.tribunal)
    _validar_año(body.año)
    job = jm.crear_job(
        tribunal     = body.tribunal,
        año          = body.año,
        desde        = body.desde,
        hasta        = body.hasta,
        dry_run      = body.dry_run,
        vista_previa = body.vista_previa,
        solo_descarga = body.solo_descarga,
        flujo_proveedores = body.flujo_proveedores,
        modelo       = body.modelo,
        iniciado_por = body.iniciado_por,
        categoria    = body.categoria,
        workers      = "desde_indice" if body.desde_indice else "",
    )
    return job


# ── Índice rápido de SATJE (worker/indice_worker.py) ──────────────────────────

class NuevoIndice(BaseModel):
    """Barre (prefijo × año) en SATJE guardando solo número + idMateria.
    prefijos vacío = todas las judicaturas de config/materia.py."""
    prefijos:   list[str] = []
    años:       list[str] = []
    hilos:      int   = 8
    pausa:      float = 0.3
    max_vacias: int   = 30
    iniciado_por: str = ""


@app.post("/indice", dependencies=[Depends(verificar_key)])
def crear_indice(body: NuevoIndice):
    import json as _json
    from config.config import TRIBUNALES
    prefijos = [p.zfill(5) for p in (body.prefijos or list(TRIBUNALES))]
    from datetime import datetime as _dt
    años = body.años or [str(a) for a in range(2016, _dt.now().year + 1)]
    for a in años:
        _validar_año(a)
    job = jm.crear_job(
        tribunal="-", año="-", desde=1, hasta=len(prefijos) * len(años), dry_run=False,
        modelo="-", iniciado_por=body.iniciado_por, workers="indice", categoria="indice",
        parametros=_json.dumps({"prefijos": prefijos, "años": años, "hilos": body.hilos,
                                "pausa": body.pausa, "max_vacias": body.max_vacias}),
    )
    return {**job, "pares": len(prefijos) * len(años)}


@app.get("/indice/resumen", dependencies=[Depends(verificar_key)])
def resumen_indice():
    from worker.indice_worker import resumen
    from config import materia as _MAT
    return resumen(sorted(_MAT.IDS_MATERIA_SATJE))


# ── Mapeo de volumen (worker/mapeo_worker.py) ─────────────────────────────────

class NuevoMapeo(BaseModel):
    """Cuenta causas por judicatura y año sin barrer número por número.
    Vacíos = las 24 provincias, 2016 al año actual, claves penales."""
    provincias: list[str] = []
    años:       list[str] = []
    claves:     list[str] = []
    hilos:      int   = 6
    pausa:      float = 0.3
    iniciado_por: str = ""


@app.post("/mapeo", dependencies=[Depends(verificar_key)])
def crear_mapeo(body: NuevoMapeo):
    import json as _json
    for a in body.años:
        _validar_año(a)
    job = jm.crear_job(
        tribunal="-", año="-", desde=1, hasta=None, dry_run=False, modelo="-",
        iniciado_por=body.iniciado_por, workers="mapeo", categoria="mapeo",
        parametros=_json.dumps(body.model_dump(exclude={"iniciado_por"}), ensure_ascii=False),
    )
    return job


@app.get("/mapeo/resumen", dependencies=[Depends(verificar_key)])
def resumen_mapeo():
    from worker.mapeo_worker import resumen
    return resumen()


class DescargaDesdeIndice(BaseModel):
    """Crea un job solo_descarga + desde_indice por cada (prefijo, año) ya
    indexado que tenga causas de la materia y que no tenga ya un job igual."""
    prefijos: list[str] = []
    años:     list[str] = []
    iniciado_por: str = "corpus-desde-indice"


@app.post("/indice/descargar", dependencies=[Depends(verificar_key)])
def descargar_desde_indice(body: DescargaDesdeIndice):
    from worker.indice_worker import resumen
    from config import materia as _MAT
    from config.config import TRIBUNALES
    pares = resumen(sorted(_MAT.IDS_MATERIA_SATJE))["pares_materia"]
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT tribunal, año FROM worker_jobs
                            WHERE workers='desde_indice' AND estado <> 'cancelado'""")
            ya = {(t, a) for t, a in cur.fetchall()}
    creados = []
    for p in pares:
        pf, a = p["prefijo"], p["anio"]
        if not p["terminado"] or pf not in TRIBUNALES or (pf, a) in ya:
            continue
        if body.prefijos and pf not in body.prefijos:
            continue
        if body.años and a not in body.años:
            continue
        job = jm.crear_job(tribunal=pf, año=a, desde=1, hasta=None, dry_run=False,
                           modelo="gpt-4o", iniciado_por=body.iniciado_por,
                           workers="desde_indice", solo_descarga=True)
        creados.append({"id": job["id"], "tribunal": pf, "año": a, "causas": p["causas"]})
    return {"creados": len(creados), "jobs": creados}


class NuevaReextraccion(BaseModel):
    """Re-extraer con IA desde los PDFs ya guardados (sin SATJE).
    O bien `numeros` explícitos, o bien el filtro tribunal/año/completitud/limite."""
    numeros:           list[str] = []
    niveles:           list[str] = ["tribunal", "corte", "apelacion"]
    tribunal:          str = "-"
    año:               str = "-"
    completitud:       Optional[str] = None     # p. ej. "PDF sin análisis" | "Parcial - revisar"
    limite:            Optional[int] = None
    modelo:            str = "gpt-4o"
    flujo_proveedores: str = "alibaba"
    iniciado_por:      str = ""


@app.post("/reextraer", dependencies=[Depends(verificar_key)])
def crear_job_reextraccion(body: NuevaReextraccion):
    niveles = [n for n in body.niveles if n in ("tribunal", "corte", "apelacion")]
    if not niveles:
        raise HTTPException(400, "niveles debe incluir tribunal, corte y/o apelacion")
    if body.tribunal not in ("-", "", None):
        _validar_tribunal(body.tribunal)
    numeros = [n.strip() for n in body.numeros if n.strip()]
    job = jm.crear_job_reextraccion(
        numeros, modelo=body.modelo, niveles=niveles, tribunal=body.tribunal,
        anio=body.año, completitud=body.completitud, limite=body.limite,
        iniciado_por=body.iniciado_por, flujo_proveedores=body.flujo_proveedores,
    )
    return {**job, "n_numeros": len(numeros), "niveles": niveles}


@app.post("/jobs/lista", dependencies=[Depends(verificar_key)])
def crear_job_lista(body: NuevaLista):
    """Lanza un job que procesa un conjunto PERSONALIZADO de causas, cada una
    con flags de qué extraer (tribunal / corte)."""
    items = [
        (i.juicio.strip(), i.tribunal, i.corte)
        for i in body.items
        if i.juicio.strip() and (i.tribunal or i.corte)
    ]
    if not items:
        raise HTTPException(status_code=400,
                            detail="La lista está vacía o ninguna causa tiene flags (tribunal/corte).")
    job = jm.crear_job_lista(
        items, modelo=body.modelo, iniciado_por=body.iniciado_por,
        flujo_proveedores=body.flujo_proveedores,
    )
    return {**job, "n_items": len(items)}


@app.post("/jobs/lista/excel", dependencies=[Depends(verificar_key)])
async def crear_job_lista_excel(
    archivo:           UploadFile = File(...),
    modelo:            str = Form("Qwen/Qwen3-235B-A22B-Instruct-2507"),
    flujo_proveedores: str = Form("alibaba"),
    iniciado_por:      str = Form(""),
):
    """Igual que POST /jobs/lista pero recibiendo un Excel de 3 columnas
    (sentencia | tribunal | corte)."""
    contenido = await archivo.read()
    try:
        items = _parsear_excel_lista(contenido)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el Excel: {e}")
    if not items:
        raise HTTPException(
            status_code=400,
            detail="El Excel no tiene causas con flags. Columnas esperadas: sentencia | tribunal | corte.",
        )
    job = jm.crear_job_lista(
        items, modelo=modelo, iniciado_por=iniciado_por,
        flujo_proveedores=flujo_proveedores,
    )
    return {**job, "n_items": len(items)}


@app.get("/jobs", dependencies=[Depends(verificar_key)])
def listar_jobs():
    return jm.listar_jobs()


@app.get("/jobs/{job_id}", dependencies=[Depends(verificar_key)])
def obtener_job(job_id: int):
    job = jm.obtener_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    return job


@app.post("/jobs/{job_id}/pause", dependencies=[Depends(verificar_key)])
def pausar(job_id: int):
    ok = jm.pausar_job(job_id)
    if not ok:
        raise HTTPException(400, "No se puede pausar (job no está corriendo)")
    return {"ok": True}


@app.post("/jobs/{job_id}/resume", dependencies=[Depends(verificar_key)])
def reanudar(job_id: int):
    ok = jm.reanudar_job(job_id)
    if not ok:
        raise HTTPException(400, "No se puede reanudar (estado inválido)")
    return {"ok": True}


@app.post("/jobs/{job_id}/kill", dependencies=[Depends(verificar_key)])
def matar(job_id: int):
    jm.matar_job(job_id)
    return {"ok": True}


@app.get("/jobs/{job_id}/events")
async def eventos_sse(
    job_id: int,
    x_worker_key: Optional[str] = Header(None),
    key: Optional[str] = None,
):
    """
    SSE: emite eventos de progreso en tiempo real para un job.
    Acepta la API key como header X-Worker-Key O como query param ?key=
    para permitir conexiones directas desde el browser via EventSource.
    """
    api_key = x_worker_key or key
    if not WORKER_API_KEY or api_key != WORKER_API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")

    job = jm.obtener_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")

    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    loop = asyncio.get_event_loop()

    def _enqueue(evento: dict):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, evento)
        except Exception:
            pass

    # Wrapper sincrónico que el job_manager puede usar desde el hilo de extracción
    class _SyncQueue:
        def put_nowait(self, item):
            _enqueue(item)

    sync_q = _SyncQueue()
    jm.suscribir_sse(job_id, sync_q)

    async def _stream():
        try:
            # Estado inicial del job
            estado_actual = jm.obtener_job(job_id)
            yield _sse(estado_actual)

            # Si el job ya está procesando una causa, mandarla de inmediato
            # (el evento "procesando" pudo haberse emitido antes de que el cliente conectara)
            procesando = jm.get_procesando_actual(job_id)
            if procesando:
                yield _sse(procesando)

            while True:
                try:
                    evento = await asyncio.wait_for(queue.get(), timeout=30)
                    yield _sse(evento)
                    if evento.get("estado") in ("completado", "cancelado"):
                        break
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # keepalive
        finally:
            jm.desuscribir_sse(job_id, sync_q)

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ── Balances de APIs IA ───────────────────────────────────────────────────────

@app.get("/balance", dependencies=[Depends(verificar_key)])
async def obtener_balances():
    """
    Consulta saldo/estado de las APIs de IA.
    OpenAI: billing API solo accesible con org-key → validamos con /models.
    DeepInfra: endpoint de cuenta con saldo real.
    """
    import httpx

    openai_key    = os.environ.get("OPENAI_API_KEY", "")
    deepinfra_key = os.environ.get("DEEPINFRA_API_KEY", "")
    results: dict = {}

    async with httpx.AsyncClient(timeout=8) as client:
        # OpenAI — la billing API requiere org-admin key; validamos que la key
        # funcione con /v1/models (call barato) y reportamos "activo".
        if openai_key:
            try:
                r = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {openai_key}"},
                )
                if r.status_code == 200:
                    results["openai"] = {"active": True}
                else:
                    results["openai"] = {"active": False, "error": f"HTTP {r.status_code}"}
            except Exception as e:
                results["openai"] = {"active": False, "error": str(e)[:120]}

        # DeepInfra — no expone saldo vía API; validamos la key con /v1/me
        if deepinfra_key:
            try:
                r = await client.get(
                    "https://api.deepinfra.com/v1/me",
                    headers={"Authorization": f"Bearer {deepinfra_key}"},
                )
                if r.status_code == 200:
                    results["deepinfra"] = {"active": True}
                else:
                    results["deepinfra"] = {"active": False, "error": f"HTTP {r.status_code}"}
            except Exception as e:
                results["deepinfra"] = {"active": False, "error": str(e)[:120]}

    return results


# ── Duración de casos (backfill) ─────────────────────────────────────────────

@app.post("/casos/duracion/backfill", dependencies=[Depends(verificar_key)])
def iniciar_backfill_duracion(job_id: Optional[int] = None, dry_run: bool = False):
    """
    Inicia el cálculo de duración para todos los casos con sentencia que
    aún no tienen duracion_dias. Corre en background (no bloquea).
    job_id: si se especifica, solo procesa ese job; None = todos.
    """
    from worker.duration_worker import lanzar_backfill_async, estado_backfill
    estado = estado_backfill()
    if estado["corriendo"]:
        raise HTTPException(409, "El backfill de duración ya está corriendo")
    lanzar_backfill_async(job_id=job_id, dry_run=dry_run)
    return {"iniciado": True, "job_id": job_id, "dry_run": dry_run}


@app.get("/casos/duracion/estado", dependencies=[Depends(verificar_key)])
def estado_backfill_duracion():
    """Estado actual del backfill de duración (si está corriendo o el resultado del último)."""
    from worker.duration_worker import estado_backfill
    return estado_backfill()


# ── Listado de actores agrupados ─────────────────────────────────────────────

@app.get("/actores", dependencies=[Depends(verificar_key)])
def listar_actores(tipo: str = "", q: str = "", page: int = 1, per_page: int = 50):
    """
    Lista actores únicos con su tipo y conteo de causas.
    tipo: filtra por 'empresa' | 'persona_natural' | 'desconocido'.
    q:    búsqueda por nombre (contains, case-insensitive).
    """
    from worker.strapi_client import StrapiClient
    strapi = StrapiClient()
    todos = strapi.listar_actores_agrupados()

    if tipo:
        todos = [a for a in todos if a.get("tipo_actor") == tipo]
    if q:
        q_lower = q.lower()
        todos = [a for a in todos if q_lower in (a.get("actor_normalizado") or "").lower()]

    total = len(todos)
    start = (page - 1) * per_page
    pagina = todos[start: start + per_page]
    return {"total": total, "page": page, "per_page": per_page, "actores": pagina}


@app.get("/actores/{actor_nombre}/causas", dependencies=[Depends(verificar_key)])
def causas_de_actor(actor_nombre: str, page: int = 1, per_page: int = 25):
    """Devuelve causas de un actor normalizado específico."""
    from worker.strapi_client import StrapiClient
    import urllib.parse
    nombre = urllib.parse.unquote(actor_nombre)
    strapi = StrapiClient()
    result = strapi.listar_causas_actor(nombre, page=page, page_size=per_page)
    causas = [
        {
            "juicio": (item.get("attributes", item)).get("juicio", ""),
            "actor":  (item.get("attributes", item)).get("actor", ""),
            "resumen": (item.get("attributes", item)).get("resumen_del_caso", ""),
        }
        for item in result.get("data", [])
    ]
    pag = result.get("meta", {}).get("pagination", {})
    return {
        "total": pag.get("total", len(causas)),
        "page": page,
        "per_page": per_page,
        "causas": causas,
    }


# ── Clasificación de actores (backfill) ──────────────────────────────────────

@app.post("/actores/backfill", dependencies=[Depends(verificar_key)])
def iniciar_backfill_actores(dry_run: bool = False):
    """
    Inicia la clasificación de actores (empresa / persona_natural) para todas las
    causas en Strapi que tienen actor pero no tipo_actor. Corre en background.
    """
    from worker.actor_worker import lanzar_backfill_async, estado_backfill
    estado = estado_backfill()
    if estado["corriendo"]:
        raise HTTPException(409, "El backfill de actores ya está corriendo")
    lanzar_backfill_async(dry_run=dry_run)
    return {"iniciado": True, "dry_run": dry_run}


@app.get("/actores/backfill/estado", dependencies=[Depends(verificar_key)])
def estado_backfill_actores():
    """Estado del backfill de clasificación de actores."""
    from worker.actor_worker import estado_backfill
    return estado_backfill()


# ── Registro global de casos ──────────────────────────────────────────────────

@app.get("/casos", dependencies=[Depends(verificar_key)])
def listar_casos_global(
    page: int = 1,
    per_page: int = 50,
    tribunal: str = "",
    año: str = "",
    estado: str = "",
    actor: str = "",
    demandado: str = "",
    sort: str = "procesado_en",
    dir: str = "desc",
):
    """Registro global de todos los casos procesados, con paginación, filtros y orden."""
    return jm.listar_casos_global(page=page, per_page=per_page,
                                   tribunal=tribunal, año=año, estado=estado,
                                   actor=actor, demandado=demandado,
                                   sort=sort, dir=dir)


@app.get("/casos/stats", dependencies=[Depends(verificar_key)])
def stats_casos():
    """Estadísticas agregadas de todos los casos registrados."""
    return jm.stats_casos()


# ── Log de casos por job ──────────────────────────────────────────────────────

@app.get("/jobs/{job_id}/procesando", dependencies=[Depends(verificar_key)])
def get_procesando(job_id: int):
    """Devuelve la causa que se está procesando actualmente en este job, o {} si ninguna."""
    procesando = jm.get_procesando_actual(job_id)
    return procesando or {}


@app.get("/jobs/{job_id}/casos", dependencies=[Depends(verificar_key)])
def listar_casos(job_id: int, sin_sentencia: bool = False):
    """
    Devuelve el registro oficial de causas procesadas en un job.
    sin_sentencia=true → solo casos sin sentencia (omitidos: abandono, en trámite, etc.).
    """
    job = jm.obtener_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    return jm.listar_casos_job(job_id, sin_sentencia=sin_sentencia)


# ── Subidas pendientes ────────────────────────────────────────────────────────

@app.get("/pending-uploads", dependencies=[Depends(verificar_key)])
def listar_pendientes():
    """Lista las extracciones cuya subida a Strapi falló y están pendientes de reintento."""
    return jm.listar_pendientes()


@app.post("/pending-uploads/retry", dependencies=[Depends(verificar_key)])
def reintentar_pendientes():
    """Reintenta subir a Strapi todas las entradas pendientes."""
    return jm.reintentar_pendientes()


# ── Logs persistentes por job ─────────────────────────────────────────────────

@app.get("/jobs/{job_id}/checklist", dependencies=[Depends(verificar_key)])
def checklist_job(job_id: int, campo: Optional[str] = None):
    """
    Checklist de campos faltantes en causas con sentencia del job.
    ?campo=cuantia → filtra solo causas donde cuantía está faltando.

    Campos posibles: cuantia, tipo_accion, decision_final, objeto,
    jurisprudencia, categorizacion, fecha_emision, otros_actos, pruebas, abogados.
    """
    job = jm.obtener_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    return jm.listar_checklist(job_id, campo=campo)


@app.get("/jobs/{job_id}/logs", dependencies=[Depends(verificar_key)])
def listar_logs_job(job_id: int, nivel: Optional[str] = None, limit: int = 100, offset: int = 0):
    """
    Devuelve el historial de logs persistidos de un job (más recientes
    primero), paginado con limit/offset. nivel: filtra por INFO | WARN | ERROR.
    """
    job = jm.obtener_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    return jm.listar_logs(job_id, nivel=nivel, limit=limit, offset=offset)


# ── Backfill por lote ─────────────────────────────────────────────────────────
# A diferencia de /jobs (que itera un rango tribunal+año+desde..hasta), esto
# procesa un LOTE de tamaño fijo de causas con un campo faltante — sin elegir
# cuáles, siempre las próximas priorizando las nunca intentadas. Para seguir,
# se lanza otra corrida: las ya resueltas no vuelven a aparecer en la cola.

@app.get("/backfill/estimar", dependencies=[Depends(verificar_key)])
def estimar_backfill(campo: str, cantidad: int = 100, modo: str = "completar"):
    """
    Estima cuánto tardaría un lote de `cantidad` causas ANTES de
    lanzarlo, en base al promedio real de corridas anteriores del mismo
    campo. Sin historial (menos de 5 causas procesadas), confiable=False.

    modo="completar" (default): solo causas donde el campo falta.
    modo="sobrescribir": todo el historial con sentencia, tenga o no el dato.
    """
    from worker.backfill_worker import _HANDLERS
    if campo not in jm._CAMPOS_FALTANTES:
        raise HTTPException(400, f"Campo inválido. Opciones: {jm._CAMPOS_FALTANTES}")
    if campo not in _HANDLERS:
        raise HTTPException(400, f"Todavía no hay backfill implementado para '{campo}'. Disponibles: {list(_HANDLERS)}")
    if modo not in jm._MODOS_BACKFILL:
        raise HTTPException(400, f"Modo inválido. Opciones: {jm._MODOS_BACKFILL}")

    pendientes_totales = jm.contar_pendientes_backfill(campo, modo=modo)
    estimado = jm.estimar_tiempo_backfill(campo, min(cantidad, pendientes_totales))
    return {
        "campo": campo,
        "modo": modo,
        "cantidad_solicitada": cantidad,
        "pendientes_totales": pendientes_totales,
        **estimado,
    }


@app.post("/backfill", dependencies=[Depends(verificar_key)])
def crear_backfill(body: NuevoBackfill):
    from worker.backfill_worker import _HANDLERS
    if body.campo not in jm._CAMPOS_FALTANTES:
        raise HTTPException(400, f"Campo inválido. Opciones: {jm._CAMPOS_FALTANTES}")
    if body.campo not in _HANDLERS:
        raise HTTPException(400, f"Todavía no hay backfill implementado para '{body.campo}'. Disponibles: {list(_HANDLERS)}")
    if body.lote <= 0 or body.lote > 1000:
        raise HTTPException(400, "lote debe estar entre 1 y 1000")
    if body.modo not in jm._MODOS_BACKFILL:
        raise HTTPException(400, f"Modo inválido. Opciones: {jm._MODOS_BACKFILL}")

    job = jm.crear_job(
        tribunal="-", año="0000", desde=0, hasta=None, dry_run=False,
        modelo="qwen-vision",
        iniciado_por=body.iniciado_por,
        workers=f"backfill:{body.campo}:{body.lote}:{body.modo}",
        categoria="backfill",
    )
    return job


@app.post("/embeddings", dependencies=[Depends(verificar_key)])
def crear_job_embeddings(body: NuevoEmbedding):
    """Lanza el (re)embebido de secciones en causa_section_embeddings.

    Va en la categoría 'extraccion' a propósito: comparte los mismos 4 slots
    que las extracciones, así que si están ocupados el job espera en
    'pendiente' en vez de sumarse como un quinto proceso en paralelo.
    """
    if body.modo not in ("faltantes", "todo"):
        raise HTTPException(400, "modo debe ser 'faltantes' o 'todo'")
    job = jm.crear_job(
        tribunal="-", año="0000", desde=0, hasta=None, dry_run=False,
        modelo="bge-m3",
        iniciado_por=body.iniciado_por,
        workers=f"embeddings:{body.modo}",
        categoria="extraccion",
    )
    return job


@app.post("/briefs", dependencies=[Depends(verificar_key)])
def crear_job_brief(body: NuevoBrief):
    """Genera los briefs doctrinales (OpenAI). Mismos 4 slots que la extracción."""
    validas = ("", "jueces", "tribunales", "cnj", "abogados", "entidades", "actores")
    if body.seccion not in validas:
        raise HTTPException(400, f"seccion inválida. Opciones: {validas[1:]} o vacío")
    job = jm.crear_job(
        tribunal="-", año="0000", desde=0, hasta=None, dry_run=False,
        modelo=os.environ.get("BRIEF_MODEL", "gpt-4o-mini"),
        iniciado_por=body.iniciado_por,
        workers=f"brief:{body.seccion}" if body.seccion else "brief",
        categoria="extraccion",
    )
    return job


@app.post("/revision", dependencies=[Depends(verificar_key)])
def crear_job_revision(body: NuevaRevision):
    """Revisa novedades en SATJE y encola solo lo que falta.

    Es el reemplazo barato del barrido completo: ese recorre 17.116 números y
    reprocesa todo, esto consulta lo que puede moverse y deja el trabajo
    pesado para las causas que de verdad cambiaron.

    Con particiones > 1 crea varios jobs, cada uno con su grupo de
    tribunales, para que corran en paralelo en los 4 slots.
    """
    from worker.revision_worker import repartir_tribunales
    if body.año and not (body.año.isdigit() and len(body.año) == 4):
        raise HTTPException(400, "año debe ser de 4 dígitos")
    if body.modo not in ("diaria", "completa"):
        raise HTTPException(400, "modo debe ser 'diaria' o 'completa'")
    if not 1 <= body.particiones <= 6:
        raise HTTPException(400, "particiones debe estar entre 1 y 6")

    grupos = repartir_tribunales(body.particiones)
    jobs = []
    for i, grupo in enumerate(grupos, 1):
        sufijo = f":{body.año}:{body.modo}" + (":descarga" if body.descarga else "")
        if len(grupos) > 1:
            sufijo += ":t=" + ",".join(grupo)
        jobs.append(jm.crear_job(
            tribunal="-", año=body.año or "0000", desde=0, hasta=None,
            dry_run=False, modelo="-",
            iniciado_por=body.iniciado_por or f"revision {i}/{len(grupos)}",
            workers=f"revision{sufijo}",
            categoria="extraccion",
        ))
    return {"jobs": jobs, "grupos": grupos}


# ── Prompts versionados ─────────────────────────────────────────────────────
# Editables desde la webapp sin tocar código — worker_prompts guarda el
# historial completo; el extractor siempre lee la versión "activa" (con
# hasta ~20s de cache en memoria, ver resolucion/ai/_cliente.py::cargar_prompt).

@app.get("/prompts", dependencies=[Depends(verificar_key)])
def listar_prompts():
    """Un renglón por prompt, con el contenido de su versión activa."""
    return jm.listar_prompts()


@app.get("/prompts/{nombre}/versiones", dependencies=[Depends(verificar_key)])
def listar_versiones_prompt(nombre: str):
    versiones = jm.listar_versiones_prompt(nombre)
    if not versiones:
        raise HTTPException(404, f"Prompt '{nombre}' no encontrado")
    return versiones


@app.post("/prompts/{nombre}", dependencies=[Depends(verificar_key)])
def guardar_prompt(nombre: str, body: GuardarPrompt):
    if nombre not in jm.PROMPTS_EDITABLES:
        raise HTTPException(400, f"Prompt inválido. Opciones: {jm.PROMPTS_EDITABLES}")
    if body.modo not in ("sobrescribir", "nueva_version"):
        raise HTTPException(400, "modo debe ser 'sobrescribir' o 'nueva_version'")
    if not body.contenido.strip():
        raise HTTPException(400, "El contenido no puede estar vacío")
    try:
        return jm.guardar_prompt(
            nombre, body.contenido, modo=body.modo,
            nota=body.nota, creado_por=body.creado_por,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/prompts/{nombre}/activar/{version}", dependencies=[Depends(verificar_key)])
def activar_version_prompt(nombre: str, version: int):
    if nombre not in jm.PROMPTS_EDITABLES:
        raise HTTPException(400, f"Prompt inválido. Opciones: {jm.PROMPTS_EDITABLES}")
    ok = jm.activar_version_prompt(nombre, version)
    if not ok:
        raise HTTPException(404, f"No existe la versión {version} de '{nombre}'")
    return {"ok": True, "nombre": nombre, "version_activa": version}


# ── Schedules (cron) ──────────────────────────────────────────────────────────

@app.get("/schedules", dependencies=[Depends(verificar_key)])
def listar_schedules():
    from worker import scheduler as sched
    return sched.listar_schedules()


@app.post("/schedules", dependencies=[Depends(verificar_key)])
def crear_schedule(body: NuevoSchedule):
    from worker import scheduler as sched
    # tribunal/año solo aplican a la extracción por barrido. Un schedule de
    # embeddings, brief o revisión no tiene tribunal, y validarlo lo rechazaría.
    if not body.workers:
        _validar_tribunal(body.tribunal)
        _validar_año(body.año)
    return sched.crear_schedule(
        nombre    = body.nombre,
        tribunal  = body.tribunal,
        año       = body.año,
        desde     = body.desde,
        hasta     = body.hasta,
        modelo    = body.modelo,
        cron_expr = body.cron_expr,
        workers   = body.workers,
        categoria = body.categoria,
    )


@app.put("/schedules/{schedule_id}/toggle", dependencies=[Depends(verificar_key)])
def toggle_schedule(schedule_id: int, activo: bool):
    from worker import scheduler as sched
    try:
        return sched.toggle_schedule(schedule_id, activo)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/schedules/{schedule_id}", dependencies=[Depends(verificar_key)])
def eliminar_schedule(schedule_id: int):
    from worker import scheduler as sched
    sched.eliminar_schedule(schedule_id)
    return {"ok": True}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, default=str)}\n\n"


from config.config import TRIBUNALES

def _validar_tribunal(tribunal: str):
    if tribunal not in TRIBUNALES:
        raise HTTPException(400, f"Tribunal '{tribunal}' no reconocido. "
                            f"Válidos: {list(TRIBUNALES.keys())}")


def _validar_año(año: str):
    if not (año.isdigit() and len(año) == 4):
        raise HTTPException(400, f"Año '{año}' inválido. Debe ser 4 dígitos (ej. 2019)")


# ── Punto de entrada ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
