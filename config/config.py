"""
Configuracion central del extractor de sentencias (materia en config/materia.py).

Este es el unico lugar donde viven las constantes del sistema. Si algo
cambia (un tribunal nuevo, una pausa distinta, otro modelo de GPT, etc.)
se modifica AQUI y se aplica en todo el proyecto.

Sub-modulos del proyecto que importan de aqui:
  - apertura/extractor_simple.py
  - resolucion/extractor.py, resolucion/gpt.py, resolucion/gpt_cnj.py, resolucion/reprocesar.py
  - gui/inspector.py
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Rutas del proyecto ──────────────────────────────────────────────────────
# Raiz del proyecto (un nivel arriba de este archivo, que vive en config/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROMPTS_DIR     = PROJECT_ROOT / "prompts"
RESULTADOS_DIR  = PROJECT_ROOT / "resultados"
DOCS_DIR        = PROJECT_ROOT / "docs"
LAUNCHERS_DIR   = PROJECT_ROOT / "launchers"

# Ejemplos few-shot que se inyectan en los prompts del modelo. Una corrida los
# relee al importar gpt.py / gpt_cnj.py, así que el cliente puede agregar /
# quitar ejemplos sin tocar código — basta con dropearlos como .md/.txt en
# la carpeta correspondiente y relanzar el bot.
EJEMPLOS_DIR          = PROJECT_ROOT / "ejemplos"
EJEMPLOS_TRIBUNAL_DIR = EJEMPLOS_DIR / "tribunal"   # se inyectan en el Prompt 2
EJEMPLOS_CORTE_DIR    = EJEMPLOS_DIR / "corte"      # se inyectan en el Prompt 5 (CNJ)

# Carpeta donde viven todos los Excels (un Sentencias-{ano}.xlsx por año).
EXCEL_DIR = RESULTADOS_DIR / "excel"

# Excels acumulados (fuente unica de verdad por workflow).
# RESOLUCION: ya no es un único archivo, sino uno por año (Sentencias-2020.xlsx,
# Sentencias-2021.xlsx, ...). Dentro de cada uno hay una hoja por tribunal
# (Guayas, Pichincha, Cuenca, etc.). EXCEL_RESOLUCION queda como default
# (año por defecto) para compat con tests y código legacy — para la ruta real
# de cada corrida, usá excel_resolucion(ano).
EXCEL_RESOLUCION = EXCEL_DIR / "Sentencias-2020.xlsx"
EXCEL_APERTURA   = EXCEL_DIR / "apertura_demandas.xlsx"

# Carpetas raiz por tipo de output. Dentro van subcarpetas por corrida.
PDFS_RESOLUCION_DIR  = RESULTADOS_DIR / "pdfs"  / "resolucion"
PDFS_APERTURA_DIR    = RESULTADOS_DIR / "pdfs"  / "apertura"
WORDS_RESOLUCION_DIR = RESULTADOS_DIR / "words" / "resolucion"
WORDS_APERTURA_DIR   = RESULTADOS_DIR / "words" / "apertura"

# Logs (corridas nuevas van directo aqui; las historicas viven en historicos/)
LOGS_RESOLUCION_DIR = RESULTADOS_DIR / "logs" / "resolucion"
LOGS_APERTURA_DIR   = RESULTADOS_DIR / "logs" / "apertura"

# Marcador para datos previos al refactor
HISTORICO_PRE_REFACTOR = "historico_pre-refactor"


# ─── Judicaturas, instancias y materia ───────────────────────────────────────
# Todo lo que depende de la materia jurídica vive en config/materia.py.
# Aquí se re-exportan los nombres históricos para no tocar el resto del código.
from config import materia as MATERIA  # noqa: E402

TRIBUNALES      = MATERIA.JUDICATURAS
TRIBUNALES_SLUG = MATERIA.JUDICATURAS_SLUG
ID_CNJ          = MATERIA.ID_CNJ

# Materias que el sistema procesa (idMateria del SATJE)
MATERIAS_ACEPTADAS     = MATERIA.IDS_MATERIA_SATJE
ETIQUETA_OTRA_MATERIA  = MATERIA.ETIQUETA_OTRA_MATERIA
# Compat histórica (tributario): 36 = Contencioso Tributario, 37 = Administrativo
MATERIA_TRIBUTARIO     = 36
MATERIA_ADMINISTRATIVO = 37


# ─── Parametros de scraping ──────────────────────────────────────────────────

# Cuantas causas no encontradas consecutivas antes de cortar la busqueda
MAX_VACIAS_RESOLUCION = 30
MAX_VACIAS_APERTURA   = 20

# Pausa entre llamadas a SATJE (segundos). Evita rate limits.
PAUSA_SEG = 1.5

# Cada cuantas causas procesadas se hace flush al Excel (guardado incremental)
GUARDAR_CADA_RESOLUCION = 10
GUARDAR_CADA_APERTURA   = 5

# Anos default por workflow (se pueden sobrescribir via flag --ano)
ANO_DEFAULT_RESOLUCION = "2020"
ANO_DEFAULT_APERTURA   = "2026"


# ─── API del SATJE (Funcion Judicial del Ecuador) ────────────────────────────
# Estos URLs viven en api.py — aqui se duplican como referencia.
# Si la API cambia de host, hay que actualizar AMBOS lugares.

URLS_SATJE = {
    "base_causas": "https://api.funcionjudicial.gob.ec/EXPEL-CONSULTA-CAUSAS-SERVICE/api/consulta-causas",
    "base_clex":   "https://api.funcionjudicial.gob.ec/EXPEL-CONSULTA-CAUSAS-CLEX-SERVICE/api/consulta-causas-clex",
    "base_doc":    "https://api.funcionjudicial.gob.ec/CJ-DOCUMENTO-SERVICE/api/document/query/hba",
}


# ─── OpenAI / GPT ────────────────────────────────────────────────────────────

GPT_MODELO_PRINCIPAL = "gpt-4o"
GPT_MODELO_AUX       = "gpt-4o-mini"   # extracciones JSON cortas, mas barato

# Limites de tokens de salida por prompt (cambiar aqui afecta calidad y costo).
#
# IMPORTANTE: "extraccion" se usa en DOS prompts:
#   - Prompt 2 tribunal (19 secciones)        en resolucion/gpt.py
#   - Prompt 5 CNJ casacion (14 secciones)    en resolucion/gpt_cnj.py
# Subir este valor afecta ambos.
#
# Historia: estaba en 4500 → 7000 → 8500. Subido a 16000 porque sentencias
# de 30-50 páginas necesitan ~12k-15k tokens de output para las 19 secciones
# completas. Con 8500 el modelo se cortaba a mitad del análisis. 16000 es el
# máximo de GPT-4o.
GPT_MAX_TOKENS = {
    # Prompt 1 pide MÁXIMO 500 palabras, y el tope de tokens NO es el mecanismo
    # para hacer cumplir ese límite: cortar la salida a la mitad no produce un
    # resumen más corto, produce un resumen incompleto guardado como si
    # estuviera bien.
    #
    # Se probó bajarlo a 850 (de convertir 500 palabras a ~750 tokens, que es la
    # relación del INGLÉS) y truncaba las sentencias densas: medido sobre los
    # 3.646 resúmenes reales del corpus, en español salen ~2 tokens por palabra,
    # así que 500 palabras son ~1.000 tokens. El máximo observado —528 palabras,
    # ~1.033 tokens— no entraba.
    #
    # 2000 deja margen para cualquier resumen que respete el prompt. Si Qwen se
    # pasa de largo, se corrige en el prompt, no acá.
    "resumen":         2000,    # Prompt 1: resumen ejecutivo (máx ~500 palabras)
    "extraccion":      16000,   # Prompts 2 (tribunal) y 5 (CNJ): 14-19 secciones largas
    "jurisprudencia":  10000,   # Prompts 3 y 6 (subido de 4000 y 6000: la doctrina citada larga truncaba; puerta civil v7)
    # Subido de 500 — se truncaba seguido en la práctica: "Problemática
    # Principal" es texto libre + hasta 4 categorías/subcategorías repetidas,
    # 500 no alcanzaba (visto en logs reales: "truncado — max_tokens=500").
    "clasificacion":   1500,    # Prompt 4: clasificacion catalogo
    "tipos_defecto":   1000,    # Prompt 7: catalogo interno de vicios (solo codigos, sin prosa)
    # Prompt 8: codigos por lado + 2 citas textuales (POR_QUE / EVIDENCIA_AUSENTE).
    # 800 alcanza: medido sobre 100 clasificaciones reales, el maximo observado
    # fue ~430 tokens y el promedio ~250.
    "tipo_prueba":     800,
    # Prompt 9: una sola cita textual o "NINGUNA". 400 sobra.
    "evidencia_ausente": 400,
    # Prompt 10: JSON de 9 campos, casi todos frases cortas.
    "extraccion_estructurada": 900,
    "json_extract":    300,     # Llamada auxiliar GPT-mini
    "default":         4000,
}

# Limite de caracteres del PDF que se mandan a GPT (evita exceder contexto).
#
# GPT-4o (128k ctx): 250k chars (~62k tok input) + prompts (~13k) + output (16k) = ~91k.
# Subir más de 300k arriesga el límite de 128k de GPT-4o.
# Cubre sentencias densas de hasta ~50 páginas (~5000 chars/página).
GPT_MAX_CHARS_INPUT = 250_000

# Limite para modelos Qwen vía DeepInfra (256k token context window).
# 800k chars ≈ 200k tokens de input + prompts (~13k) + output (16k) = ~229k/256k.
# Cubre sentencias de hasta ~160 páginas — más que suficiente para el corpus actual.
QWEN_MAX_CHARS_INPUT = 800_000

GPT_TEMPERATURE = 0   # fidelidad maxima, sin creatividad

# Pausa entre llamadas a GPT (segundos) para no saturar rate limit
GPT_PAUSA_SEG = 3


# ─── Google Drive ────────────────────────────────────────────────────────────

CARPETA_DRIVE_RESOLUCION = MATERIA.CARPETA_DRIVE_RESOLUCION
CARPETA_DRIVE_APERTURA   = MATERIA.CARPETA_DRIVE_APERTURA


# ─── Excel — un archivo por año, una hoja por tribunal ───────────────────────
#
# Estructura nueva (Junio 2026):
#   resultados/excel/
#     Sentencias-2020.xlsx
#       ├── Hoja: Guayas
#       ├── Hoja: Pichincha
#       ├── Hoja: Cuenca
#       ├── ...
#     Sentencias-2021.xlsx
#       ├── Hoja: Guayas
#       ├── ...
#     ...
#
# Esta separación facilita el revisado al cliente: cada año un archivo, dentro
# las hojas alfabéticas por tribunal. Antes había un solo `resolucion_sentencias.xlsx`
# con hojas SENTENCIAS-{año} que mezclaba todo.

def excel_resolucion(ano: str | int) -> Path:
    """Devuelve la ruta al Sentencias-{ano}.xlsx para el año dado."""
    return EXCEL_DIR / f"Sentencias-{ano}.xlsx"


def hoja_resolucion(tribunal_prefijo: str) -> str:
    """Devuelve el nombre de hoja del Excel para un tribunal dado.
    Usa el slug capitalizado (ej. "09501" -> "Guayas"). Cae en el prefijo
    si no encuentra el slug, para que no rompamos en tribunales nuevos."""
    slug = TRIBUNALES_SLUG.get(tribunal_prefijo, tribunal_prefijo)
    return slug.capitalize()


# Apertura (demandas): seguimos con un único archivo por ahora.
HOJA_EXCEL_PREFIJO_APERTURA = "DEMANDAS"
HOJA_EXCEL_APERTURA = f"{HOJA_EXCEL_PREFIJO_APERTURA} {ANO_DEFAULT_APERTURA}"

# Colores para celdas del Excel (segun "Estado de Juicio")
COLORES_ESTADO = {
    "Archivado":                   "C6EFCE",   # verde
    MATERIA.ESTADO_EN_CNJ:         "FFEB9C",   # amarillo
    MATERIA.ESTADO_EN_APELACION:   "FFF2CC",   # amarillo claro
    MATERIA.ESTADO_TERMINADO_APELACION: "C6EFCE",   # verde (terminó)
    MATERIA.ESTADO_EN_PRIMERA:     "FFFFFF",   # blanco
    "Sin resultados":              "F4CCCC",   # rojo claro
    MATERIA.ETIQUETA_OTRA_MATERIA: "D9D9D9",   # gris
}


# ─── Estados explicitos (no son COGEP, son estados "de tramite") ─────────────
# Valores que puede tener Resultado tribunal cuando ningun motivo COGEP matchea

RESULTADO_EN_TRAMITE      = "En tramite (sin sentencia aun)"
RESULTADO_SIN_ACTUACIONES = "Sin actuaciones registradas"

# Resultado en Corte (analogos al lado CNJ)
RESULTADO_CNJ_EN_TRAMITE = "Casación presentada."
# Resultado en apelación (Corte Provincial) — solo materias con ese nivel
RESULTADO_APELACION_EN_TRAMITE = "Apelación presentada."
RESULTADO_APELACION_CON_SENTENCIA = "Con sentencia de apelación."


# ─── Estados de Completitud del analisis ─────────────────────────────────────
# Nueva columna que indica si la fila es confiable para reportar o requiere
# revision manual. Permite distinguir "sin sentencia" (esperable) de "GPT
# extrajo cosas que no aparecen en el PDF" (revisar).

COMPLETITUD_COMPLETO     = "Completo"                # GPT extrajo y validacion paso
COMPLETITUD_PARCIAL      = "Parcial - revisar"       # GPT corrio pero hubo problemas
COMPLETITUD_PDF_SIN_GPT  = "PDF sin analisis"        # se bajo PDF pero GPT desactivado
COMPLETITUD_PENDIENTE    = "Pendiente (en tramite)"  # causa activa, sin sentencia
COMPLETITUD_SIN_ACTUS    = "Sin actuaciones"         # causa sin movimientos
COMPLETITUD_NO_APLICABLE = "No aplicable"            # no es de la materia
COMPLETITUD_ERROR_GPT    = "Error GPT"               # llamada a GPT fallo
COMPLETITUD_ARCHIVADO    = "Archivado (COGEP)"       # terminada por motivo COGEP no-sentencia


# ─── Validaciones post-GPT ───────────────────────────────────────────────────
# Vocabulario cerrado: si GPT devuelve algo fuera de estas listas, sospechoso

DECISIONES_VALIDAS = list(MATERIA.DECISIONES_PRIMERA_INSTANCIA)
TIPOS_ACCION_VALIDOS = list(MATERIA.TIPOS_ACCION)

# Si la cuantia tiene >0 digitos y no es "no consta" -> valida
# (regex simple, no necesita lista)

# Umbral minimo de chars para considerar un resumen aceptable
MIN_CHARS_RESUMEN = 100


# ─── Helpers ─────────────────────────────────────────────────────────────────

def cargar_api_key() -> str:
    """Carga la API key de OpenAI desde config/.env o desde una variable
    de entorno. Devuelve string vacio si no encuentra nada.

    Tolerante con formato:
      - Acepta espacios alrededor del "=" (OPENAI_API_KEY = sk-proj-...)
      - Acepta comillas simples o dobles alrededor del valor
      - Ignora líneas vacías y comentarios (# ...)
      - Acepta cualquier orden de líneas en el .env
      - Advierte si la key no tiene el prefijo esperado (sk-...), que es la
        causa más frecuente de "401 Incorrect API key".
    """
    key = ""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() != "OPENAI_API_KEY":
                    continue
                v = v.strip()
                # Quitar comillas si las hay
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1].strip()
                if v and v != "pega-tu-key-aqui":
                    key = v
                    break
    if not key:
        key = os.environ.get("OPENAI_API_KEY", "").strip()

    # Advertencia temprana si la key no tiene el prefijo esperado.
    # Las keys de OpenAI siempre empiezan con "sk-" (sk-proj-, sk-svcacct-, sk-).
    # Si no, es casi seguro que se copió incompleta o se pegó mal.
    if key and not key.startswith("sk-"):
        print(
            "  [WARN cargar_api_key] La API Key NO empieza con 'sk-'. "
            "Probablemente la copiaste incompleta desde OpenAI.\n"
            "  Las keys validas empiezan con 'sk-proj-...', 'sk-svcacct-...' o 'sk-...'.\n"
            "  Volve a copiarla COMPLETA desde https://platform.openai.com/api-keys"
        )
    return key


def cargar_proxy_brightdata() -> tuple:
    """Carga RESIDENTIAL_PROXY_URL y RESIDENTIAL_PROXY_INSECURE desde config/.env
    o variables de entorno. Devuelve (url, insecure) o ('', False) si no hay."""
    url = ""
    insecure = False
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1].strip()
                if k == "RESIDENTIAL_PROXY_URL" and v:
                    url = v
                elif k == "RESIDENTIAL_PROXY_INSECURE" and v == "1":
                    insecure = True
    if not url:
        # BDATA_DATACENTER_PROXY_URL es el nombre nuevo y el que describe lo
        # que hay: zona de datacenter de Bright Data con IPs de Ecuador, no un
        # proxy residencial. El viejo se mantiene para los .env existentes.
        url = (os.environ.get("BDATA_DATACENTER_PROXY_URL", "").strip()
               or os.environ.get("RESIDENTIAL_PROXY_URL", "").strip())
    if not insecure:
        insecure = (os.environ.get("BDATA_DATACENTER_PROXY_INSECURE", "").strip() == "1"
                    or os.environ.get("RESIDENTIAL_PROXY_INSECURE", "").strip() == "1")
    return url, insecure


def nombre_corrida(tribunal_prefijo: str,
                   desde: int,
                   hasta: Optional[int] = None,
                   ahora: Optional[datetime] = None) -> str:
    """Genera el identificador de carpeta para una corrida de scraping.

    Formato: YYYYMMDD_HHMM_<tribunal>_<desde>-<hasta>
    Ejemplo: 20260527_1345_cuenca_001-080

    Si `hasta` es None, se usa el sufijo `_<desde>-fin`.
    """
    ahora = ahora or datetime.now()
    slug  = TRIBUNALES_SLUG.get(tribunal_prefijo, tribunal_prefijo)
    if hasta is None:
        sufijo_rango = f"{desde:03d}-fin"
    else:
        sufijo_rango = f"{desde:03d}-{hasta:03d}"
    return f"{ahora.strftime('%Y%m%d_%H%M')}_{slug}_{sufijo_rango}"


def asegurar_carpetas_resultados() -> None:
    """Crea (idempotente) toda la jerarquia de `resultados/` si no existe."""
    for p in [
        RESULTADOS_DIR / "excel",
        PDFS_RESOLUCION_DIR, PDFS_APERTURA_DIR,
        WORDS_RESOLUCION_DIR, WORDS_APERTURA_DIR,
        LOGS_RESOLUCION_DIR, LOGS_APERTURA_DIR,
    ]:
        p.mkdir(parents=True, exist_ok=True)
