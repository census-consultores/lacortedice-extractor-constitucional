"""
Utilidades de procesamiento de texto extraído de PDFs.

pypdf entrega texto crudo (sin tocar) con problemas conocidos: headers/
footers repetidos en cada página, números cortados por saltos de línea
("USD 50.\\n000" en vez de "USD 50.000"), separadores tipográficos.
Estos helpers limpian el texto antes de que el modelo lo lea, sin tocar
contenido sustantivo.

Reutilizado por:
  - resolucion/gpt.py       (sentencias del tribunal)
  - resolucion/gpt_cnj.py   (sentencias de casación CNJ)
  - apertura/extractor_simple.py (demandas sin GPT, solo regex)
"""

import base64
import re
from collections import Counter
from pathlib import Path


# ─── Ejemplos few-shot (carga desde ejemplos/{tribunal,corte}/) ─────────────

def cargar_ejemplos_few_shot(carpeta: Path, limite_chars: int = 80_000) -> str:
    """Lee todos los archivos .md / .txt de `carpeta` y los concatena en un
    bloque de texto con separadores claros, listo para inyectar dentro del
    prompt antes del marcador "SENTENCIA A ANALIZAR:".

    Devuelve "" si la carpeta no existe o está vacía.

    `limite_chars` (default 80k chars ≈ 20k tokens) corta cuando los ejemplos
    suman demasiado, para no saturar el contexto del modelo cuando el cliente
    pega ejemplos enormes. Los ejemplos pesan más que el texto de la sentencia
    en promedio, y gpt-4o aguanta 128k tokens total.
    """
    if not carpeta.exists() or not carpeta.is_dir():
        return ""
    # Acepta .md y .txt — el cliente puede usar el que prefiera.
    archivos = sorted(
        list(carpeta.glob("*.md")) + list(carpeta.glob("*.txt"))
    )
    # Filtrar archivos vacíos y dotfiles.
    archivos = [a for a in archivos if a.is_file() and a.stat().st_size > 0
                and not a.name.startswith(".")]
    if not archivos:
        return ""

    bloques = []
    total = 0
    for i, ruta in enumerate(archivos, 1):
        try:
            contenido = ruta.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not contenido:
            continue
        encabezado = f"--- EJEMPLO {i} ({ruta.stem}) ---"
        bloque = f"{encabezado}\n{contenido}\n"
        if total + len(bloque) > limite_chars:
            # Cortamos acá para no exceder el límite. Lo logueamos.
            print(f"  [INFO ejemplos] Corte por límite de chars en ejemplo {i} ({ruta.name})")
            break
        bloques.append(bloque)
        total += len(bloque)

    if not bloques:
        return ""
    return "\n".join(bloques).rstrip() + "\n"


# ─── Limpieza del texto de pypdf ─────────────────────────────────────────────

# Patrones de líneas que son ruido (numeración de página, separadores).
_RE_PAG_NUMERO  = re.compile(r"^\s*(p[áa]gina\s+)?\d+\s*(de|/)\s*\d+\s*$", re.IGNORECASE)
_RE_SOLO_DIGITO = re.compile(r"^\s*\d{1,3}\s*$")
_RE_SEPARADOR   = re.compile(r"^[\s\-_=•·\.]+$")

# Une dígitos cortados por salto de línea: "USD 50.\n000" -> "USD 50.000".
# Conservador: requiere que el primer grupo termine en separador (., , ')
# para no juntar números legítimamente distintos (ej. "USD 50.000,00\n16. " no
# debe quedar "USD 50.000,0016. ").
_RE_NUMERO_ROTO = re.compile(r"(\d+[.,'])\n\s*(\d)")

# Colapsa espacios horizontales múltiples (no toca saltos de línea).
_RE_ESPACIOS_MULT = re.compile(r"[ \t]{2,}")

# Más de 3 saltos de línea seguidos -> 2 (preserva separación de párrafos).
_RE_SALTOS_MULT = re.compile(r"\n{3,}")


def limpiar_texto_pypdf(paginas: list) -> str:
    """Limpia el texto crudo de pypdf antes de mandárselo a GPT (o al parser regex).

    Recibe la lista de strings (uno por página, en el orden del PDF) y devuelve
    el texto unificado y limpio. Pasos:

      1. Detecta líneas cortas que se repiten en >=1/3 de las páginas
         (header/footer del tribunal, "Página X de Y", etc.) y las elimina.
      2. Quita líneas que son solo número de página o separadores.
      3. Une números rotos por salto de línea (en cascada si hace falta).
      4. Colapsa espacios y saltos de línea excesivos.

    Conservador: si una página es muy corta (<5 líneas) la deja intacta —
    probablemente es portada y no tiene ruido.
    """
    if not paginas:
        return ""

    # 1) Detectar headers/footers: líneas cortas que se repiten en muchas páginas.
    contador = Counter()
    for pag in paginas:
        lineas = [l.strip() for l in pag.splitlines() if l.strip()]
        # Solo consideramos primeras 3 y últimas 3 líneas (donde van headers/footers).
        candidatas = set(lineas[:3] + lineas[-3:])
        for linea in candidatas:
            if 0 < len(linea) <= 80:
                contador[linea] += 1

    umbral_repeticion = max(3, len(paginas) // 3)
    ruido_repetido = {l for l, c in contador.items() if c >= umbral_repeticion}

    # 2) Limpiar cada página.
    paginas_limpias = []
    for pag in paginas:
        lineas = pag.splitlines()
        if len(lineas) < 5:
            paginas_limpias.append(pag)
            continue
        out = []
        for linea in lineas:
            stripped = linea.strip()
            if not stripped:
                out.append("")
                continue
            if stripped in ruido_repetido:
                continue
            if _RE_PAG_NUMERO.match(stripped) or _RE_SEPARADOR.match(stripped):
                continue
            # Dígito aislado en línea propia: header/footer numérico.
            if _RE_SOLO_DIGITO.match(stripped) and len(stripped) <= 3:
                continue
            out.append(linea)
        paginas_limpias.append("\n".join(out))

    texto = "\n".join(paginas_limpias)

    # 3) Unir números rotos. Aplicar 2 veces porque puede haber rupturas en
    #    cascada (ej. "50.\n000.\n000" requiere dos pasadas).
    for _ in range(2):
        texto_nuevo = _RE_NUMERO_ROTO.sub(r"\1\2", texto)
        if texto_nuevo == texto:
            break
        texto = texto_nuevo

    # 4) Normalizar espacios y saltos de línea.
    texto = _RE_ESPACIOS_MULT.sub(" ", texto)
    texto = _RE_SALTOS_MULT.sub("\n\n", texto)

    return texto.strip()


# ─── Fallback regex para cuantía ──────────────────────────────────────────────

# Cubre el caso "el dato está ahí pero GPT lo perdió". Conservador a propósito:
# solo activa cuando hay un label explícito de "Cuantía" — no escanea cualquier
# monto suelto del texto para evitar capturar valores de glosas individuales
# en vez del total.
_RE_CUANTIA_FALLBACK = re.compile(
    r"cuant[ií]a"
    r"(?:\s+(?:del\s+caso|del\s+juicio|del\s+proceso|total))?"
    r"[\s\:\-\.\)]{0,40}"
    r"(?P<monto>"
    r"(?:USD|US\$|U\.S\.\$|\$)\s*[\d][\d\.,'\s]{1,30}"
    r"|"
    r"[\d][\d\.,'\s]{1,30}\s*(?:USD|d[óo]lares|d[óo]lar)"
    r")",
    re.IGNORECASE,
)


def buscar_cuantia_en_texto(texto: str) -> str:
    """Busca la cuantía con regex sobre el texto del PDF. Devuelve "" si
    no encuentra nada con suficiente confianza."""
    if not texto:
        return ""
    m = _RE_CUANTIA_FALLBACK.search(texto)
    if not m:
        return ""
    monto = re.sub(r"\s+", " ", m.group("monto")).strip(" .,;:-")
    # Necesita al menos 2 dígitos para considerarse válido.
    digitos = sum(1 for c in monto if c.isdigit())
    if digitos < 2:
        return ""
    return monto


def cuantia_vacia(valor: str) -> bool:
    """True si la cuantía devuelta por GPT debe ser reemplazada por el
    fallback regex (vacía, 'No consta', 'Indeterminada', o sin dígitos).
    'Indeterminada' es un valor válido en demandas — lo dejamos como vacío
    para que el caller decida si activar fallback (en sentencias sí, en
    demandas no porque 'Indeterminada' ahí significa exactamente eso)."""
    v = (valor or "").strip()
    if not v:
        return True
    if "no consta" in v.lower():
        return True
    if not any(c.isdigit() for c in v):
        return True
    return False


# ─── Regex de más campos clave ────────────────────────────────────────────────
# Fallbacks para cuando GPT no encuentra estos campos en el texto. Patrones
# conservadores que solo capturan cuando el contexto es claro.

# Fecha en formato DD/MM/AAAA o DD-MM-AAAA cerca de "fecha de emisión".
_RE_FECHA_EMISION = re.compile(
    r"fecha\s+de\s+emisi[óo]n[\s\:\-\.]*"
    r"(?P<fecha>\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})",
    re.IGNORECASE,
)

# Fecha escrita en palabras ("22 de abril del 2019") cerca de "fecha de
# emisión" — las sentencias casi nunca traen el formato numérico, lo
# habitual es la fecha en prosa. Sin esto, buscar_fecha_emision() nunca
# encontraba nada aunque la IA sí identificara la fecha correctamente.
_RE_FECHA_EMISION_PROSA = re.compile(
    r"fecha\s+de\s+emisi[óo]n[\s\:\-\.]*"
    r"(?P<fecha>\d{1,2}\s+de\s+\w+\s+del?\s+\d{4})",
    re.IGNORECASE,
)

# Número de juicio formato XXXXX-AAAA-XXXXX (5-4-5 dígitos). Ecuador.
_RE_NUMERO_JUICIO = re.compile(r"\b(\d{5}-\d{4}-\d{5})\b")

# Email simple.
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Juez Ponente: línea "Juez Ponente: Dr. Nombre Apellido" o variantes.
_RE_JUEZ_PONENTE = re.compile(
    r"juez(?:a)?\s+ponente[\s\:\-\.]*"
    r"(?P<nombre>(?:Dr\.?a?|Ab\.?|Mgs\.?)?\s*[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ\.\s]{3,80}?)"
    r"(?=\n|$|;|,\s+ponente|\.\s|,\s+(?:Dr|Ab|otros))",
    re.IGNORECASE,
)

# "Abogado del Actor:" / "Abogado del Demandado:" — sección 5 (Partes
# Procesales) del prompt de extracción (prompt_2_extraccion.txt). Captura
# hasta el próximo bullet/sección, no hasta fin de texto.
_RE_ABOGADO_ACTOR = re.compile(
    r"abogado\s+del\s+actor[\s\:\-\.]*\n?\s*"
    r"(?P<nombre>.+?)"
    r"(?=\n\s*[●•\*\-]?\s*(?:demandado|6[\.\:]|$))",
    re.IGNORECASE | re.DOTALL,
)
_RE_ABOGADO_DEMANDADO = re.compile(
    r"abogado\s+del\s+demandado[\s\:\-\.]*\n?\s*"
    r"(?P<nombre>.+?)"
    r"(?=\n\s*[●•\*\-]?\s*(?:6[\.\:]|tipo\s+de\s+acci[óo]n|$))",
    re.IGNORECASE | re.DOTALL,
)


def buscar_fecha_emision(texto: str) -> str:
    """
    Busca la fecha de emisión en el texto, en formato numérico
    ('Fecha de emisión: 22/04/2019') o en prosa ('Fecha de emisión: 22 de
    abril del 2019' — el formato real más común en sentencias). '' si no
    encuentra ninguno.
    """
    if not texto:
        return ""
    m = _RE_FECHA_EMISION.search(texto)
    if m:
        return m.group("fecha")
    m = _RE_FECHA_EMISION_PROSA.search(texto)
    return m.group("fecha") if m else ""


def buscar_numero_juicio(texto: str) -> str:
    """Busca el primer patrón XXXXX-AAAA-XXXXX en el texto. '' si no encuentra."""
    if not texto:
        return ""
    m = _RE_NUMERO_JUICIO.search(texto)
    return m.group(1) if m else ""


def buscar_emails(texto: str) -> str:
    """Devuelve todos los emails únicos del texto unidos por '; '. '' si no hay.
    Filtra correos genéricos del SATJE/Ecuador (admin@, info@, no-reply@)."""
    if not texto:
        return ""
    crudos = _RE_EMAIL.findall(texto)
    if not crudos:
        return ""
    vistos = []
    bloqueados = ("admin@", "info@", "no-reply@", "noreply@", "soporte@")
    for e in crudos:
        e_low = e.lower()
        if any(e_low.startswith(b) for b in bloqueados):
            continue
        if e_low not in [v.lower() for v in vistos]:
            vistos.append(e)
    return "; ".join(vistos[:5])  # máx 5 para no saturar el Excel


# Roles/cargos genéricos que NO son un nombre propio. Si la sentencia no
# identifica al abogado, el modelo a veces escribe el rol ("procurador
# judicial") — no debe inferirse ni guardarse: el campo queda vacío (#4).
# _EXACTOS: solo se descartan si el valor completo ES el rol (sin nombre).
_ROLES_EXACTOS = (
    "abogado", "abogada", "defensor", "defensora", "procurador", "procuradora",
    "no aplica", "no identificado", "no se identifica", "s/n", "n/a",
)
# _PREFIJO: frases-rol que suelen venir con un calificativo detrás
# ("procurador judicial de la parte…") pero siguen sin ser un nombre.
_ROLES_PREFIJO = (
    "procurador judicial", "procuradora judicial",
    "procurador fiscal", "procuradora fiscal",
    "defensa tecnica", "defensor publico", "defensora publica",
    "casillero judicial", "abogado defensor", "abogado patrocinador",
    "procurador comun",
)

# Cargo/función al INICIO del texto → es un cargo, no un nombre propio. Si el
# abogado no está nombrado, mejor dejar el campo vacío que guardar el cargo
# (ej. "Procuradora Fiscal del Director Zonal 8 del Servicio de Rentas...").
_CARGO_INICIO = (
    "procurador", "procuradora", "director", "directora", "subdirector",
    "subdirectora", "delegado", "delegada", "fiscal", "defensor", "defensora",
    "ministro", "ministra", "secretario", "secretaria", "gerente",
    "representante", "apoderado", "apoderada", "jefe", "jefa",
    "coordinador", "coordinadora", "administrador", "administradora",
)

# Honoríficos/títulos al inicio del nombre — se guardan pelados (sin "Ab.", "Dr.").
_HONORIFICOS_ABOGADO = re.compile(
    r"^(?:ab|abg|abog|dr|dra|ing|econ|eco|lcdo|lcda|lic|mgs|mgtr|msc|phd|sr|sra|srta)\.?\s+",
    re.IGNORECASE,
)


def limpiar_nombre_abogado(nombre: str) -> str:
    """Normaliza el nombre de un abogado: quita markdown y honoríficos ('Ab.',
    'Dr.'), y devuelve '' si es solo un rol/cargo (no un nombre propio)."""
    if not nombre:
        return ""
    nombre = re.sub(r"[\*#_]", "", nombre)
    # Quitar honoríficos al inicio, repetidamente ("Ab. Dr. Juan" → "Juan").
    prev = None
    while prev != nombre:
        prev = nombre
        nombre = _HONORIFICOS_ABOGADO.sub("", nombre).strip()
    nombre = re.sub(r"\s+", " ", nombre).strip(" .,;:-\n")
    # Cortar cláusula de cargo al final tras coma: ", en (su) calidad de
    # procuradora fiscal", ", procurador judicial", ", en representación de…".
    # Queda solo el nombre propio (#4).
    nombre = re.sub(
        r"\s*,\s*(?:en\s+(?:su\s+)?calidad\s+de|en\s+representaci[óo]n\s+de|"
        r"en\s+su\s+condici[óo]n\s+de|procurador\w*|delegad\w*|apoderad\w*|"
        r"fiscal\b|defensor\w*).*$",
        "", nombre, flags=re.IGNORECASE,
    ).strip(" .,;:-")
    # Quitar matrícula / número de registro al final del nombre, que arrastra el
    # fallback de escritos. Dos casos:
    #  a) con etiqueta explícita y valor alfanumérico: "Mat. 17-2004-260-F.a",
    #     "Foro No. 987", "Registro 12-3" → se quita todo.
    #  b) número suelto sin etiqueta al final: "Eduardo Ledesma H., 7515".
    nombre = re.sub(
        r"[\s,;:.\-]*(?:mat(?:r[íi]cula)?|foro|casillero(?:\s+judicial)?|"
        r"reg(?:istro)?|colegiatura)\.?\s*(?:n[o°º]\.?\s*)?[\d][\w.\-]*\.?\s*$",
        "", nombre, flags=re.IGNORECASE,
    ).strip(" .,;:-")
    nombre = re.sub(r"[\s,;:.\-]+\d{2,6}\)?\.?\s*$", "", nombre).strip(" .,;:-")
    if not nombre or len(nombre) < 4 or "no consta" in nombre.lower():
        return ""
    # #4: si es solo un rol/cargo genérico (sin nombre propio), dejar vacío.
    import unicodedata
    base = unicodedata.normalize("NFD", nombre.lower()).encode("ascii", "ignore").decode("ascii")
    base = base.strip(" .,;:-")
    if base in _ROLES_EXACTOS:
        return ""
    if base in _ROLES_PREFIJO or any(base.startswith(r + " ") for r in _ROLES_PREFIJO):
        return ""
    if base.split(" ", 1)[0] in _CARGO_INICIO:
        return ""
    return nombre[:255]


def _limpiar_nombre_abogado(m) -> str:
    return limpiar_nombre_abogado(m.group("nombre")) if m else ""


def buscar_abogados(texto_extraccion: str) -> tuple:
    """
    Busca 'Abogado del Actor:' y 'Abogado del Demandado:' en el texto de
    extracción (sección 5, Partes Procesales — prompt_2_extraccion.txt).
    A diferencia de buscar_juez_ponente/buscar_fecha_emision (que operan
    sobre el PDF crudo como fallback), esto opera sobre lo que el modelo
    ya escribió — el PDF original no trae esos labels literales.

    Devuelve (abogado_actor, abogado_demandado); '' en cada uno si no consta.
    """
    if not texto_extraccion:
        return "", ""
    actor     = _limpiar_nombre_abogado(_RE_ABOGADO_ACTOR.search(texto_extraccion))
    demandado = _limpiar_nombre_abogado(_RE_ABOGADO_DEMANDADO.search(texto_extraccion))
    return actor, demandado


def buscar_juez_ponente(texto: str) -> str:
    """Busca 'Juez Ponente: Nombre' en el texto. '' si no encuentra.
    Conservador: solo captura cuando el label está explícito."""
    if not texto:
        return ""
    m = _RE_JUEZ_PONENTE.search(texto)
    if not m:
        return ""
    nombre = re.sub(r"\s+", " ", m.group("nombre")).strip(" .,;:-")
    # Filtrar capturas degeneradas (sólo título o muy corto)
    if len(nombre) < 5 or nombre.lower() in ("dr.", "dra.", "ab."):
        return ""
    return nombre


# ─── Validación de legibilidad del texto extraído ────────────────────────────

def texto_legible(texto: str) -> tuple:
    """Determina si el texto extraído del PDF es suficiente para GPT en modo
    texto. Si no, conviene caer a GPT-Vision sobre las imágenes.
    Retorna (es_legible: bool, motivo: str)."""
    if not texto or not texto.strip():
        return False, "Sin texto extraíble (PDF escaneado)"
    n_chars = len(texto)
    if n_chars < 500:
        return False, f"Texto muy corto ({n_chars} chars)"
    palabras = texto.split()
    if len(palabras) < 80:
        return False, f"Muy pocas palabras ({len(palabras)})"
    # Densidad de palabras comunes del español
    texto_lower = texto.lower()
    comunes = ["que ", " de ", " la ", " el ", " los ", " las ", " en ", " no ", " con ", " por "]
    coincidencias = sum(1 for c in comunes if c in texto_lower)
    if coincidencias < 4:
        return False, "Densidad muy baja de español (OCR malo o escaneado)"
    return True, f"Texto OK ({n_chars} chars, {len(palabras)} palabras)"


# ─── PDF → imágenes base64 (PyMuPDF) ──────────────────────────────────────────

def pdf_a_imagenes_b64(pdf_bytes: bytes, max_paginas: int = 12, dpi: int = 150,
                       pagina_inicio: int = 0) -> list:
    """Convierte páginas de un PDF a PNG base64, listas para GPT-Vision.

    pagina_inicio: índice 0-based de la primera página a renderizar.
    max_paginas:   cuántas páginas renderizar a partir de pagina_inicio.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise ImportError("PyMuPDF no instalado. Ejecuta: pip3 install pymupdf")

    imagenes = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    fin = min(len(doc), pagina_inicio + max_paginas)
    zoom = dpi / 72  # 72 dpi es el default de PyMuPDF
    mat = fitz.Matrix(zoom, zoom)
    for i in range(pagina_inicio, fin):
        page = doc[i]
        pix = page.get_pixmap(matrix=mat)
        png_bytes = pix.tobytes("png")
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        imagenes.append(b64)
    doc.close()
    return imagenes
