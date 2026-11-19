"""
Análisis de sentencias de PRIMERA INSTANCIA con IA.

analizar(pdf_bytes, numero, demandado, modelo, job_id) → dict

Soporta:
  - OpenAI gpt-4o (y variantes)  → prompts 1,2,3,4,7
  - Qwen vía DeepInfra            → mismos prompts

El dict de retorno es idéntico sin importar el proveedor:
  resumen, extraccion, jurisprudencia, categorias, tipos_defecto,
  cuantia, tipo_accion, objeto_controversia, decision_final,
  cuantia_origen, fecha_emision, juez_ponente,
  abogado_actor, abogado_demandado,
  docx_bytes, administracion, modo, pdf_truncado,
  validacion_ok, validacion_problemas, error
"""

import re as _re
import re
import os
import time
import json
import httpx

from resolucion.ai._cliente import (
    cargar_api_key_openai, cargar_api_key_openrouter, es_modelo_qwen,
    cargar_prompt, extraer_texto_pdf,
    llamar_openai, llamar_qwen, crear_docx_tribunal,
    GPT_MAX_TOKENS, GPT_MAX_CHARS_INPUT, QWEN_MAX_CHARS_INPUT,
    GPT_PAUSA_SEG, EJEMPLOS_TRIBUNAL_DIR,
    DECISIONES_VALIDAS, TIPOS_ACCION_VALIDOS, MIN_CHARS_RESUMEN,
    elegir_salida_reintento,
)
from config.pdf_utils import (
    cargar_ejemplos_few_shot, texto_legible, pdf_a_imagenes_b64,
    buscar_cuantia_en_texto, cuantia_vacia,
    buscar_fecha_emision, buscar_juez_ponente, buscar_emails, buscar_abogados,
)
from config import materia as MATERIA
from resolucion.ai import controles, regla_cero
from worker import logger as log

# ── Catálogo (no editable en vivo desde la webapp — ver PROMPTS_EDITABLES
# en worker/job_manager.py) ──────────────────────────────────────────────────
_CATALOGO = cargar_prompt("catalogo.txt").rstrip()

# Ejemplos few-shot: se leen una sola vez al importar (no son prompts
# editables, son archivos de referencia aparte).
_EJEMPLOS = cargar_ejemplos_few_shot(EJEMPLOS_TRIBUNAL_DIR)


def _codigos_controversia(texto: str) -> list:
    """Códigos del prompt 7 presentes en su respuesta, en el orden del catálogo."""
    try:
        import json as _j
        from config.config import PROMPTS_DIR
        validos = _j.loads((PROMPTS_DIR / "codigos.json").read_text(encoding="utf-8"))["tipos_defecto"]
    except Exception:
        return []
    return [c for c in validos if c != "SIN_ESPECIFICAR" and re.search(rf"\b{c}\b", texto or "")]


def _cargar_prompts_editables():
    """
    Recarga los 7 prompts editables en cada extracción (en vez de una sola
    vez al importar el módulo) — así una edición hecha desde la webapp
    aplica en la próxima causa procesada, sin reiniciar el worker. cargar_prompt()
    ya cachea unos segundos, así que esto no le pega a la BD por cada
    llamada de IA dentro de la misma causa.
    """
    sistema        = cargar_prompt("sistema.txt")
    sistema_neutro = cargar_prompt("sistema_neutro.txt")
    p1_resumen     = cargar_prompt("prompt_1_resumen.txt")
    p2_extrac      = cargar_prompt("prompt_2_extraccion.txt")
    p3_jurisp      = cargar_prompt("prompt_3_jurisprudencia.txt")
    p4_clasif      = cargar_prompt("prompt_4_clasificacion.txt")
    p7_tipos       = cargar_prompt("prompt_7_tipos_defecto.txt")
    p9_evidencia   = cargar_prompt("prompt_9_evidencia_ausente.txt")

    if _EJEMPLOS:
        marcador = "SENTENCIA A ANALIZAR:"
        bloque = (
            "EJEMPLOS DE REFERENCIA (sigue este mismo formato y nivel de detalle "
            "para la sentencia que viene al final):\n\n" + _EJEMPLOS + "\n"
        )
        p2_extrac = (
            p2_extrac.replace(marcador, bloque + marcador)
            if marcador in p2_extrac
            else p2_extrac.rstrip() + "\n\n" + bloque + marcador + "\n"
        )
    return sistema, sistema_neutro, p1_resumen, p2_extrac, p3_jurisp, p4_clasif, p7_tipos, p9_evidencia


# ── Catálogo por administración ───────────────────────────────────────────────

# Las administraciones (catálogos por entidad demandada) vienen de
# config/materia.py. En materias con un solo catálogo el dict está vacío y
# toda la lógica de "administración" se desactiva sola.
_CATALOGO_HEADERS = [
    (sigla, _re.escape(nombre).replace(r"\ ", r"\s+"))
    for sigla, (nombre, _kw) in MATERIA.ADMINISTRACIONES.items()
]
_PATRONES_ADMIN = [
    (sigla, kw) for sigla, (_nombre, kw) in MATERIA.ADMINISTRACIONES.items()
]
# Tributario buscaba SENAE antes que GAD y GAD antes que SRI; se respeta ese
# orden si existe (es el orden de especificidad de los patrones).
_ORDEN_ADMIN = ["SENAE", "GAD", "SRI"]
_PATRONES_ADMIN.sort(key=lambda t: _ORDEN_ADMIN.index(t[0]) if t[0] in _ORDEN_ADMIN else 99)


def _parsear_catalogo() -> dict:
    """Respaldo: parte el catálogo en texto plano (catalogo.txt) por administración."""
    pos = []
    for nombre, patron in _CATALOGO_HEADERS:
        m = _re.search(patron, _CATALOGO, _re.IGNORECASE)
        if m:
            pos.append((nombre, m.start()))
    pos.sort(key=lambda x: x[1])
    secciones = {}
    for i, (nombre, inicio) in enumerate(pos):
        fin = pos[i + 1][1] if i + 1 < len(pos) else len(_CATALOGO)
        secciones[nombre] = _CATALOGO[inicio:fin].strip()
    return secciones


# ── Catálogo leído del SISTEMA (Strapi) ───────────────────────────────────────
# El catálogo de categorías se lee directamente del sistema (colección
# `categorias`, ya organizada por entidad y por niveles padre→hijo), no de un
# texto plano. El texto plano (catalogo.txt) queda solo como respaldo si Strapi
# no está disponible al arrancar el worker.

_STRAPI_URL   = os.environ.get("STRAPI_URL", "http://localhost:1337")
_STRAPI_TOKEN = os.environ.get("STRAPI_API_TOKEN", "")

# PRÓXIMO: incluir la DESCRIPCIÓN de cada categoría en el catálogo que ve la IA,
# para afinar la elección de la subcategoría correcta. Hoy las descripciones en
# el sistema son de relleno ("Categoría de N nivel X"), así que esto queda
# APAGADO. Cuando los abogados llenen la `descripcion` real de cada categoría en
# el panel, poner CATALOGO_CON_DESCRIPCIONES=1 (o cambiar el default) y la
# clasificación las usará sin más cambios de código.
_INCLUIR_DESCRIPCIONES = os.environ.get("CATALOGO_CON_DESCRIPCIONES", "0") == "1"
# Descripciones de relleno que no aportan criterio: se ignoran aunque el flag
# esté encendido, hasta que se reemplacen por texto real.
_DESC_RELLENO = _re.compile(r'^\s*categor[ií]a de (primer|segundo|tercer|cuarto)\s+nivel', _re.IGNORECASE)

_HEADER_ADMIN = {sigla: nombre for sigla, (nombre, _kw) in MATERIA.ADMINISTRACIONES.items()}


def _admin_de_entidad(nombre_ent: str) -> str:
    n = (nombre_ent or "").lower()
    if not MATERIA.ADMINISTRACIONES:
        return ""
    if "rentas" in n:      return "SRI"
    if "aduana" in n:      return "SENAE"
    if "descentral" in n:  return "GAD"
    return ""


def _construir_catalogo_por_admin_desde_sistema() -> dict:
    """Lee las categorías del sistema (Strapi) y arma {admin: texto}, agrupadas
    por entidad y respetando la jerarquía padre→subcategorías. Devuelve {} si no
    hay token o no se pudo leer, para que el llamador use el respaldo."""
    if not _STRAPI_TOKEN:
        return {}
    cats, page = [], 1
    while True:
        resp = httpx.get(
            f"{_STRAPI_URL}/api/categorias",
            params={
                "fields[0]": "nombre", "fields[1]": "descripcion",
                "populate[entidad][fields][0]": "nombre",
                "populate[categoria_padre][fields][0]": "nombre",
                "pagination[page]": page, "pagination[pageSize]": 200,
            },
            headers={"Authorization": f"Bearer {_STRAPI_TOKEN}"},
            timeout=30,
        )
        body = resp.json()
        datos = body.get("data", []) or []
        cats.extend(datos)
        pag = body.get("meta", {}).get("pagination", {})
        if not datos or page >= pag.get("pageCount", 0):
            break
        page += 1

    from collections import defaultdict, OrderedDict
    # admin -> (roots ordenados, {padre: [hijos]})
    roots  = defaultdict(list)
    hijos  = defaultdict(lambda: defaultdict(list))
    for it in cats:
        nombre = (it.get("nombre") or "").strip()
        if not nombre:
            continue
        admin = _admin_de_entidad(((it.get("entidad") or {}) or {}).get("nombre") or "")
        if not admin:
            continue
        desc  = (it.get("descripcion") or "").strip()
        padre = ((it.get("categoria_padre") or {}) or {}).get("nombre") or ""
        padre = padre.strip()
        if padre:
            hijos[admin][padre].append((nombre, desc))
        else:
            roots[admin].append((nombre, desc))

    def _linea(nombre, desc, indent=""):
        base = f"{indent}• {nombre}"
        if _INCLUIR_DESCRIPCIONES and desc and not _DESC_RELLENO.match(desc):
            base += f": {desc}"
        return base

    salida = {}
    for admin in MATERIA.ADMINISTRACIONES:
        if not roots.get(admin) and not hijos.get(admin):
            continue
        lineas = [_HEADER_ADMIN[admin], ""]
        nombres_root = {n for n, _ in roots.get(admin, [])}
        for nombre, desc in roots.get(admin, []):
            lineas.append(_linea(nombre, desc))
            for hn, hd in hijos.get(admin, {}).get(nombre, []):
                lineas.append(_linea(hn, hd, "   "))
        # padres que no vinieron como root (por si falta el nivel-1): al final
        for padre, hs in hijos.get(admin, {}).items():
            if padre not in nombres_root:
                lineas.append(_linea(padre, ""))
                for hn, hd in hs:
                    lineas.append(_linea(hn, hd, "   "))
        salida[admin] = "\n".join(lineas).strip()
    return salida


def _cargar_catalogo_por_admin() -> dict:
    """Catálogo desde el sistema; si falla, respaldo en texto plano."""
    if not MATERIA.ADMINISTRACIONES:
        return {}          # catálogo único: se manda catalogo.txt completo
    try:
        desde_sistema = _construir_catalogo_por_admin_desde_sistema()
        if desde_sistema:
            print(f"[catalogo] leído del sistema: {', '.join(sorted(desde_sistema))}")
            return desde_sistema
        print("[catalogo] sistema vacío o sin token; usando respaldo texto plano")
    except Exception as e:
        print(f"[catalogo] no se pudo leer del sistema ({e}); usando respaldo texto plano")
    return _parsear_catalogo()


_CATALOGO_POR_ADMIN = _cargar_catalogo_por_admin()


def _detectar_admin(demandado: str, texto_pdf: str = "") -> str:
    fuente = (demandado or "").lower() + " " + (texto_pdf or "")[:5000].lower()
    for admin, patrones in _PATRONES_ADMIN:
        if any(p in fuente for p in patrones):
            return admin
    return ""


def _catalogo_admin(admin: str) -> str:
    if admin and admin in _CATALOGO_POR_ADMIN and _CATALOGO_POR_ADMIN[admin]:
        return _CATALOGO_POR_ADMIN[admin]
    return _CATALOGO


# ── Structured outputs (OpenAI) ───────────────────────────────────────────────

_SCHEMA_CAMPOS = {
    "name": "campos_sentencia",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["cuantia", "tipo_accion", "objeto_controversia", "decision_final"],
        "properties": {
            "cuantia": {"type": "string",
                        "description": "Cuantía tal como aparece (ej: 'USD 50.000,00'). Si no consta: 'No consta'."},
            "tipo_accion": {"type": "string",
                            "enum": list(MATERIA.TIPOS_ACCION_ENUM_IA)},
            "objeto_controversia": {"type": "string",
                                     "description": "Resumen del objeto de controversia (máx 250 chars)."},
            "decision_final": {"type": "string",
                               "enum": list(MATERIA.DECISIONES_ENUM_IA)},
        },
    },
}

_SCHEMA_CUANTIA = {
    "name": "cuantia_focal",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["cuantia"],
        "properties": {
            "cuantia": {"type": "string",
                        "description": "Cuantía total del juicio (ej: 'USD 50.000,00'). Si no consta: 'No consta'."},
        },
    },
}


# ── Validación post-GPT ───────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return (s or "").upper().replace("Á","A").replace("É","E").replace("Í","I")\
        .replace("Ó","O").replace("Ú","U").replace("Ñ","N")


def _seccion_presente(cabeceras: list, texto: str, min_chars: int = 40) -> bool:
    """
    True si al menos una de las cabeceras aparece en el texto Y tiene contenido
    real a continuación (no solo "No consta").
    """
    t = (texto or "").upper()
    for cab in cabeceras:
        idx = t.find(cab.upper())
        if idx == -1:
            continue
        fragmento = t[idx + len(cab): idx + len(cab) + 250].lstrip(": \n")
        # Si el fragmento empieza con "NO CONSTA" o tiene menos de min_chars → vacío
        if len(fragmento) >= min_chars and not fragmento.startswith("NO CONSTA"):
            return True
    return False


def _auditoria_campos(resultado: dict, texto_extraccion: str) -> dict:
    """
    Devuelve {campo: bool} donde True = presente/completo, False = faltante.
    Se almacena en _auditoria dentro del resultado para que el orquestador
    lo persista en la BD como columnas booleanas individuales.

    Campos cubiertos:
      cuantia, tipo_accion, decision_final, objeto, jurisprudencia,
      categorizacion, fecha_emision, otros_actos, pruebas, abogados.
    """
    def _vacio(v: str) -> bool:
        s = (v or "").strip().lower()
        return not s or s.startswith("no consta") or s in ("n/a", "-", "—", "vacío", "vacio")

    return {
        "cuantia":        not _vacio(resultado.get("cuantia")),
        "tipo_accion":    not _vacio(resultado.get("tipo_accion")),
        "decision_final": not _vacio(resultado.get("decision_final")),
        "objeto":         not _vacio(resultado.get("objeto_controversia")),
        "jurisprudencia": len((resultado.get("jurisprudencia") or "").strip()) >= 100,
        "categorizacion": len((resultado.get("categorias") or "").strip()) >= 20,
        "fecha_emision":  not _vacio(resultado.get("fecha_emision")),
        "otros_actos":    _seccion_presente(
            ["10. OTROS ACTOS", "10: OTROS ACTOS", "OTROS ACTOS ADMINISTRATIVOS RELACIONADOS"],
            texto_extraccion,
        ),
        "pruebas":        _seccion_presente(
            ["13. PRUEBAS", "PRUEBAS PRESENTADAS", "MEDIOS DE PRUEBA"],
            texto_extraccion,
        ),
        "abogados":       bool(resultado.get("abogado_actor") or resultado.get("abogado_demandado")),
    }


def _validar(resultado: dict, texto_pdf: str) -> tuple:
    """Reglas sin LLM que detectan salidas inválidas de GPT. Retorna (ok, [problemas])."""
    problemas = []
    pdf_truncado = bool(resultado.get("pdf_truncado"))

    resumen = (resultado.get("resumen") or "").strip()
    if not resumen:
        problemas.append("Resumen vacío — GPT no generó ningún texto")
    elif len(resumen) < MIN_CHARS_RESUMEN:
        problemas.append(f"Resumen muy corto ({len(resumen)} chars, mínimo {MIN_CHARS_RESUMEN})")
    elif resumen.lower().startswith("[error"):
        problemas.append(f"Resumen contiene error GPT: {resumen[:80]}")

    cuantia = (resultado.get("cuantia") or "").strip()
    if cuantia and "no consta" not in cuantia.lower():
        if not any(c.isdigit() for c in cuantia):
            problemas.append(f"Cuantía no numérica: '{cuantia[:50]}'")
        elif not pdf_truncado and texto_pdf and len(texto_pdf) >= 2000:
            digitos_cuantia = _re.sub(r"\D", "", cuantia)
            if len(digitos_cuantia) >= 4:
                digitos_pdf = _re.sub(r"\D", "", texto_pdf)
                if digitos_cuantia[:4] not in digitos_pdf:
                    problemas.append(
                        f"Cuantía '{cuantia[:40]}' no aparece en el PDF — posible alucinación"
                    )

    decision = (resultado.get("decision_final") or "").strip().lower()
    if decision and "no consta" not in decision:
        if not any(v in decision for v in DECISIONES_VALIDAS):
            problemas.append(f"Decisión no reconocida: '{resultado.get('decision_final','')[:50]}'")

    tipo = _norm(resultado.get("tipo_accion") or "").lower()
    if tipo and "no consta" not in tipo:
        # Ambos lados sin tildes: la lista tiene "acción…" y `tipo` ya viene sin tildes.
        if not any(_norm(v).lower() in tipo for v in TIPOS_ACCION_VALIDOS):
            problemas.append(f"Tipo de acción no reconocido: '{resultado.get('tipo_accion','')[:50]}'")

    if not (resultado.get("objeto_controversia") or "").strip():
        problemas.append("Objeto de controversia vacío")

    return (len(problemas) == 0, problemas)


# ── Extracción de campos clave (Qwen, sin Structured Outputs) ─────────────────

def _extraer_campos_qwen(texto_extraccion: str, api_key: str, modelo: str,
                          job_id: int, causa: str, flujo_proveedores: str = "alibaba") -> dict:
    import openai, json as _json
    from resolucion.ai._cliente import (
        OPENROUTER_BASE_URL, OPENROUTER_MODELO_QWEN, resolver_orden_proveedores,
        TIMEOUT_LLAMADA_SEG, GPT_PAUSA_SEG,
    )
    client = openai.OpenAI(
        api_key=api_key, base_url=OPENROUTER_BASE_URL,
        timeout=TIMEOUT_LLAMADA_SEG, max_retries=0,
    )
    prompt = (
        f"Del siguiente análisis de sentencia ecuatoriana en materia {MATERIA.NOMBRE}, "
        "extrae los 4 campos y responde SOLO en JSON (sin markdown):\n"
        '- cuantia: valor monetario (ej: "USD 50.000,00") o "No consta"\n'
        f'- tipo_accion: uno de {json.dumps(MATERIA.TIPOS_ACCION_ENUM_IA, ensure_ascii=False)}\n'
        "- objeto_controversia: resumen en máximo 250 caracteres\n"
        f'- decision_final: uno de {json.dumps(MATERIA.DECISIONES_ENUM_IA, ensure_ascii=False)}\n\n'
        "TEXTO:\n" + texto_extraccion[:12000]
    )
    try:
        resp = client.chat.completions.create(
            model=OPENROUTER_MODELO_QWEN, messages=[{"role": "user", "content": prompt}],
            max_tokens=400, temperature=0,
            extra_body={
                "enable_thinking": False,
                "provider": {"order": resolver_orden_proveedores(flujo_proveedores), "allow_fallbacks": False},
            },
        )
        raw = (resp.choices[0].message.content or "").strip()
        if "```" in raw:
            for parte in raw.split("```"):
                parte = parte.strip().lstrip("json").strip()
                if parte.startswith("{"):
                    try:
                        return _json.loads(parte)
                    except _json.JSONDecodeError:
                        continue
        return _json.loads(raw)
    except Exception as e:
        log.warn(f"Qwen campos_json: {type(e).__name__}: {e}", job_id=job_id, causa=causa)
        return {}


# ── Función principal ─────────────────────────────────────────────────────────

# Gatillo textual para el prompt 9: solo tiene sentido preguntarle al modelo
# si el tribunal reprocha una prueba faltante cuando el texto TRAE alguna forma
# de "no/tampoco presentó/adjuntó/aportó...". Medido sobre 50 sentencias
# reales: dispara en 54%, o sea ahorra ~46% de las llamadas del prompt 9 sin
# perder hallazgos (se validó contra los 5 casos confirmados a mano — un
# gatillo más estrecho, solo "no presentó", perdía 2 de esos 5).
_RE_EVIDENCIA_AUSENTE = _re.compile(
    r'(no|tampoco)\s+(se\s+)?(ha\s+|han\s+|hab[ei]r\w*\s+)?'
    r'(present\w+|adjunt\w+|aport\w+|acompa\u00f1\w+|exhib\w+|alleg\w+|incorpor\w+|agreg\w+|anex\w+)',
    _re.I,
)


def clasificar_y_tipificar(llamar, p4_clasif: str, p7_tipos: str, texto_rec: str,
                           demandado: str = "", prefijo: str = "", etiqueta: str = "IA",
                           job_id: int = None, numero: str = None) -> dict:
    """
    Prompt 4 (clasificación en el catálogo) y prompt 7 (tipos de defecto →
    puntos controvertidos) sobre una sentencia. Lo usan el tribunal y, con un
    `prefijo` que acota la clasificación al objeto del recurso, la apelación y
    la casación (resolucion/ai/corte.py).

    `llamar(prompt_base, max_tokens)` → (texto, finish_reason), ya con el
    texto de la sentencia y el modelo resueltos por el llamador.

    Retorna {"administracion", "categorias", "tipos_defecto", "truncados"}.
    """
    truncados = []
    administracion = _detectar_admin(demandado, texto_rec)
    catalogo_ef    = _catalogo_admin(administracion)

    nota_admin = ""
    if administracion:
        nota_admin = (
            f"\n\nINSTRUCCIÓN CRÍTICA: el demandado es de tipo {administracion}. "
            f"Solo elige categorías del catálogo {administracion}. "
            "NO mezcles con otras secciones.\n"
        )
        log.info(f"Administración detectada: {administracion} (catálogo filtrado)", job_id=job_id, causa=numero)
    else:
        log.info("Administración no detectada; catálogo completo", job_id=job_id, causa=numero)

    # ── Prompt 4: Clasificación ───────────────────────────────────────────────
    log.info(f"{etiqueta} Clasificación jurídica ({MATERIA.NOMBRE})", job_id=job_id, causa=numero)
    prompt_clas = prefijo + p4_clasif + nota_admin + "\n" + catalogo_ef + "\n\nSENTENCIA A ANALIZAR:\n"
    clasificacion, fr4 = llamar(prompt_clas, GPT_MAX_TOKENS["clasificacion"])
    if fr4 == "length": truncados.append("Clasificación")
    from resolucion.ai._cliente import normalizar_clasificacion
    clasificacion, _ = normalizar_clasificacion(clasificacion)

    # ── Prompt 7: Tipos de defecto (clasificación interna, no se muestra al
    # usuario — solo alimenta el motor de atribución de jurimetría) ──────────
    log.info(f"{etiqueta} Tipos de defecto (interno)", job_id=job_id, causa=numero)
    tipos_defecto, fr7 = llamar(prefijo + p7_tipos, GPT_MAX_TOKENS["tipos_defecto"])
    if fr7 == "length": truncados.append("Tipos de defecto")

    return {"administracion": administracion, "categorias": clasificacion,
            "tipos_defecto": tipos_defecto, "truncados": truncados}


def analizar(
    pdf_bytes: bytes,
    numero: str,
    demandado: str = "",
    modelo: str = "gpt-4o",
    job_id: int = None,
    nombre_tribunal: str = "",
    ano: str = "",
    flujo_proveedores: str = "alibaba",
) -> dict:
    """
    Analiza una sentencia de tribunal con los 4 prompts.

    Parámetros:
      pdf_bytes       — contenido del PDF como bytes
      numero          — número de juicio (ej: "09501-2019-00010")
      demandado       — nombre del demandado del SATJE (para detectar SRI/SENAE/GAD)
      modelo          — nombre del modelo de IA; "qwen" en el nombre → usa DeepInfra
      job_id          — id del job para logs y SSE (None en scripts locales)
      nombre_tribunal — para el encabezado del Word
      ano             — para el encabezado del Word

    Retorna dict con todos los campos o {"error": "...mensaje..."}.
    """
    es_qwen = es_modelo_qwen(modelo)
    max_chars = QWEN_MAX_CHARS_INPUT if es_qwen else GPT_MAX_CHARS_INPUT
    _sistema_base, _sistema_neutro, _p1_resumen, _p2_extrac, _p3_jurisp, _p4_clasif, _p7_tipos, _p9_evidencia = \
        _cargar_prompts_editables()

    resultado = {
        "resumen": "", "extraccion": "", "jurisprudencia": "", "categorias": "",
        "tipos_defecto": "",
        "evidencia_ausente": "",
        "cuantia": "", "tipo_accion": "", "objeto_controversia": "", "decision_final": "",
        "cuantia_origen": "", "fecha_emision": "", "juez_ponente": "", "emails": "",
        "abogado_actor": "", "abogado_demandado": "",
        "docx_bytes": None, "administracion": "",
        "modo": "texto", "pdf_truncado": False,
        "validacion_ok": False, "validacion_problemas": [], "error": "",
    }

    # ── API key ───────────────────────────────────────────────────────────────
    if es_qwen:
        api_key = cargar_api_key_openrouter()
        if not api_key:
            resultado["error"] = "OPENROUTER_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado
    else:
        api_key = cargar_api_key_openai()
        if not api_key:
            resultado["error"] = "OPENAI_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado

    # ── Extracción de texto ───────────────────────────────────────────────────
    texto = extraer_texto_pdf(pdf_bytes, job_id=job_id, causa=numero)
    resultado["pdf_truncado"] = bool(texto and len(texto) >= max_chars)
    # Los prompts reciben solo el voto de mayoría; el voto salvado llena sus
    # secciones en controles.aplicar().
    texto_rec, voto = controles.separar_voto((texto or "")[:max_chars])
    if voto:
        log.info(f"Voto separado de la mayoría ({len(voto['texto'])} chars)", job_id=job_id, causa=numero)

    # Modo visión (solo OpenAI; PDF escaneado sin texto extraíble)
    imagenes_vision = None
    if not es_qwen:
        legible, motivo = texto_legible(texto)
        if not legible:
            log.info(f"Texto no legible ({motivo}); intentando modo visión", job_id=job_id, causa=numero)
            try:
                imagenes_vision = pdf_a_imagenes_b64(pdf_bytes, max_paginas=25, dpi=150)
                if imagenes_vision:
                    resultado["modo"] = "vision"
                    log.info(f"Modo VISIÓN activo: {len(imagenes_vision)} imágenes", job_id=job_id, causa=numero)
                else:
                    imagenes_vision = None
            except Exception as e:
                log.warn(f"No se pudo generar imágenes para visión: {e}", job_id=job_id, causa=numero)

    if not imagenes_vision and not texto:
        resultado["error"] = "No se pudo extraer texto del PDF (PDF vacío, protegido o corrompido)"
        log.error(resultado["error"], job_id=job_id, causa=numero)
        return resultado

    log.info(
        f"IA [{modelo.split('/')[-1]}]: {len(texto)} chars del PDF"
        + (" (truncado)" if resultado["pdf_truncado"] else ""),
        job_id=job_id, causa=numero,
    )

    truncados = []

    def _llamar(prompt_base: str, max_tokens: int, sistema: str = None,
                response_format: dict = None, modelo_override: str = None,
                temperatura: float = None) -> tuple:
        _sistema = sistema or _sistema_base
        _modelo  = modelo_override or modelo
        if es_qwen:
            from resolucion.ai._cliente import resolver_orden_proveedores
            return llamar_qwen(prompt_base + texto_rec, _sistema, max_tokens,
                               api_key, _modelo, job_id=job_id, causa=numero,
                               orden_proveedores=resolver_orden_proveedores(flujo_proveedores),
                               temperatura=temperatura)
        return llamar_openai(
            (prompt_base + ("\n[Contenido entregado como imágenes adjuntas]\n" if imagenes_vision else texto_rec)),
            _sistema, max_tokens, api_key, _modelo, response_format,
            imagenes_b64=(imagenes_vision if imagenes_vision else None),
            job_id=job_id, causa=numero,
        )

    # ── Prompt 1: Resumen ─────────────────────────────────────────────────────
    log.info("IA [1/4] Resumen ejecutivo", job_id=job_id, causa=numero)
    resumen, fr1 = _llamar(_p1_resumen, GPT_MAX_TOKENS["resumen"])
    if fr1 == "length": truncados.append("Resumen")
    resultado["resumen"] = resumen
    time.sleep(GPT_PAUSA_SEG)

    # ── Prompt 2: Extracción ──────────────────────────────────────────────────
    log.info("IA [2/4] Extracción (19 secciones)", job_id=job_id, causa=numero)
    extraccion, fr2 = _llamar(_p2_extrac, GPT_MAX_TOKENS["extraccion"], sistema=_sistema_neutro)

    # Una sentencia de tamaño normal NO debería truncar la extracción de 19
    # secciones: cuando pasa, casi siempre es un BUCLE DE REPETICIÓN del modelo
    # (con temperatura 0 la salida es determinística por proveedor, así que se
    # queda pegado). En vez de continuar esa salida (sería continuar la basura),
    # se REINTENTA la extracción completa con algo de temperatura para romper el
    # bucle — un intento fresco suele salir limpio. Hasta 2 reintentos; se queda
    # el primero no-truncado (o, si todos truncan, el de menos repetición: el
    # más largo suele ser el del bucle).
    _reint = 0
    while fr2 == "length" and _reint < 2:
        _reint += 1
        log.warn(
            f"IA [2/4] Extracción truncada en doc de tamaño normal (probable "
            f"bucle de repetición) — reintentando completo (intento {_reint})",
            job_id=job_id, causa=numero,
        )
        nueva, fr_n = _llamar(_p2_extrac, GPT_MAX_TOKENS["extraccion"],
                              sistema=_sistema_neutro, temperatura=0.35)
        if not nueva or nueva.startswith("[Error") or "Lo siento" in nueva[:120]:
            break
        if elegir_salida_reintento(extraccion or "", nueva, fr_n):
            extraccion = nueva
        fr2 = fr_n
    # Sentencias muy largas (piloto civil v2: 400k caracteres) agotan el tope de
    # salida sin bucle: los reintentos también cortan y se pierden las secciones
    # finales (argumentos, decisión). Se pide la continuación desde la primera
    # sección que falta, una vez, y se pega al final.
    if fr2 == "length" and extraccion and not extraccion.startswith("[Error"):
        faltan = secciones_faltantes(extraccion, 19)
        if faltan:
            log.warn(f"IA [2/4] Extracción cortada: faltan secciones {faltan[0]}–19 — pidiendo continuación",
                     job_id=job_id, causa=numero)
            cont, fr_c = _llamar(_p2_extrac + instruccion_continuacion(faltan[0], 19),
                                 GPT_MAX_TOKENS["extraccion"], sistema=_sistema_neutro)
            if cont and not cont.startswith("[Error"):
                extraccion = recortar_seccion_incompleta(extraccion, faltan[0]) + "\n" + cont.strip()
                fr2 = fr_c
    if fr2 == "length":
        truncados.append("Extracción 19 secciones")

    if extraccion.startswith("[Error"):
        log.warn(
            f"Prompt 2 falló tras agotar reintentos: {extraccion[:120]} — "
            "sin extracción para esta causa",
            job_id=job_id, causa=numero,
        )
        extraccion = ""
    elif "Lo siento" in extraccion[:120]:
        log.warn(
            f"Prompt 2 rechazado por filtro de contenido: {extraccion[:120]}",
            job_id=job_id, causa=numero,
        )
        extraccion = ""
    else:
        resultado["extraccion"] = extraccion
        # Abogados: sección 5 (Partes Procesales) — se parsea del propio
        # texto que escribió el modelo, no del PDF crudo (el PDF no trae
        # el label "Abogado del Actor:" literal).
        resultado["abogado_actor"], resultado["abogado_demandado"] = buscar_abogados(extraccion)
        # Extraer campos clave
        if es_qwen:
            campos = _extraer_campos_qwen(extraccion, api_key, modelo, job_id, numero, flujo_proveedores)
        else:
            import openai as _oai, json as _json
            client_tmp = _oai.OpenAI(api_key=api_key)
            try:
                resp_tmp = client_tmp.chat.completions.create(
                    model=modelo,
                    messages=[{"role": "user", "content":
                        f"Del siguiente análisis de sentencia en materia {MATERIA.NOMBRE}, extrae los campos. "
                        "Si no consta, 'No consta'.\n\nTEXTO:\n" + extraccion[:12000]}],
                    max_tokens=GPT_MAX_TOKENS.get("json_extract", 400),
                    temperature=0,
                    response_format={"type": "json_schema", "json_schema": _SCHEMA_CAMPOS},
                )
                ch_tmp = resp_tmp.choices[0]
                campos = _json.loads(ch_tmp.message.content or "{}") if not getattr(ch_tmp.message, "refusal", None) else {}
            except Exception as e_tmp:
                log.warn(f"Extracción campos JSON: {type(e_tmp).__name__}: {e_tmp}", job_id=job_id, causa=numero)
                campos = {}

        resultado["cuantia"]             = campos.get("cuantia", "")
        resultado["tipo_accion"]         = campos.get("tipo_accion", "")
        resultado["objeto_controversia"] = campos.get("objeto_controversia", "")
        resultado["decision_final"]      = campos.get("decision_final", "")
        if not cuantia_vacia(resultado["cuantia"]):
            resultado["cuantia_origen"] = "qwen" if es_qwen else "gpt"

    # Fallback regex cuantía
    if cuantia_vacia(resultado["cuantia"]):
        cr = buscar_cuantia_en_texto(texto_rec)
        if cr:
            log.info(f"Cuantía recuperada por regex: {cr}", job_id=job_id, causa=numero)
            resultado["cuantia"]        = cr
            resultado["cuantia_origen"] = "regex"

    # Fallback regex otros campos
    if texto_rec:
        # El PDF crudo casi nunca trae la etiqueta "fecha de emisión" (la
        # fecha suele estar en el encabezado sin ese rótulo) — se prueba
        # también contra la propia extracción de la IA, que sí sigue el
        # formato "4. Fecha de Emisión:" pedido en el prompt.
        resultado["fecha_emision"] = (
            buscar_fecha_emision(texto_rec) or buscar_fecha_emision(extraccion)
        )
        resultado["juez_ponente"]  = buscar_juez_ponente(texto_rec)
        resultado["emails"]        = buscar_emails(texto_rec)

    time.sleep(GPT_PAUSA_SEG)

    # ── Prompt 3: Jurisprudencia ──────────────────────────────────────────────
    log.info("IA [3/4] Jurisprudencia y criterios", job_id=job_id, causa=numero)
    jurisprudencia, fr3 = _llamar(_p3_jurisp, GPT_MAX_TOKENS["jurisprudencia"])
    if fr3 == "length": truncados.append("Jurisprudencia")
    resultado["jurisprudencia"] = jurisprudencia
    time.sleep(GPT_PAUSA_SEG)

    # ── Prompts 4 y 7: Clasificación y tipos de defecto ──────────────────────
    clas = clasificar_y_tipificar(_llamar, _p4_clasif, _p7_tipos, texto_rec,
                                  demandado=demandado, job_id=job_id, numero=numero)
    administracion = clas["administracion"]
    clasificacion  = clas["categorias"]
    tipos_defecto  = clas["tipos_defecto"]
    truncados.extend(clas["truncados"])
    resultado["administracion"] = administracion
    resultado["categorias"]     = clasificacion
    resultado["tipos_defecto"]  = tipos_defecto

    # ── Prompt 9: evidencia ausente (interno) ────────────────────────────────
    # A diferencia del prompt 8 (tipo de prueba), este SÍ necesita la sentencia
    # completa: medido, con los campos ya extraídos la precisión fue 0% — el
    # pasaje donde el tribunal reprocha una prueba faltante vive en el cuerpo
    # del análisis, fuera de las secciones que se extraen.
    if _RE_EVIDENCIA_AUSENTE.search(texto_rec):
        log.info("IA [evidencia ausente] (interno)", job_id=job_id, causa=numero)
        evidencia_ausente, fr9 = _llamar(_p9_evidencia, GPT_MAX_TOKENS["evidencia_ausente"])
        resultado["evidencia_ausente"] = evidencia_ausente
    else:
        log.info("IA [evidencia ausente] omitido — sin gatillo en el texto",
                 job_id=job_id, causa=numero)

    # ── Controles deterministas (antes del Word y de lo que se guarda) ────────
    controles_info = controles.aplicar(resultado, texto_rec, voto, nivel="tribunal",
                                      prompts=[_p1_resumen, _p2_extrac, _p3_jurisp])
    for ajuste in controles_info["ajustes"]:
        log.info(f"Control: {ajuste}", job_id=job_id, causa=numero)

    # ── Generar Word ──────────────────────────────────────────────────────────
    regla_cero.exigir_sin_error_api(resultado)
    log.info("IA: Generando Ficha Técnica Word", job_id=job_id, causa=numero)
    docx = crear_docx_tribunal(
        numero, resultado["resumen"], resultado["extraccion"], resultado["jurisprudencia"], resultado["categorias"],
        nombre_tribunal=nombre_tribunal, ano=ano, job_id=job_id, causa=numero,
        perfil={"controversias": _codigos_controversia(resultado["tipos_defecto"])},
    )
    if docx:
        resultado["docx_bytes"] = docx
    else:
        log.warn("No se pudo generar el Word del tribunal", job_id=job_id, causa=numero)

    # ── Validación ────────────────────────────────────────────────────────────
    ok, problemas = _validar(resultado, texto_rec)
    for t in truncados:
        problemas.append(f"Respuesta truncada (max_tokens): {t}")
    problemas += controles_info["senales"]
    if truncados or controles_info["senales"]:
        ok = False
    resultado["validacion_ok"]        = ok
    resultado["validacion_problemas"] = problemas
    if not ok:
        log.warn(
            f"Validación: {len(problemas)} problema(s): {'; '.join(problemas)}",
            job_id=job_id, causa=numero,
        )

    # ── Auditoría de campos ────────────────────────────────────────────────────
    # otros_actos/pruebas buscan los encabezados "10. Otros Actos..."/
    # "13. Pruebas..." — ese formato lo escribe la IA (según el prompt), NO
    # existe literal en el PDF crudo de la sentencia. Tenía que revisar
    # resultado["extraccion"] (lo que escribió la IA), no texto_rec (el PDF
    # original) — por eso estos dos campos casi siempre se marcaban como
    # faltantes aunque la IA sí los hubiera extraído bien.
    auditoria = _auditoria_campos(resultado, resultado.get("extraccion", ""))
    resultado["_auditoria"] = auditoria
    faltantes = [k for k, presente in auditoria.items() if not presente]
    if faltantes:
        log.warn(f"Campos faltantes: {', '.join(faltantes)}", job_id=job_id, causa=numero)

    return resultado


def analizar_solo_jurisprudencia(pdf_bytes: bytes, numero: str, modelo: str = "gpt-4o",
                                  job_id: int = None, flujo_proveedores: str = "alibaba") -> dict:
    """
    Corre ÚNICAMENTE el prompt 3 (jurisprudencia y criterios) sobre una
    sentencia de tribunal ya conocida — para el backfill de causas donde
    ese campo quedó en "No consta" antes de un ajuste al prompt, sin
    tener que volver a correr resumen/extracción/clasificación (caro y
    ya sabemos que esos campos están bien).

    Retorna {"jurisprudencia": str, "error": str}.
    """
    resultado = {"jurisprudencia": "", "error": ""}
    es_qwen   = es_modelo_qwen(modelo)
    max_chars = QWEN_MAX_CHARS_INPUT if es_qwen else GPT_MAX_CHARS_INPUT

    if es_qwen:
        api_key = cargar_api_key_openrouter()
        if not api_key:
            resultado["error"] = "OPENROUTER_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado
    else:
        api_key = cargar_api_key_openai()
        if not api_key:
            resultado["error"] = "OPENAI_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado

    texto = extraer_texto_pdf(pdf_bytes, job_id=job_id, causa=numero)
    if not texto:
        resultado["error"] = "No se pudo extraer texto del PDF (PDF vacío, protegido o corrompido)"
        log.error(resultado["error"], job_id=job_id, causa=numero)
        return resultado
    texto_rec, _ = controles.separar_voto(texto[:max_chars])

    sistema    = cargar_prompt("sistema.txt")
    p3_jurisp  = cargar_prompt("prompt_3_jurisprudencia.txt")

    log.info(f"IA [solo jurisprudencia] {modelo.split('/')[-1]}", job_id=job_id, causa=numero)
    if es_qwen:
        from resolucion.ai._cliente import resolver_orden_proveedores
        texto_ia, finish = llamar_qwen(
            p3_jurisp + texto_rec, sistema, GPT_MAX_TOKENS["jurisprudencia"],
            api_key, modelo, job_id=job_id, causa=numero,
            orden_proveedores=resolver_orden_proveedores(flujo_proveedores),
        )
    else:
        texto_ia, finish = llamar_openai(
            p3_jurisp + texto_rec, sistema, GPT_MAX_TOKENS["jurisprudencia"],
            api_key, modelo, None, job_id=job_id, causa=numero,
        )
    if finish == "error" or texto_ia.startswith("[Error"):
        resultado["error"] = texto_ia
        return resultado
    resultado["jurisprudencia"] = texto_ia
    return resultado


def clasificar_tipo_prueba(objeto: str, pruebas_actor: str, pruebas_demandado: str,
                           criterio_sala: str, numero: str = "", modelo: str = "gpt-4o",
                           job_id: int = None, flujo_proveedores: str = "alibaba") -> dict:
    """
    Corre ÚNICAMENTE el prompt 8 (tipo de prueba por lado + por qué prosperó).

    A diferencia de los prompts 1-7, este NO recibe el PDF: toma los 4 campos
    YA extraídos por los prompts 2 y 3. No es una optimización cosmética —
    medido sobre el corpus real, el PDF completo promedia ~14.700 tokens vs
    ~900 de estos 4 campos, y como cada prompt vuelve a pagar su input, mandar
    el PDF acá costaría ~5-6x más sin aportar nada: el prompt solo necesita
    saber qué presentó cada parte y qué dijo el tribunal al valorarlo.

    Validado contra clasificación manual de 40 causas antes de integrarlo.

    Retorna {"tipo_prueba": str, "error": str} — el texto crudo se parsea en
    job_manager.guardar_tipos_prueba().
    """
    resultado = {"tipo_prueba": "", "error": ""}
    es_qwen   = es_modelo_qwen(modelo)

    if es_qwen:
        api_key = cargar_api_key_openrouter()
        if not api_key:
            resultado["error"] = "OPENROUTER_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado
    else:
        api_key = cargar_api_key_openai()
        if not api_key:
            resultado["error"] = "OPENAI_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado

    if not (pruebas_actor or pruebas_demandado):
        resultado["error"] = "Sin pruebas de ninguna parte — nada que clasificar"
        return resultado

    sistema = cargar_prompt("sistema.txt")
    p8      = cargar_prompt("prompt_8_tipo_prueba.txt")
    user    = (
        f"OBJETO DE LA CONTROVERSIA:\n{objeto or '(no consta)'}\n\n"
        f"PRUEBAS DEL ACTOR:\n{pruebas_actor or '(no consta)'}\n\n"
        f"PRUEBAS DEL DEMANDADO:\n{pruebas_demandado or '(no consta)'}\n\n"
        f"CRITERIO DEL JUZGADOR PARA RESOLVER:\n{criterio_sala or '(no consta)'}"
    )

    log.info("IA [tipo de prueba] (interno)", job_id=job_id, causa=numero)
    if es_qwen:
        from resolucion.ai._cliente import resolver_orden_proveedores
        texto_ia, finish = llamar_qwen(
            p8 + "\n\n" + user, sistema, GPT_MAX_TOKENS["tipo_prueba"],
            api_key, modelo, job_id=job_id, causa=numero,
            orden_proveedores=resolver_orden_proveedores(flujo_proveedores),
        )
    else:
        texto_ia, finish = llamar_openai(
            p8 + "\n\n" + user, sistema, GPT_MAX_TOKENS["tipo_prueba"],
            api_key, modelo, None, job_id=job_id, causa=numero,
        )
    if finish == "error" or texto_ia.startswith("[Error"):
        resultado["error"] = texto_ia
        return resultado
    resultado["tipo_prueba"] = texto_ia
    return resultado


def extraccion_estructurada(objeto: str, arg_demandado: str, pruebas_actor: str,
                            pruebas_demandado: str, jurisprudencia: str, criterio_sala: str,
                            favorable: bool, entidad: str = "", numero: str = "",
                            modelo: str = "gpt-4o", job_id: int = None,
                            flujo_proveedores: str = "alibaba") -> dict:
    """
    Corre el prompt 10 (extracción estructurada Nivel 2) y devuelve el JSON con
    los 9 campos de `causa_extraccion`: eje_decision, prueba_suficiente,
    tipo_prueba_clave, norma_clave, precedente_citado, tesis_contraparte,
    como_superar, clave_resultado, cita_textual.

    Reemplaza a scripts/extract_corpus.mjs (lacortedice-webapp), un script suelto
    que corrió una sola vez el 2026-07-06 y dejó 578 causas (16% del corpus) sin
    estos campos — rompiendo dos de los seis factores de atribución de jurimetría
    para esas causas, en silencio.

    OJO — deriva de modelo: las ~3600 filas históricas se generaron con
    gpt-4o-mini; esta corre con el modelo del job. En campos categóricos
    cerrados (eje_decision tiene 8 opciones) modelos distintos eligen distinto,
    y eso puede verse en analítica como un cambio real del corpus cuando es solo
    un cambio de modelo. Es una deriva ACEPTADA a conciencia y RASTREABLE:
    `causa_extraccion.model` guarda con qué modelo se generó cada fila, así que
    ante cualquier anomalía se puede segmentar por modelo para descartarla.

    Retorna {"campos": dict | None, "error": str}.
    """
    resultado = {"campos": None, "error": ""}
    es_qwen = es_modelo_qwen(modelo)

    if es_qwen:
        api_key = cargar_api_key_openrouter()
        if not api_key:
            resultado["error"] = "OPENROUTER_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado
    else:
        api_key = cargar_api_key_openai()
        if not api_key:
            resultado["error"] = "OPENAI_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado

    if not criterio_sala or len(criterio_sala.strip()) < 200:
        # Mismo umbral que usaba el script: sin razonamiento sustancial del
        # tribunal no hay nada que extraer y el modelo termina inventando.
        resultado["error"] = "criterio_sala_para_resolver muy corto — nada que extraer"
        return resultado

    sistema = cargar_prompt("sistema.txt")
    p10 = cargar_prompt("prompt_10_extraccion_estructurada.txt")
    user = (
        f"RESULTADO: {'favorable (prosperó el actor)' if favorable else 'desfavorable (no prosperó)'}\n"
        f"DEMANDADO: {entidad or '(no consta)'}\n\n"
        f"OBJETO DE LA CONTROVERSIA: {(objeto or '(no consta)')[:600]}\n"
        f"ARGUMENTOS DEL DEMANDADO: {(arg_demandado or '(no consta)')[:900]}\n"
        f"PRUEBAS DEL DEMANDADO: {(pruebas_demandado or '(no consta)')[:500]}\n"
        f"PRUEBAS DEL ACTOR: {(pruebas_actor or '(no consta)')[:600]}\n"
        f"JURISPRUDENCIA MENCIONADA: {(jurisprudencia or '(no consta)')[:900]}\n"
        f"RAZONAMIENTO DEL JUZGADOR: {criterio_sala[:2200]}"
    )

    log.info("IA [extraccion estructurada] (interno)", job_id=job_id, causa=numero)
    if es_qwen:
        from resolucion.ai._cliente import resolver_orden_proveedores
        texto_ia, finish = llamar_qwen(
            p10 + "\n\n" + user, sistema, GPT_MAX_TOKENS["extraccion_estructurada"],
            api_key, modelo, job_id=job_id, causa=numero,
            orden_proveedores=resolver_orden_proveedores(flujo_proveedores),
        )
    else:
        texto_ia, finish = llamar_openai(
            p10 + "\n\n" + user, sistema, GPT_MAX_TOKENS["extraccion_estructurada"],
            api_key, modelo, {"type": "json_object"}, job_id=job_id, causa=numero,
        )
    if finish == "error" or texto_ia.startswith("[Error"):
        resultado["error"] = texto_ia
        return resultado

    # El modelo puede envolver el JSON en ```json ... ``` aunque se le pida crudo.
    limpio = _re.sub(r"^```(?:json)?\s*|\s*```$", "", (texto_ia or "").strip())
    try:
        resultado["campos"] = json.loads(limpio)
    except Exception as e:
        resultado["error"] = f"JSON invalido: {type(e).__name__}: {str(e)[:120]}"
        log.error(resultado["error"], job_id=job_id, causa=numero)
    return resultado

_RE_TITULO_SECCION = re.compile(r"^\s*(\d{1,2})\.\s+\S", re.M)


def secciones_faltantes(texto: str, total: int) -> list:
    """Números de sección (1..total) que no aparecen como título 'N. ...' en la
    salida, a partir de la primera que falta (las anteriores se dan por buenas)."""
    presentes = {int(m.group(1)) for m in _RE_TITULO_SECCION.finditer(texto or "")}
    primera = next((n for n in range(1, total + 1) if n not in presentes), None)
    return [] if primera is None else list(range(primera, total + 1))


def recortar_seccion_incompleta(texto: str, primera_faltante: int) -> str:
    """Quita la última sección presente (la que se cortó a mitad) para que la
    continuación la rehaga completa."""
    ultima = primera_faltante - 1
    m = None
    for m in re.finditer(rf"^\s*{ultima}\.\s+\S", texto, re.M):
        pass
    return texto[:m.start()].rstrip() if m and ultima > 1 else texto.rstrip()


def instruccion_continuacion(desde: int, hasta: int) -> str:
    ultima = desde - 1
    return (f"\n\nCONTINUACIÓN: tu respuesta anterior se cortó por extensión. Escribe ahora SOLO las "
            f"secciones {max(ultima, 1)} a {hasta}, con los mismos títulos exactos y las mismas reglas; "
            f"no repitas las secciones anteriores ni agregues introducción.\n\nSENTENCIA:\n")
