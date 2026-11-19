"""
Cliente de IA de bajo nivel: OpenAI y DeepInfra/Qwen.

Funciones puras de llamada HTTP y utilidades compartidas
(extracción de texto PDF, generación de Word).
No tiene lógica de prompts ni de extracción de campos.
"""

import io
import os
import re
from pathlib import Path

# Rutas y constantes de config
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from config.config import (
    PROMPTS_DIR,
    GPT_MODELO_PRINCIPAL,
    GPT_MAX_TOKENS,
    GPT_MAX_CHARS_INPUT,
    QWEN_MAX_CHARS_INPUT,
    GPT_TEMPERATURE,
    GPT_PAUSA_SEG,
    EJEMPLOS_TRIBUNAL_DIR,
    EJEMPLOS_CORTE_DIR,
    cargar_api_key as _cargar_api_key_openai_cfg,
    DECISIONES_VALIDAS,
    TIPOS_ACCION_VALIDOS,
    MIN_CHARS_RESUMEN,
)
from config.pdf_utils import limpiar_texto_pypdf, cargar_ejemplos_few_shot
from worker import logger as log

DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELO_QWEN = "qwen/qwen3-235b-a22b-2507"

# Orden de proveedores para el mismo modelo detrás de OpenRouter.
#
# DeepInfra queda ÚLTIMO a propósito, no primero: OpenRouter deja de poder
# hacer failover en cuanto el proveedor elegido empieza a responder algo,
# aunque sea lento — y el fallo típico de DeepInfra es justo ese (acepta la
# llamada y se cuelga generando, no un error duro e inmediato). Ponerlo
# primero no garantiza que salte rápido al siguiente; priorizamos los de
# mejor uptime medido aunque cuesten un poco más. allow_fallbacks=False para
# que si estos fallan todos, la request falle limpio en vez de caer en un
# proveedor sin validar (venice/nebius, <93% de uptime).
#
# Tres flujos seleccionables (mismo modelo, mismas pesas — solo cambia por
# dónde arranca la búsqueda) para poder comparar en la práctica si conviene
# más arrancar por Alibaba (mejor uptime medido), por Novita (mismo precio
# que DeepInfra pero mejor throughput/uptime reciente) o por GMICloud (el
# más barato de la lista y el único de los tres que soporta cache de prompt).
#
# Precios medidos contra la API de OpenRouter (por 1M tokens, input/output):
#   GMICloud  $0.0875 / $0.3500   + cache_read $0.0175
#   Alibaba   $0.1495 / $0.5980   (sin cache)
#   Novita    $0.0900 / $0.5800   (sin cache)
# Sobre el pipeline completo eso da ~$0.024/causa por Alibaba vs ~$0.014 por
# GMICloud (-41%). GMICloud NO tiene uptime medido todavía en este proyecto:
# por eso entra como flujo opcional y con Alibaba de respaldo inmediato en
# segundo lugar, no como default.
OPENROUTER_ORDEN_ALIBABA_PRIMERO = [
    "alibaba", "novita", "parasail", "atlas-cloud", "streamlake", "google-vertex", "deepinfra",
]
OPENROUTER_ORDEN_NOVITA_PRIMERO = [
    "novita", "alibaba", "parasail", "atlas-cloud", "streamlake", "google-vertex", "deepinfra",
]
# Mismo patrón que el flujo Novita: el elegido primero y Alibaba pisándole los
# talones como red de seguridad (además se lleva el reintento extra de
# _PROVEEDOR_DOBLE_INTENTO esté donde esté en la lista).
OPENROUTER_ORDEN_GMICLOUD_PRIMERO = [
    "gmicloud", "alibaba", "novita", "parasail", "atlas-cloud", "streamlake", "google-vertex", "deepinfra",
]
_ORDENES_OPENROUTER = {
    "alibaba":  OPENROUTER_ORDEN_ALIBABA_PRIMERO,
    "novita":   OPENROUTER_ORDEN_NOVITA_PRIMERO,
    "gmicloud": OPENROUTER_ORDEN_GMICLOUD_PRIMERO,
}
# Compat: default para cualquier código que todavía use el nombre viejo.
OPENROUTER_PROVEEDORES_PERMITIDOS = OPENROUTER_ORDEN_ALIBABA_PRIMERO


def resolver_orden_proveedores(nombre: str) -> list:
    """'alibaba' (default), 'novita' o 'gmicloud' — cuál va primero en la cadena."""
    return _ORDENES_OPENROUTER.get(nombre, OPENROUTER_ORDEN_ALIBABA_PRIMERO)


# Proveedor con mejor uptime medido de los dos que pueden ir primero — se le
# da un reintento extra dondequiera que esté en la lista (no solo cuando es
# el primero: en el flujo "novita", Alibaba va segundo y de todos modos
# merece las dos chances).
_PROVEEDOR_DOBLE_INTENTO = "alibaba"


def _calendario_avance(orden: list, n_intentos: int, proveedor_doble: str = _PROVEEDOR_DOBLE_INTENTO) -> list:
    """
    Índice de `orden` desde donde arrancar la cadena en cada intento —
    avanza un proveedor por intento fallido, salvo `proveedor_doble`
    (Alibaba), que consume dos intentos antes de avanzar más allá de él,
    esté donde esté en la lista.
    """
    calendario = []
    idx = 0
    usado_doble = False
    for _ in range(n_intentos):
        calendario.append(idx)
        if idx < len(orden) and orden[idx] == proveedor_doble and not usado_doble:
            usado_doble = True
        else:
            idx += 1
    return calendario

# Errores transitorios (rate limit / servidor saturado / red) — vale la pena
# reintentar con backoff. Otros errores (auth, bad request) no se reintentan.
REINTENTOS_TRANSITORIOS = 3
BACKOFF_BASE_SEG = 5

# Timeout por intento HTTP (no confundir con TIMEOUT_CAUSA_SEG, que es el
# límite total por causa). Sin esto, el SDK de OpenAI usa su default de 600s
# *y además* reintenta 2 veces por su cuenta antes de que nuestro propio
# retry-con-backoff se entere — un solo colgón podía comerse hasta 30 min.
# max_retries=0 en el cliente para que la única capa de retry sea la nuestra.
#
# 150s (antes 300s) — calibrado contra latencia real observada con Alibaba
# como proveedor principal: ningún prompt (ni el más pesado, Extracción de
# 19 secciones) tardó más de ~95s en corridas limpias. 300s dejaba que un
# solo colgón se comiera hasta ~20 min entre reintentos — desproporcionado
# frente a los ~2-5 min que tarda una causa completa cuando todo anda bien.
TIMEOUT_LLAMADA_SEG = 150


def _es_error_transitorio(e: Exception) -> bool:
    import openai
    return isinstance(e, (
        openai.RateLimitError,
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.InternalServerError,
    ))


# ── Claves de API ─────────────────────────────────────────────────────────────

def cargar_api_key_openai() -> str:
    return _cargar_api_key_openai_cfg()


def cargar_api_key_deepinfra() -> str:
    env_path = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
    if env_path.exists():
        for linea in env_path.read_text().splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, _, v = linea.partition("=")
            if k.strip() == "DEEPINFRA_API_KEY":
                return v.strip()
    return os.environ.get("DEEPINFRA_API_KEY", "").strip()


def cargar_api_key_openrouter() -> str:
    env_path = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
    if env_path.exists():
        for linea in env_path.read_text().splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, _, v = linea.partition("=")
            if k.strip() == "OPENROUTER_API_KEY":
                return v.strip()
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def es_modelo_qwen(modelo: str) -> bool:
    return "qwen" in modelo.lower()


# ── Prompts ───────────────────────────────────────────────────────────────────
#
# Prioridad: BD (worker_prompts, editable desde la webapp sin redeployar) →
# archivo (prompts/*.txt, fallback si el prompt nunca se editó o la BD no
# está disponible). Cache corto en memoria para no pegarle a la BD en cada
# llamada dentro de un mismo job — las ediciones tardan como mucho
# _PROMPT_CACHE_TTL segundos en verse reflejadas, sin necesidad de reiniciar
# el worker.

_PROMPT_CACHE: dict = {}
_PROMPT_CACHE_TTL = 20  # segundos


def cargar_prompt(nombre: str) -> str:
    import time as _time

    ahora = _time.time()
    cacheado = _PROMPT_CACHE.get(nombre)
    if cacheado and (ahora - cacheado[1]) < _PROMPT_CACHE_TTL:
        return cacheado[0]

    contenido = None
    try:
        from worker.job_manager import obtener_prompt_activo
        contenido = obtener_prompt_activo(nombre)
    except Exception:
        contenido = None  # BD no disponible — cae a archivo

    if contenido is None:
        ruta = PROMPTS_DIR / nombre
        if not ruta.exists():
            raise FileNotFoundError(
                f"Falta el archivo de prompt: {ruta}\n"
                "Verifica que la carpeta 'prompts/' esté completa."
            )
        contenido = ruta.read_text(encoding="utf-8").rstrip("\n") + "\n"

    _PROMPT_CACHE[nombre] = (contenido, ahora)
    return contenido


# ── Extracción de texto PDF ───────────────────────────────────────────────────

def extraer_texto_pdf(pdf_bytes: bytes, job_id: int = None, causa: str = None) -> str:
    """
    Extrae y limpia el texto de un PDF dado como bytes.
    Devuelve string vacío si falla (con log de error).
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        paginas = [(p.extract_text() or "") for p in reader.pages]
        return limpiar_texto_pypdf(paginas)
    except Exception as e:
        log.error(
            f"extraer_texto_pdf: {type(e).__name__}: {e} — "
            "PDF no legible; se intentará modo visión si está disponible",
            job_id=job_id, causa=causa,
        )
        return ""


# ── Llamadas HTTP ─────────────────────────────────────────────────────────────


# ── Datos personales: la ficha nunca transcribe números de cédula ─────────────
_RE_CEDULA = re.compile(
    r",?\s*(?:con\s+|portador[a]?\s+de\s+(?:la\s+)?)?"
    r"(?:c[ée]dula(?:\s+de\s+(?:identidad|ciudadan[ií]a))?|C\.\s?I\.?)"
    r"\s*(?:N\.?\s?[°ºo]\.?|No\.?|n[úu]mero|#)?\s*:?\s*\d{9,10}(?:-\d)?",
    re.IGNORECASE,
)


def quitar_cedulas(texto: str) -> str:
    """Elimina menciones de número de cédula (", con cédula de identidad #0801…").
    Los RUC de compañías se conservan (no van precedidos de 'cédula')."""
    if not texto:
        return texto
    return _RE_CEDULA.sub("", texto)


def llamar_openai(
    prompt: str,
    sistema: str,
    max_tokens: int,
    api_key: str,
    modelo: str = None,
    response_format: dict = None,
    imagenes_b64: list = None,
    job_id: int = None,
    causa: str = None,
) -> tuple:
    """
    Llama a OpenAI en modo texto o visión (si imagenes_b64 no está vacío).
    Retorna (texto, finish_reason). finish_reason: stop | length | content_filter | error.
    - length       → respuesta truncada por max_tokens, el campo puede estar incompleto
    - content_filter → OpenAI rechazó el prompt; llamar con sistema_neutro
    - error        → excepción de red/API; texto contiene el mensaje de error
    """
    import openai, time as _time
    client = openai.OpenAI(api_key=api_key, timeout=TIMEOUT_LLAMADA_SEG, max_retries=0)
    modelo_efectivo = modelo or GPT_MODELO_PRINCIPAL

    if imagenes_b64:
        contenido_user = [{"type": "text", "text": prompt}]
        for img in imagenes_b64:
            contenido_user.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img}", "detail": "high"},
            })
        mensajes = [
            {"role": "system", "content": sistema},
            {"role": "user",   "content": contenido_user},
        ]
    else:
        mensajes = [
            {"role": "system", "content": sistema},
            {"role": "user",   "content": prompt},
        ]

    kwargs = dict(
        model=modelo_efectivo,
        messages=mensajes,
        max_tokens=max_tokens,
        temperature=GPT_TEMPERATURE,
    )
    if response_format is not None:
        kwargs["response_format"] = response_format

    for intento in range(REINTENTOS_TRANSITORIOS + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            finish = choice.finish_reason or "stop"

            if getattr(choice.message, "refusal", None):
                log.warn(
                    f"OpenAI rechazó el prompt (refusal): {choice.message.refusal[:120]}",
                    job_id=job_id, causa=causa,
                )
                return "", "content_filter"

            texto = (choice.message.content or "").strip()
            if finish == "length":
                log.warn(
                    f"Respuesta OpenAI truncada — max_tokens={max_tokens} alcanzado; "
                    "el campo puede estar incompleto",
                    job_id=job_id, causa=causa,
                )
            return quitar_cedulas(texto), finish

        except Exception as e:
            if _es_error_transitorio(e) and intento < REINTENTOS_TRANSITORIOS:
                espera = BACKOFF_BASE_SEG * (2 ** intento)
                log.warn(
                    f"OpenAI {modelo_efectivo}: {type(e).__name__} "
                    f"(intento {intento + 1}/{REINTENTOS_TRANSITORIOS + 1}) — "
                    f"reintentando en {espera}s",
                    job_id=job_id, causa=causa,
                )
                _time.sleep(espera)
                continue
            log.error(
                f"OpenAI {modelo_efectivo}: {type(e).__name__}: {e}",
                job_id=job_id, causa=causa,
            )
            return f"[Error OpenAI: {e}]", "error"


def llamar_qwen(
    prompt: str,
    sistema: str,
    max_tokens: int,
    api_key: str,
    modelo: str,
    job_id: int = None,
    causa: str = None,
    orden_proveedores: list = None,
    temperatura: float = None,
) -> tuple:
    """
    Llama a Qwen vía OpenRouter (API OpenAI-compatible), que a su vez
    reparte entre varios proveedores del mismo modelo (DeepInfra, Alibaba,
    Novita, Parasail, etc.) — ver resolver_orden_proveedores(). DeepInfra solo
    mide ~96% de uptime para este modelo; pegarle directo dejaba causas
    enteras sin extracción cuando estaba lento/caído.
    Retorna (texto, finish_reason).
    - length → respuesta truncada
    - error  → excepción de red/API
    """
    import openai, time as _time
    client = openai.OpenAI(
        api_key=api_key, base_url=OPENROUTER_BASE_URL,
        timeout=TIMEOUT_LLAMADA_SEG, max_retries=0,
    )
    nombre_corto = modelo.split("/")[-1]
    orden = orden_proveedores or OPENROUTER_ORDEN_ALIBABA_PRIMERO
    n_intentos = REINTENTOS_TRANSITORIOS + 1
    calendario = _calendario_avance(orden, n_intentos)

    for intento in range(n_intentos):
        # Alibaba (mejor uptime medido) tiene dos intentos antes de que se
        # avance más allá de él, esté primero o segundo en la lista — el
        # resto de los proveedores se saltan apenas fallan una vez: si ya
        # se colgó, lo más probable es un pico de carga puntual, no un
        # error suelto, e insistir no suele ayudar. orden[-1:] como piso
        # si ya se probaron todos.
        orden_intento = orden[calendario[intento]:] or orden[-1:]
        try:
            resp = client.chat.completions.create(
                model=OPENROUTER_MODELO_QWEN,
                messages=[
                    {"role": "system", "content": sistema},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=GPT_TEMPERATURE if temperatura is None else temperatura,
                extra_body={
                    "provider": {
                        "order": orden_intento,
                        "allow_fallbacks": False,
                    },
                },
            )
            choice = resp.choices[0]
            finish = choice.finish_reason or "stop"
            texto = (choice.message.content or "").strip()
            if finish == "length":
                log.warn(
                    f"Qwen {nombre_corto} truncado — max_tokens={max_tokens} alcanzado",
                    job_id=job_id, causa=causa,
                )
            return quitar_cedulas(texto), finish
        except Exception as e:
            if _es_error_transitorio(e) and intento < REINTENTOS_TRANSITORIOS:
                espera = BACKOFF_BASE_SEG * (2 ** intento)
                log.warn(
                    f"Qwen {nombre_corto}: {type(e).__name__} "
                    f"(intento {intento + 1}/{REINTENTOS_TRANSITORIOS + 1}) — "
                    f"reintentando en {espera}s",
                    job_id=job_id, causa=causa,
                )
                _time.sleep(espera)
                continue
            log.error(
                f"Qwen {nombre_corto}: {type(e).__name__}: {e}",
                job_id=job_id, causa=causa,
            )
            return f"[Error Qwen: {e}]", "error"


def proporcion_lineas_repetidas(texto: str) -> float:
    """Proporción de líneas no vacías que repiten una línea anterior (0 a 1).
    Sirve para elegir, entre varias salidas truncadas, la que tiene menos
    bucle de repetición (la más larga suele ser justamente la del bucle)."""
    lineas = [l.strip() for l in (texto or "").splitlines() if l.strip()]
    if not lineas:
        return 1.0
    return 1 - len(set(lineas)) / len(lineas)


def elegir_salida_reintento(actual: str, nueva: str, fin_nueva: str) -> bool:
    """True si la salida del reintento debe reemplazar a la actual (truncada):
    siempre que la nueva no esté truncada; si también trunca, solo si tiene
    menos repetición."""
    if fin_nueva != "length":
        return True
    return proporcion_lineas_repetidas(nueva) < proporcion_lineas_repetidas(actual)


# ── Generación de Word ────────────────────────────────────────────────────────

import re as _re_docx

# Detecta líneas tipo título/etiqueta dentro del bloque de una sección
# ("14. Argumentos de la Parte Actora", "Fecha:", "Voto salvado:", "NORMATIVA
# APLICADA:") para resaltarlas en negrita — sin esto, el título de cada
# sub-sección se ve exactamente igual que el cuerpo del texto, lo que hace
# difícil auditar visualmente dónde termina una sub-sección y empieza la
# siguiente (o notar cuando el modelo mezcló dos sub-secciones en una).
_RE_TITULO_NUMERADO = _re_docx.compile(r'^\s*\d+[\.\)]\s+\S')
_RE_ETIQUETA = _re_docx.compile(r'^\s*([A-ZÁÉÍÓÚÑ][^:\n]{0,80}:)\s*(.*)$')

# Líneas separadoras markdown (---, ___, ***) sin equivalente en Word — se
# descartan en vez de volcarse como texto plano.
_RE_SEPARADOR_MD = _re_docx.compile(r'^[-_*]{3,}$')
# Encabezado markdown (#, ##, ### ...) al inicio de línea.
_RE_HEADER_MD = _re_docx.compile(r'^#{1,6}\s*')



# ── Clasificación: categorías con su nombre, tomado del catálogo ──────────────
def _catalogo_estructurado() -> dict:
    """{n_categoria: (nombre, [subcategorías])} leído de prompts/catalogo.txt."""
    from config.config import PROMPTS_DIR
    lineas = (PROMPTS_DIR / "catalogo.txt").read_text(encoding="utf-8").split("\n")
    cats, cur = {}, None
    for i, l in enumerate(lineas):
        if re.match(r"^Categor[ií]a \d+", l) and i + 1 < len(lineas):
            m = re.match(r"^(\d+)\.\t(.+)", lineas[i + 1])
            if m:
                cur = int(m.group(1)); cats[cur] = (m.group(2).strip(), [])
        elif cur and re.match(r"^\d+\.\t", l) and not re.match(r"^Categor", lineas[i - 1]):
            cats[cur][1].append(l.split("\t", 1)[1].strip())
    return cats


def _reubicar_subcategorias(perfil: list, cats: dict) -> list:
    """Mueve cada subcategoría a la categoría a la que pertenece en el catálogo
    (el modelo a veces cuelga "Procedimiento ejecutivo" de la 16 en vez de la 24).
    Solo se mueve si el nombre existe en una única categoría; si no, se deja."""
    dueño = {}
    for k, (_, subs) in cats.items():
        for s in subs:
            dueño.setdefault(s.lower(), set()).add(k)
    nuevo = []
    for n, nombre, subs in perfil:
        entrada = next((e for e in nuevo if e[0] == n), None) or [n, nombre, []]
        if entrada not in nuevo:
            nuevo.append(entrada)
        for s in subs:
            ks = dueño.get(s.lower(), set())
            if not ks:
                continue   # subcategoría inventada por el modelo (no está en el catálogo): se descarta
            destino = n if (n in ks or len(ks) != 1) else next(iter(ks))
            e = next((e for e in nuevo if e[0] == destino), None)
            if e is None:
                e = [destino, cats[destino][0], []]
                nuevo.append(e)
            if s not in e[2]:
                e[2].append(s)
    return [e for e in nuevo if e[2]]   # una categoría sin subcategoría es un rótulo vacío ("Categoría 2:")


_RE_CATEGORIA_CLAS = re.compile(r"^\s*\**\s*Categor[ií]a\s*\[?\d+", re.I)
# Inicio de la lista definitiva cuando el modelo deja a la vista su deliberación.
_RE_LISTA_FINAL = re.compile(r"^\W*(?:conclusi[oó]n final|categor[ií]as (?:definitivas|finales)|"
                             r"clasificaci[oó]n (?:final|definitiva)|CLASIFICACI[OÓ]N DE LA SENTENCIA)", re.I)
# Texto de trabajo del modelo: "*(Nota: No aplicable… Se elimina.)*", "Verificación final:",
# "(Confirmado: …)".
_RE_DELIBERACION = re.compile(r"^\W*(?:nota\b|verificaci[oó]n|confirmad|se elimina|no aplicable|no se clasifica)", re.I)


def _lista_final(lineas: list, i0: int) -> list:
    """Si después de la primera categoría hay un rótulo de lista definitiva
    ("Conclusión final de clasificación:", "Categorías definitivas…", otro
    "CLASIFICACIÓN DE LA SENTENCIA…") seguido de categorías, se queda el
    último: un encabezado completo reemplaza todo; un rótulo solo, las
    categorías previas."""
    finales = [k for k in range(i0 + 1, len(lineas)) if _RE_LISTA_FINAL.match(lineas[k])
               and any(_RE_CATEGORIA_CLAS.match(l) for l in lineas[k + 1:])]
    if not finales:
        return lineas
    k = finales[-1]
    if re.match(r"^\W*CLASIFICACI[OÓ]N DE LA SENTENCIA", lineas[k], re.I):
        return lineas[k:]
    return lineas[:i0] + lineas[k + 1:]


def normalizar_clasificacion(texto: str) -> tuple:
    """Reescribe el bloque de categorías del prompt 4 en un formato fijo:
        Categoría 9: Despido intempestivo
        • Subcategoría: Configuración del despido intempestivo
    completando el nombre de la categoría desde el catálogo cuando el modelo
    lo omite. Usa la lista definitiva (_lista_final), borra las notas y
    verificaciones del modelo y las categorías sin subcategoría.
    Devuelve (texto_normalizado, [(n, categoría, [subcategorías])])."""
    if not texto:
        return texto, []
    try:
        cats = _catalogo_estructurado()
    except Exception:
        cats = {}
    lineas = [l.rstrip() for l in texto.split("\n")]
    i0 = next((i for i, l in enumerate(lineas) if _RE_CATEGORIA_CLAS.match(l)), None)
    if i0 is None:
        return texto, []
    lineas = _lista_final(lineas, i0)
    i0 = next(i for i, l in enumerate(lineas) if _RE_CATEGORIA_CLAS.match(l))
    perfil, actual, verificando = [], None, False
    i = i0
    while i < len(lineas):
        l = re.sub(r"[*#]", "", lineas[i]).strip()
        m = re.match(r"^Categor[ií]a\s*\[?(\d+)\]?\s*[:.\-–]?\s*(.*)$", l)
        # La verificación sigue en viñetas ("- La categoría 7.4 … Se elimina.") hasta la próxima categoría.
        verificando = not m and (verificando or bool(re.match(r"^\W*verificaci[oó]n", l, re.I)))
        if verificando or _RE_DELIBERACION.match(l):
            i += 1; continue
        if m:
            n = int(m.group(1)); nombre = m.group(2).strip()
            if not nombre and i + 1 < len(lineas):
                sig = re.sub(r"[*#]", "", lineas[i + 1]).strip()
                if (sig and not re.match(r"^[•\-]?\s*Subcategor", sig) and not sig.startswith("Categor")
                        and not _RE_DELIBERACION.match(sig)):
                    nombre = sig; i += 1
            # El modelo a veces numera en orden ("Categoría 1") y pone el número
            # real del catálogo junto al nombre ("11. Visto bueno"): manda el
            # nombre; si no coincide con el catálogo, el número del nombre.
            mn = re.match(r"^(\d+)\.\s*(.+)$", nombre)
            if mn:
                n, nombre = int(mn.group(1)), mn.group(2).strip()
            por_nombre = next((k for k, (nom, _) in cats.items()
                               if nombre and nom.lower() == nombre.lower()), None)
            if por_nombre:
                n = por_nombre
            nombre = cats.get(n, (nombre or "", []))[0] or nombre
            actual = [n, nombre, []]; perfil.append(actual)
            i += 1; continue
        m = re.match(r"^[•\-]?\s*Subcategor[ií]a\s*:?\s*(.*)$", l)
        if m and actual is not None:
            sub = re.sub(r"^\d+\.\s*", "", m.group(1).strip())
            if not sub and i + 1 < len(lineas):
                sig = re.sub(r"[*#]", "", lineas[i + 1]).strip()
                if (sig and not sig.startswith("Categor") and not re.match(r"^[•\-]?\s*Subcategor", sig)
                        and not _RE_DELIBERACION.match(sig)):
                    sub = re.sub(r"^\d+\.\s*", "", sig); i += 1
            if sub:
                actual[2].append(sub)
            i += 1; continue
        if l and not perfil:
            i += 1; continue
        if l:   # texto fuera de formato tras las categorías: se conserva al final
            break
        i += 1
    perfil = _reubicar_subcategorias(perfil, cats)
    cuerpo = []
    for n, nombre, subs in perfil:
        cuerpo.append(f"Categoría {n}: {nombre}")
        cuerpo += [f"• Subcategoría: {s}" for s in subs]
    resto = [r for r in lineas[i:] if not _RE_DELIBERACION.match(re.sub(r"[*#]", "", r).strip())]
    nuevo = "\n".join(lineas[:i0] + cuerpo + ([""] + resto if any(r.strip() for r in resto) else []))
    return nuevo, [tuple(x) for x in perfil]


# Rótulos internos que la ficha muestra con el nombre que pidieron los abogados
# (piloto civil v2): las normas de las secciones de argumentos son las que invoca
# la parte, no las que aplica el juez. El texto interno no cambia (lo parsean
# controles.py y strapi_client).
_ROTULOS_FICHA = {
    "NORMATIVA APLICADA:": "NORMATIVA INVOCADA POR LA PARTE:",
    "Normativa Aplicada": "Normativa invocada por la parte",
    "No consta normativa aplicada atribuible a esta parte en la sentencia.":
        "No consta en la sentencia normativa invocada por esta parte.",
}


def _frases_decision() -> set:
    """Frases canónicas de decisión (codigos.json): la ficha ya las muestra en
    el perfil del caso, así que no se repiten como última línea de la decisión."""
    import json as _json
    try:
        d = _json.loads((PROMPTS_DIR / "codigos.json").read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {f.strip() for k in ("decisiones_primera_instancia", "decisiones_apelacion", "decisiones_casacion")
            for f in d.get(k, [])}


def _agregar_seccion_docx(doc, titulo: str, contenido: str):
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    h = doc.add_heading(titulo, 2)
    h.runs[0].font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
    frases = _frases_decision()
    lineas = [l.strip() for l in contenido.split("\n")]
    for i, linea in enumerate(lineas):
        if not linea or _RE_SEPARADOR_MD.match(linea):
            continue
        linea = _ROTULOS_FICHA.get(linea, linea)
        if linea.startswith("NORMATIVA APLICADA:"):
            linea = "NORMATIVA INVOCADA POR LA PARTE:" + linea[len("NORMATIVA APLICADA:"):]
        # Frase canónica suelta al final de "Decisión Final" (repetía la decisión).
        siguiente = next((l for l in lineas[i + 1:] if l), "")
        anterior = next((l for l in reversed(lineas[:i]) if l), "")
        if linea in frases and _RE_TITULO_NUMERADO.match(siguiente) and anterior and not _RE_TITULO_NUMERADO.match(anterior):
            continue
        # Secciones como Jurisprudencia/Clasificación traen más markdown
        # (###, **negrita** envolviendo la línea entera) que Extracción —
        # sin sacarlo, queda "**JURISPRUDENCIA**" literal en vez de negrita
        # real. Se saca el marcador acá; si la línea queda con forma de
        # "Etiqueta:" o "N. Título", los bloques de abajo igual la resaltan.
        linea = _RE_HEADER_MD.sub("", linea).replace("**", "").strip()
        if not linea:
            continue
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        if _RE_TITULO_NUMERADO.match(linea):
            # "14. Argumentos de la Parte Actora" — título de sub-sección completo.
            r = p.add_run(linea)
            r.bold = True
        else:
            m = _RE_ETIQUETA.match(linea)
            if m and len(m.group(1)) <= 60:
                # "Fecha:" o "Voto salvado:" en negrita, el resto de la línea normal.
                r1 = p.add_run(m.group(1))
                r1.bold = True
                if m.group(2):
                    p.add_run(" " + m.group(2))
            else:
                p.add_run(linea)
                if len(linea) > 120:   # párrafos de prosa justificados (estilo de las fichas)
                    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    doc.add_paragraph()



def _seccion_ficha(extraccion: str, numero: int) -> str:
    """Texto de la sección N de la extracción: '6. Tipo de Acción:' (prompt 2) o
    '11. Decisión Final' sin dos puntos y con o sin ** (prompts 5 y 11)."""
    m = re.search(rf"(?ms)^[\s*#]*{numero}\.\s*[^\n:]*:?\s*\n?(.*?)(?=^[\s*#]*{numero + 1}\.\s|\Z)", extraccion or "")
    return re.sub(r"[*#]", "", m.group(1)).strip() if m else ""


# Secciones de la extracción que alimentan el perfil: prompt 2 (tribunal) y
# prompts 5/11 (casación/apelación, que solo tienen la decisión en la 11).
_SECCIONES_PERFIL_TRIBUNAL = {"tipo": 6, "cuantia": 7, "decision": 16}
_SECCIONES_PERFIL_CORTE    = {"decision": 11}


# Fórmulas de cierre que nunca son la decisión ("Notifíquese, publíquese y devuélvase.").
_RE_FORMULA_CIERRE = re.compile(r"^\W*(?:notifiq|publiq|devuelv|cumpla|hagase saber|ofic|archiv|"
                                r"actua como|ejecutoriad|remit)")


# Lo que convierte "Acepta"/"Rechaza la demanda" en parcial: la transcripción dice
# "parcialmente con lugar" o niega un rubro ("Sin lugar los demás rubros").
_RE_PARCIAL = re.compile(r"parcialmente\s+con\s+lugar|con\s+lugar\s+parcialmente|acept\w*\s+parcialmente\s+la\s+demanda")
_RE_NIEGA_RUBRO = re.compile(r"\b(?:sin lugar|no ha lugar)\b[^.;\n]{0,80}")


_RE_DEMANDA_RECHAZA = re.compile(
    r"\b(?:rechaz\w*|desech\w*|niega|negar|declara sin lugar|sin lugar)\b[^.;:]{0,80}\bla (?:demanda|accion)\b"
    r"|\bla (?:demanda|accion)\b[^.;:]{0,40}\b(?:sin lugar|improcedente|se rechaza|se desecha)\b")
_RE_DEMANDA_ACEPTA = re.compile(
    r"\b(?:acept\w*|declara con lugar|con lugar)\b[^.;:]{0,60}\bla (?:demanda|accion)\b"
    r"|\bla (?:demanda|accion)\b[^.;:]{0,30}\bcon lugar\b")


def _veredicto_demanda_en_texto(lineas: list, sin_tildes) -> str:
    """'rechaza' | 'acepta' | '' según lo que la transcripción de la parte
    resolutiva dice de la DEMANDA. Se mira cláusula por cláusula (separadas por
    coma, punto y coma o dos puntos) y solo el texto anterior a la primera
    mención de la reconvención. Si las cláusulas no coinciden entre sí (p. ej.
    la transcripción trae después el voto salvado), vale la primera: la de mayoría."""
    vistos = []
    for l in lineas:
        t = sin_tildes(l).split("reconvenc")[0]
        for c in re.split(r"[,;:]", t):
            if not re.search(r"\bla (?:demanda|accion)\b", c):
                continue
            if _RE_PARCIAL.search(c):
                vistos.append("parcial")
                continue
            if _RE_DEMANDA_ACEPTA.search(c):
                vistos.append("acepta")
            elif _RE_DEMANDA_RECHAZA.search(c):
                vistos.append("rechaza")
    return vistos[0] if vistos else ""


def _decision_demanda(lineas: list, M, sin_tildes) -> str:
    """Primera instancia: la frase sobre la DEMANDA (acepta, acepta parcialmente
    o rechaza) tiene prioridad sobre la de la reconvención y las demás líneas, y
    respeta lo parcial de la transcripción. "" si no hay frase sobre la demanda."""
    acepta, parcial, rechaza = M.DECISIONES_ENUM_IA[:3]
    frases = [l for l in lineas if sin_tildes(l).startswith(tuple(sin_tildes(c) for c in (acepta, parcial, rechaza)))
              and "reconvenc" not in sin_tildes(l)]
    if not frases:
        # Piloto civil v2: el modelo a veces cierra con la parte resolutiva sin la
        # frase canónica ("…se Acepta la demanda.", "Se rechaza la demanda."); sin
        # esto el perfil caía en la línea de la reconvención y mostraba su decisión.
        segun_texto = _veredicto_demanda_en_texto(lineas, sin_tildes)
        if segun_texto:
            return {"rechaza": rechaza, "acepta": acepta, "parcial": parcial}[segun_texto].rstrip(".") + "."
        for l in reversed(lineas):
            t = sin_tildes(l)
            if "reconvenc" in t:
                continue
            c = next((c for c in (parcial, acepta, rechaza) if sin_tildes(c).rstrip(". ") in t), None)
            if c:
                return c.rstrip(".") + "."
        return ""
    final = frases[-1]
    # Puerta civil v2 (18331-2020-00450): el modelo escribió "Acepta parcialmente la
    # demanda." cuando el juez la rechazó y aceptó parcialmente la reconvención. Lo
    # que diga la transcripción sobre la demanda (antes de mencionar la
    # reconvención) prevalece sobre la frase canónica del modelo.
    segun_texto = _veredicto_demanda_en_texto([l for l in lineas if l not in frases], sin_tildes)
    if segun_texto == "parcial":
        segun_texto = ""
    # Piloto v4 (09332-2019-03351): el modelo escribió "Acepta parcialmente" y la
    # transcripción dice "declaro CON LUGAR la demanda" sin la palabra
    # "parcialmente". Sin esa palabra en la transcripción, la aceptación es total.
    resto = " ".join(sin_tildes(l) for l in lineas if l not in frases)
    if (segun_texto == "acepta" and sin_tildes(final).startswith(sin_tildes(parcial).rstrip(". "))
            and "parcial" not in resto):
        return acepta.rstrip(".") + "."
    if segun_texto == "rechaza" and not sin_tildes(final).startswith(sin_tildes(rechaza)):
        return rechaza.rstrip(".") + "."
    if segun_texto == "acepta" and sin_tildes(final).startswith(sin_tildes(rechaza)):
        return acepta.rstrip(".") + "."
    # La transcripción, sin las líneas de la reconvención ni las frases canónicas.
    resto = [sin_tildes(l) for l in lineas if l not in frases and "reconvenc" not in sin_tildes(l)]
    niega_rubro = any(not re.search(M.NEGATIVAS_QUE_NO_CUENTAN, m.group(0))
                      for l in resto for m in _RE_NIEGA_RUBRO.finditer(l))
    if any(_RE_PARCIAL.search(l) for l in resto) or (
            sin_tildes(final).startswith(sin_tildes(acepta)) and niega_rubro):
        return parcial + "."
    return final


def _decision_perfil(texto: str, primera_instancia: bool = False) -> str:
    """Línea de decisión para el perfil. En primera instancia, la frase sobre la
    demanda (_decision_demanda). Si no, la última que empieza con una decisión
    canónica de la materia; si no hay, la canónica de casación o apelación que
    contenga la transcripción ("…NO CASA la sentencia…" → "No casa la
    sentencia."); si no, la última línea que no es una fórmula de cierre."""
    from config import materia as M
    from resolucion.ai.controles import sin_tildes
    lineas = [l.strip(" *•") for l in (texto or "").split("\n") if l.strip(" *•")]
    demanda = _decision_demanda(lineas, M, sin_tildes) if primera_instancia else ""
    if demanda:
        return demanda
    todas = [*M.DECISIONES_PRIMERA_INSTANCIA, *M.DECISIONES_APELACION, *M.DECISIONES_CASACION]
    inicios = tuple(sin_tildes(c).rstrip(". ") for c in todas)
    for l in reversed(lineas):
        if sin_tildes(l).startswith(inicios):
            return l
    lineas = [l for l in lineas if not _RE_FORMULA_CIERRE.match(sin_tildes(l))]
    oraciones = sorted((c for c in [*M.DECISIONES_APELACION, *M.DECISIONES_CASACION] if c.endswith(".")),
                       key=len, reverse=True)
    for l in reversed(lineas):
        c = next((c for c in oraciones if re.search(rf"\b{re.escape(sin_tildes(c)[:-1])}\b", sin_tildes(l))), None)
        if c:
            return c
    return lineas[-1] if lineas else ""


def _perfil_caso_texto(extraccion: str, clasificacion: str, perfil: dict = None,
                       secciones: dict = None) -> str:
    """Bloque corto al inicio de la ficha para perfilar el caso de un vistazo."""
    _, cats = normalizar_clasificacion(clasificacion or "")
    secs = secciones or _SECCIONES_PERFIL_TRIBUNAL
    # La sección 7 puede venir en viñetas ("• Capital: USD …"): se quita la viñeta
    # propia para que el perfil no muestre "• Cuantía: • …".
    _primera = lambda k: (re.sub(r"^\s*(?:[•●\-–*]\s*)+", "", _seccion_ficha(extraccion, secs[k]).split("\n")[0])
                          if k in secs else "")
    tipo = _primera("tipo")
    cuantia = _primera("cuantia")
    decision = _decision_perfil(_seccion_ficha(extraccion, secs["decision"]),
                                primera_instancia=secs is _SECCIONES_PERFIL_TRIBUNAL)
    lineas = []
    if tipo:     lineas.append(f"• Tipo de acción: {tipo}")
    if decision: lineas.append(f"• Decisión: {decision}")
    if cuantia:  lineas.append(f"• Cuantía: {cuantia}")
    if cats:
        lineas.append("• Categorías del catálogo:")
        for n, nombre, subs in cats:
            lineas.append(f"   – {n}. {nombre}" + (f" › {'; '.join(subs)}" if subs else ""))
    contr = (perfil or {}).get("controversias") or []
    if contr:
        from config import materia as MATERIA
        etiq = getattr(MATERIA, "ETIQUETAS_TIPOS_DEFECTO", {})
        lineas.append("• Puntos controvertidos: " + "; ".join(etiq.get(c) or c.replace("_", " ").capitalize() for c in contr))
    return "\n".join(lineas)


def crear_docx_tribunal(
    numero: str, resumen: str, extraccion: str,
    jurisprudencia: str, clasificacion: str,
    nombre_tribunal: str = "", ano: str = "",
    job_id: int = None, causa: str = None,
    perfil: dict = None,
) -> bytes:
    """Genera la Ficha Técnica de Tribunal como Word (.docx).
    `perfil` (opcional): {"controversias": [códigos internos del prompt 7]}."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document()
        for sec in doc.sections:
            sec.top_margin = sec.bottom_margin = Cm(2.5)
            sec.left_margin = Cm(3); sec.right_margin = Cm(2.5)

        t = doc.add_heading("FICHA TÉCNICA", 0)
        t.alignment = WD_ALIGN_PARAGRAPH.CENTER
        t.runs[0].font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

        s = doc.add_heading(f"Sentencia N.° {numero}", 1)
        s.alignment = WD_ALIGN_PARAGRAPH.CENTER
        s.runs[0].font.size = Pt(14)
        s.runs[0].font.color.rgb = RGBColor(0x2E, 0x74, 0xB5)

        subtitulo = f"{nombre_tribunal}  |  Año {ano}" if nombre_tribunal else ""
        if subtitulo:
            doc.add_paragraph(subtitulo).runs[0].font.color.rgb = RGBColor(0x70, 0x70, 0x70)
        doc.add_paragraph()

        perfil_txt = _perfil_caso_texto(extraccion, clasificacion, perfil)
        if perfil_txt:
            _agregar_seccion_docx(doc, "PERFIL DEL CASO", perfil_txt)
            doc.add_paragraph()

        _agregar_seccion_docx(doc, "I. RESUMEN EJECUTIVO", resumen)
        doc.add_page_break()
        _agregar_seccion_docx(doc, "II. EXTRACCIÓN DE DATOS DE LA SENTENCIA", extraccion)
        doc.add_page_break()
        _agregar_seccion_docx(doc, "III. JURISPRUDENCIA Y CRITERIOS DEL JUZGADOR", jurisprudencia)
        doc.add_page_break()
        from config import materia as _M
        _agregar_seccion_docx(doc, _M.TITULO_CLASIFICACION_DOCX, clasificacion)

        buf = io.BytesIO(); doc.save(buf)
        return buf.getvalue()
    except Exception as e:
        log.error(f"crear_docx_tribunal: {type(e).__name__}: {e}", job_id=job_id, causa=causa)
        return b""


def crear_docx_corte(
    numero: str, resumen: str, extraccion: str, jurisprudencia: str,
    job_id: int = None, causa: str = None,
    nivel: str = "casacion",
    clasificacion: str = "", perfil: dict = None,
) -> bytes:
    """Genera la Ficha Técnica de Casación (o de Apelación) como Word (.docx).
    `clasificacion` (prompt 4) y `perfil` ({"controversias": [códigos del
    prompt 7]}) alimentan el bloque PERFIL DEL CASO, igual que en tribunal."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Cm
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document()
        for sec in doc.sections:
            sec.top_margin = sec.bottom_margin = Cm(2.5)
            sec.left_margin = Cm(3); sec.right_margin = Cm(2.5)

        es_apel = (nivel == "apelacion")
        from config import materia as MATERIA
        t = doc.add_heading("FICHA TÉCNICA DE APELACIÓN" if es_apel else "FICHA TÉCNICA DE CASACIÓN", 0)
        t.alignment = WD_ALIGN_PARAGRAPH.CENTER
        t.runs[0].font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

        s = doc.add_heading(f"Sentencia de {'Apelación' if es_apel else 'Casación'} N.° {numero}", 1)
        s.alignment = WD_ALIGN_PARAGRAPH.CENTER
        s.runs[0].font.size = Pt(14)
        s.runs[0].font.color.rgb = RGBColor(0x2E, 0x74, 0xB5)

        doc.add_paragraph(
            (MATERIA.NOMBRE_APELACION + " – Sala Especializada") if es_apel else MATERIA.SALA_CNJ
        ).runs[0].font.color.rgb = RGBColor(0x70, 0x70, 0x70)
        doc.add_paragraph()

        perfil_txt = _perfil_caso_texto(extraccion, clasificacion, perfil,
                                        secciones=_SECCIONES_PERFIL_CORTE)
        if perfil_txt:
            _agregar_seccion_docx(doc, "PERFIL DEL CASO", perfil_txt)
            doc.add_paragraph()

        _agregar_seccion_docx(doc, "I. RESUMEN EJECUTIVO", resumen)
        doc.add_page_break()
        _agregar_seccion_docx(doc, f"II. EXTRACCIÓN DE DATOS DE LA SENTENCIA DE {'APELACIÓN' if es_apel else 'CASACIÓN'}", extraccion)
        doc.add_page_break()
        _agregar_seccion_docx(doc, f"III. JURISPRUDENCIA Y CRITERIOS DE LA {'CORTE PROVINCIAL' if es_apel else 'CORTE NACIONAL'}", jurisprudencia)

        buf = io.BytesIO(); doc.save(buf)
        return buf.getvalue()
    except Exception as e:
        log.error(f"crear_docx_corte: {type(e).__name__}: {e}", job_id=job_id, causa=causa)
        return b""


# Re-exportar constantes útiles para los módulos de ai/
__all__ = [
    "cargar_api_key_openai", "cargar_api_key_deepinfra", "normalizar_clasificacion", "quitar_cedulas", "cargar_api_key_openrouter", "es_modelo_qwen",
    "cargar_prompt", "extraer_texto_pdf",
    "llamar_openai", "llamar_qwen",
    "crear_docx_tribunal", "crear_docx_corte",
    "GPT_MAX_TOKENS", "GPT_MAX_CHARS_INPUT", "QWEN_MAX_CHARS_INPUT",
    "GPT_TEMPERATURE", "GPT_PAUSA_SEG",
    "EJEMPLOS_TRIBUNAL_DIR", "EJEMPLOS_CORTE_DIR",
    "DECISIONES_VALIDAS", "TIPOS_ACCION_VALIDOS", "MIN_CHARS_RESUMEN",
]


def sin_linea_cortada(texto: str) -> str:
    """Si la salida quedó truncada, quita la última línea a medio escribir para
    que la ficha no termine a mitad de palabra (puerta civil v7: dos fichas)."""
    t = (texto or "").rstrip()
    ultima = t[t.rfind("\n") + 1:] if "\n" in t else t
    # Una viñeta corta sin punto ("• Falta de motivación") es normal; se corta
    # solo una línea larga que termina sin cierre.
    if not t or t[-1] in ".:;)”\"»…" or "\n" not in t or len(ultima) < 80:
        return t
    return t[:t.rfind("\n")].rstrip()
