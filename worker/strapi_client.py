"""
Cliente HTTP para Strapi. Guarda causas y sube PDFs via REST API.
"""

import os
import re as _re
from config import materia as MATERIA
import httpx
from typing import Optional

from resolucion.clasificar_actor import normalizar_case_nombre


# ── Helpers de normalización y parsing ───────────────────────────────────────

def _normalizar(texto: str) -> str:
    """Minúsculas + quita tildes para comparación fuzzy."""
    import unicodedata
    return unicodedata.normalize("NFD", (texto or "").lower().strip()).encode(
        "ascii", "ignore"
    ).decode("ascii")


_HONORIFICOS_PERSONA = {"dr", "dra", "ab", "abg", "sr", "sra", "ing", "econ", "mgs", "msc"}

# Nota: los sufijos societarios (S.A., Cía. Ltda., etc.) no distinguen a la
# persona jurídica al comparar el nombre del actor y se eliminan al armar la
# CLAVE — nunca al reescribir el nombre mostrado. No se tocan
# "consorcio"/"cooperativa"/"compañía", que sí son parte distintiva.
def _n_tildes(s: str) -> int:
    """Cuenta caracteres acentuados/ñ (no ASCII) — para preferir, en empate, la
    escritura correcta del nombre sobre la versión sin tildes."""
    return sum(1 for ch in (s or "") if ord(ch) > 127)


def _clave_actor(nombre: str) -> str:
    """Clave fuerte para agrupar variantes del mismo actor escrito distinto:
    sin tildes, sin sufijos societarios (con o sin puntos), sin puntuación
    (guion/ampersand incluidos) y con espacios colapsados.
    'Grupo-Universal S.A' y 'Grupo Universal S.A.' → 'grupo universal'."""
    n = _normalizar(nombre)                       # minúsculas, sin tildes, conserva puntuación
    n = n.replace('-', ' ').replace('&', ' ')     # guion/ampersand → espacio
    # Sufijos societarios con o sin puntos: s.a, s.a.s, c.a, cia, c.i.a, ltda,
    # cltda, c.l, e.p, s.r.l, eirl, sc.
    n = _re.sub(
        r'\b(s\.?a\.?s?|c\.?a\.?|c\.?i\.?a\.?|cia|c?ltda|c\.?l\.?|e\.?p\.?|s\.?r\.?l\.?|eirl|sc)\b',
        ' ', n,
    )
    n = _re.sub(r'[^a-z0-9 ]', ' ', n)            # resto de puntuación → espacio
    return _re.sub(r'\s+', ' ', n).strip()


def _tokens_nombre(nombre: str) -> frozenset:
    """
    Normaliza un nombre de persona a un set de palabras significativas —
    para reconocer variantes del mismo juez/abogado con distinto orden
    ("Cohn Zurita Fernando Antonio" vs "Fernando Antonio Cohn Zurita"),
    honoríficos ("Dr. ..."), o un nombre de más/de menos ("Gustavo
    Durango Vela" vs "Gustavo Adolfo Durango Vela") sin depender de que
    sean substring literal uno del otro. Sin esto cada variante que
    escribe el modelo termina como un registro nuevo — visto en
    producción con jueces reales duplicados 2-3 veces por causa.
    """
    n = _normalizar(nombre)
    n = _re.sub(r'\([^)]*\)', ' ', n)  # (E), (Ponente), (Conjuez) no es parte del nombre
    palabras = [p for p in _re.split(r'\W+', n) if p and p not in _HONORIFICOS_PERSONA]
    return frozenset(palabras)


def _misma_persona(a: frozenset, b: frozenset) -> bool:
    """Mismo criterio que _tokens_nombre: subconjunto en cualquier dirección."""
    return bool(a) and bool(b) and (a <= b or b <= a)


# Honoríficos al inicio de un nombre — se quitan al guardar ("Ab. Juan" → "Juan").
_RE_HONORIFICO = _re.compile(
    r'^(?:dr|dra|ab|abg|abog|mgs|mgtr|msc|ing|econ|eco|lcdo|lcda|lic|sr|sra|srta|phd)\.?\s+',
    _re.IGNORECASE,
)


def _sin_honorifico(nombre: str) -> str:
    """Quita títulos honoríficos al inicio, repetidamente ('Ab. Dr. Juan' → 'Juan')."""
    prev = None
    n = (nombre or "").strip()
    while prev != n:
        prev = n
        n = _RE_HONORIFICO.sub('', n).strip()
    return n


# Separa nombres de jueces por ";" o por ". " — pero NO por el punto de un
# honorífico ("Dr.", "Ab."). Qwen suele listar los otros jueces separados con
# ". " ("Dr. Carlos Ferrín de la Torre. Ab. Néstor Gómez Jaramillo").
_RE_SEP_JUECES = _re.compile(
    r'\s*;\s*'
    r'|(?<!Dr)(?<!Dra)(?<!Ab)(?<!Abg)(?<!Abog)(?<!Mgs)(?<!Msc)(?<!Ing)'
    r'(?<!Sr)(?<!Sra)(?<!Lic)\.\s+'
)


def _es_multi_persona(nombre: str) -> bool:
    """
    True si el nombre parece contener DOS o más jueces concatenados
    (basura de extracciones viejas, ej. "Gómez Jaramillo. Ferrín De la Torre").
    Se usa para NO enganchar un juez individual a esos registros contaminados.
    """
    partes = [p for p in _RE_SEP_JUECES.split(nombre or "") if p and p.strip()]
    if len(partes) > 1:
        return True
    # Un juez real rara vez tiene más de 6 palabras de nombre; más = 2 personas.
    return len(_tokens_nombre(nombre or "")) > 6


def _dedup_jueces(nombres: list, limite: int | None = None) -> list:
    """
    Deduplica una lista de nombres de jueces por persona (variantes de orden,
    honoríficos o acentos cuentan como el mismo) y, si se da `limite`, la corta.
    El Tribunal lo integran 3 jueces: ponente + 2 → limite=3 en primera instancia.
    """
    vistos: list = []
    out: list = []
    for n in nombres:
        toks = _tokens_nombre(n)
        if not toks:
            continue
        if any(_misma_persona(toks, v) for v in vistos):
            continue
        vistos.append(toks)
        out.append(n)
        if limite and len(out) >= limite:
            break
    return out


def _mejor_coincidencia(texto_n: str, candidatos: list) -> Optional[dict]:
    """
    Busca, entre `candidatos` (lista de dicts con 'attributes'/'nombre'), el
    que mejor matchea `texto_n`, en tres pasadas:

    1. Match exacto — siempre gana si existe (ej. texto_n ya es una
       etiqueta corta clasificada de antemano, como en decisión de
       casación: "se caso" debe ganarle a "se caso parcialmente" aunque
       ese también "contenga" a texto_n).
    2. nombre_n es substring DE texto_n (texto_n es una oración larga que
       contiene la etiqueta) — se prefiere el nombre_n más LARGO, o sea el
       más específico. Sin esto "Con lugar" matchea como substring de
       "declara parcialmente con lugar" y gana antes de llegar a la entrada
       correcta "Parcialmente con lugar" — bug real visto en producción.
    3. texto_n es substring DE nombre_n (texto_n es corto y nombre_n una
       variante más larga que lo contiene) — acá se prefiere el nombre_n
       más CORTO, el superset más ajustado.
    """
    def nombre_de(e):
        return _normalizar((e.get("attributes", e)).get("nombre", ""))

    for e in candidatos:
        if nombre_de(e) == texto_n and texto_n:
            return e

    contenidos = [e for e in candidatos if nombre_de(e) and nombre_de(e) in texto_n]
    if contenidos:
        return max(contenidos, key=lambda e: len(nombre_de(e)))

    contenedores = [e for e in candidatos if nombre_de(e) and texto_n in nombre_de(e)]
    if contenedores:
        return min(contenedores, key=lambda e: len(nombre_de(e)))

    return None


# El "Demandado" de SATJE es el cargo + nombre del funcionario de turno
# ("Directora General del SRI Ec. Marisol Andrade", "Director Distrital de
# Huaquillas del Servicio Nacional de Aduana", con typos como "SEANE"). La
# administración tributaria demandada es SIEMPRE una de SOLO 3: SRI (rentas),
# SENAE (aduanas) o, para todo lo demás, GAD (Gobiernos Autónomos
# Descentralizados). Así no se crea una entidad nueva por cada funcionario/GAD.
_ENTIDAD_SRI   = _re.compile(r'rentas\s+internas|\bsri\b', _re.IGNORECASE)
_ENTIDAD_SENAE = _re.compile(r'aduan|\bsenae\b|\bseane\b|\bcae\b', _re.IGNORECASE)
_ENTIDAD_GAD   = _re.compile(r'municip|\bgad\b|gobierno\s+aut[oó]nomo|prefectura|consejo\s+provincial|alcald', _re.IGNORECASE)
_ADM_A_ENTIDAD = {
    "SRI":   "Servicio de Rentas Internas",
    "SENAE": "Servicio Nacional de Aduana del Ecuador",
    "GAD":   "Gobiernos Autónomos Descentralizados",
}


def _canonizar_entidad(texto: str, adm_hint: Optional[str] = None) -> str:
    """Mapea el demandado a una de las 3 entidades: SRI, SENAE o GAD.

    Prioridad: (1) lo que nombra EXPLÍCITAMENTE el texto de la parte demandada
    (SRI/SENAE/GAD por palabra clave); (2) si el texto es ambiguo, la
    administración que determinó la CLASIFICACIÓN de contenido (prompt 4), que
    sí lee el catálogo estructurado por entidad — así entidad y categorías
    quedan consistentes; (3) GAD por defecto solo si no hay ninguna señal."""
    t = texto or ""
    # Materias sin administraciones (laboral, civil…): el demandado es
    # cualquier persona o empresa; se devuelve normalizado, sin colapsar a 3.
    if not MATERIA.ADMINISTRACIONES:
        return normalizar_case_nombre(t.strip()) or t.strip()
    if _ENTIDAD_SRI.search(t):
        return _ADM_A_ENTIDAD["SRI"]
    if _ENTIDAD_SENAE.search(t):
        return _ADM_A_ENTIDAD["SENAE"]
    if _ENTIDAD_GAD.search(t):
        return _ADM_A_ENTIDAD["GAD"]
    hint = (adm_hint or "").strip().upper()
    if hint in _ADM_A_ENTIDAD:
        return _ADM_A_ENTIDAD[hint]
    return _ADM_A_ENTIDAD["GAD"]


def _clasificar_decision_apelacion(texto: str) -> Optional[str]:
    """Normaliza la decisión de segunda instancia a una de las 4 frases
    canónicas de MATERIA.DECISIONES_APELACION (Confirma / Revoca / Reforma /
    Declara la nulidad). Devuelve None si el texto no permite decidir."""
    t = _normalizar(texto)
    if not t:
        return None
    if "nulidad" in t:
        return "Declara la nulidad."
    if "reforma" in t or "modifica" in t or "parcial" in t:
        return "Reforma la sentencia."
    if "revoca" in t or "deja sin efecto" in t:
        return "Revoca la sentencia."
    if "confirma" in t or "ratifica" in t or "rechaza el recurso" in t or "niega el recurso" in t:
        return "Confirma la sentencia."
    return None


def _clasificar_decision_tribunal(texto: str) -> Optional[str]:
    """Clasifica la decisión de una sentencia de tribunal a uno de los 3
    términos del catálogo: 'con lugar', 'sin lugar' o 'parcialmente con lugar'.
    Sirve tanto para la etiqueta del AI ("No declara con lugar") como para el
    texto de la resolución ("declara sin lugar", "rechaza la demanda…").

    Reglas clave (del control de calidad):
    - "No declara con lugar" / "no ha lugar" = SIN lugar, aunque la frase
      contenga literalmente "con lugar" (el bug era mapearlo a "Con lugar").
    - Si conviven aceptación y rechazo (se acepta algo y se niega algo) = PARCIAL.
    - "rechaza" / "ratifica o confirma la validez/legalidad" del acto = sin lugar.
    """
    t = _normalizar(texto)
    if not t:
        return None
    if "parcial" in t:
        return "parcialmente con lugar"
    niega_con_lugar = bool(
        _re.search(r'no\s+(?:se\s+)?(?:declara|ha)\s+(?:con\s+)?lugar', t) or "sin lugar" in t
    )
    rechazo = niega_con_lugar or bool(_re.search(
        r'\b(rechaza|niega|desecha|deniega|inadmit|improceden|'
        r'ratifica\s+la\s+(?:validez|legalidad)|confirma\s+la\s+(?:validez|legalidad))', t
    ))
    acepta = (not niega_con_lugar) and bool(_re.search(
        r'\b(declara\s+con\s+lugar|acepta|se\s+acepta|ha\s+lugar|con\s+lugar)\b', t
    ))
    if acepta and rechazo:
        return "parcialmente con lugar"
    if rechazo:
        return "sin lugar"
    if acepta:
        return "con lugar"
    return None


def _limpiar_md(texto: str) -> str:
    """
    Elimina artefactos de markdown que el LLM introduce en campos de texto y
    UNIFORMA las viñetas a punto negro "• " (#10, #11).

    Nota: las viñetas se normalizan ANTES de tocar los asteriscos, para que un
    ítem escrito como "* algo" se convierta en "• algo" y no lo devore la regla
    de *cursiva*. No se toca "?" (es puntuación válida en español); sí se quitan
    los residuos sueltos que arrastra el modelo: *, ´, `, =, ~ (#7).
    """
    if not texto:
        return ""
    import re
    # 1) Viñetas: cualquier marcador al inicio de línea (guion, asterisco o
    #    bullet unicode) → "• " uniforme. Exige espacio detrás para no comerse
    #    "-5" (negativos) ni "**Título**" (que resuelve la regla de negrita).
    texto = re.sub(r'^[ \t]*[-*○●•◦▪▸►→·▷]+[ \t]+', '• ', texto, flags=re.MULTILINE)
    # 2) Énfasis y encabezados markdown.
    texto = re.sub(r'\*{2,}([^*]*?)\*{2,}', r'\1', texto)   # **negrita** → texto
    texto = re.sub(r'\*([^*\n]+?)\*', r'\1', texto)         # *cursiva* → texto (no cruza líneas)
    texto = re.sub(r'#{1,6}\s*', '', texto)                  # ### → ''
    texto = re.sub(r'^-{3,}\s*$', '', texto, flags=re.MULTILINE)   # ---
    texto = re.sub(r'^\*{1,2}\s*$', '', texto, flags=re.MULTILINE)  # líneas solo **
    # Asteriscos sobrantes (markdown desbalanceado como "**Título*"): ya
    # normalizamos las viñetas arriba, así que un "*" restante es residuo.
    texto = texto.replace('*', '')
    # 3) Símbolos residuales sueltos (no forman parte del contenido).
    texto = re.sub(r'[´`]', '', texto)                       # acento/backtick suelto
    texto = re.sub(r'^[=~]{2,}\s*$', '', texto, flags=re.MULTILINE)  # ===, ~~~ decorativos
    texto = re.sub(r'[ \t]{2,}', ' ', texto)                 # espacios dobles que dejan las remociones
    texto = re.sub(r'\n{3,}', '\n\n', texto)                 # max 2 saltos consecutivos
    return texto.strip()


def _limpiar_nombre_recurrente(nombre: str) -> str:
    """El nombre del recurrente (sección 7 CNJ) a veces arrastra el separador
    "---" o el título de la sección siguiente tras una línea en blanco
    ("Nombre\\n\\n---", "Nombre\\n\\n**Causal o causales...**"). Se toma lo
    previo al primer salto doble y se limpia markdown/separadores, para que el
    nombre quede en una sola línea limpia."""
    if not nombre:
        return ""
    base = nombre.split("\n\n")[0]        # corta antes del separador / título siguiente
    base = _limpiar_md(base)              # quita '---', '**', viñetas, residuos
    base = base.replace("\n", " ")        # nombre en una sola línea
    base = _re.sub(r'\s{2,}', ' ', base)
    return base.strip(" -–—•\t").strip()


def _parsear_secciones(texto: str) -> dict:
    """
    Parsea las secciones numeradas del texto de extracción de Qwen.
    Retorna {num_seccion: contenido_str}.
    Funciona con headers como "16. Decisión Final:" o "### 14. Argumentos..."
    También soporta "**16. Título**" (asteriscos alrededor del número).
    """
    if not texto:
        return {}
    partes = _re.split(r'\n[ \t]*\**(?:#{1,3}\s*)?\**(\d+)\.[ \t]+', texto)
    resultado = {}
    for i in range(1, len(partes) - 1, 2):
        try:
            num = int(partes[i])
            raw = partes[i + 1] if i + 1 < len(partes) else ""
            # La primera línea de raw es el título de la sección → omitir
            nl = raw.find('\n')
            contenido = raw[nl + 1:].strip() if nl != -1 else raw.strip()
            if contenido:
                resultado[num] = contenido
        except (ValueError, IndexError):
            pass
    return resultado


# Títulos de sección → número, para cuando Qwen omite la numeración (blob CNJ, prompt 5)
_TITULOS_CNJ = [
    (2,  _re.compile(r'jueces?\s+que\s+intervinieron', _re.IGNORECASE)),
    (3,  _re.compile(r'fecha\s+de\s+emisi[oó]n', _re.IGNORECASE)),
    # 4 y 6 aceptan el título tributario y los de apelación/casación de otras
    # materias (Sala de la Corte Provincial / Juzgado que emitió; decisión de
    # las instancias anteriores / de primera instancia).
    (4,  _re.compile(r'(?:tribunal\s+contencioso|sala\s+de\s+la\s+corte|juzgado)[^\n:]*que\s+emiti', _re.IGNORECASE)),
    (5,  _re.compile(r'antecedentes\s+del\s+caso', _re.IGNORECASE)),
    (6,  _re.compile(r'decisi[oó]n\s+(?:del\s+tribunal\s+distrital|de\s+las?\s+instancias?|de\s+primera\s+instancia)', _re.IGNORECASE)),
    (7,  _re.compile(r'recurrente', _re.IGNORECASE)),
    (8,  _re.compile(r'causal(?:es)?\s*(?:o\s+causales)?\s*(?:invocadas?)?', _re.IGNORECASE)),
    (9,  _re.compile(r'argumentos?\s+del\s+recurrente', _re.IGNORECASE)),
    (10, _re.compile(r'argumentos?\s+de\s+la\s+contraparte', _re.IGNORECASE)),
    (11, _re.compile(r'decisi[oó]n\s+final', _re.IGNORECASE)),
    (12, _re.compile(r'medidas?\s+adicionales?', _re.IGNORECASE)),
    (13, _re.compile(r'voto\s+salvado', _re.IGNORECASE)),
    (14, _re.compile(r'argumento\s+del\s+(?:juez|voto)', _re.IGNORECASE)),
]

# Ídem, para el blob de extracción del TRIBUNAL (prompt 2, 19 secciones).
# Se omite la 1 (Instancia) — texto fijo, nunca se vio con este problema.
_TITULOS_TRIBUNAL = [
    (2,  _re.compile(r'(?:tribunal|juzgado|unidad\s+judicial)\s+que\s+emit', _re.IGNORECASE)),
    (3,  _re.compile(r'jueces?\s+que\s+intervinieron', _re.IGNORECASE)),
    (4,  _re.compile(r'fecha\s+de\s+emisi[oó]n', _re.IGNORECASE)),
    (5,  _re.compile(r'partes?\s+procesales?', _re.IGNORECASE)),
    (6,  _re.compile(r'tipo\s+de\s+acci[oó]n', _re.IGNORECASE)),
    (7,  _re.compile(r'cuant[ií]a\s+del\s+caso', _re.IGNORECASE)),
    (8,  _re.compile(r'detalle\s+de\s+la\s+cuant[ií]a', _re.IGNORECASE)),
    # Sección 9/10: tributario = acto impugnado / otros actos administrativos;
    # laboral y civil = pretensiones de la parte actora / excepciones de la demandada.
    (9,  _re.compile(r'acto\s+impugnado|pretensiones', _re.IGNORECASE)),
    (10, _re.compile(r'otros\s+actos\s+administrativos|excepciones', _re.IGNORECASE)),
    (11, _re.compile(r'objeto\s+de\s+la\s+controversia', _re.IGNORECASE)),
    (12, _re.compile(r'antecedentes', _re.IGNORECASE)),
    (13, _re.compile(r'pruebas\s+presentadas', _re.IGNORECASE)),
    (14, _re.compile(r'argumentos?\s+de\s+la\s+parte\s+actora', _re.IGNORECASE)),
    # El modelo a veces usa "Contraparte" (terminología de CNJ) en vez de
    # "Parte Demandada" (la que pide el prompt 2) — se aceptan ambas.
    (15, _re.compile(r'argumentos?\s+de\s+la\s+(?:parte\s+demandada|contraparte)', _re.IGNORECASE)),
    (16, _re.compile(r'decisi[oó]n\s+final', _re.IGNORECASE)),
    (17, _re.compile(r'disposiciones?\s+adicionales?', _re.IGNORECASE)),
    (18, _re.compile(r'voto\s+salvado', _re.IGNORECASE)),
    (19, _re.compile(r'argumento\s+del\s+juez\s+sobre\s+el\s+voto\s+salvado', _re.IGNORECASE)),
]


_PATRON_TITULO_CNJ = r'(?:^|\n)[ \t]*[\*#]*\s*(?:\d+[\.\)]\s*)?{}[^\n]{{0,120}}:[^\n]*\n?'


def _recortar_secciones_embebidas(secs: dict, titulos: list = _TITULOS_CNJ) -> dict:
    """
    A veces el modelo omite el número de UNA sección intermedia (ej. escribe
    "Causal o causales invocadas:" en vez de "8. Causal..."). El parser
    numerado no detecta ahí un corte, y ese contenido queda atrapado al
    final de la sección anterior (que sí tenía número) — se pierde
    silenciosamente en vez de figurar bajo su propio número.

    A diferencia del fallback de _parsear_secciones_cnj (todo o nada: solo
    corre si el parser numerado no encontró NADA), esto corre siempre,
    encima de lo que haya logrado el parser numerado, buscando dentro de
    cada sección ya extraída si el título de una sección POSTERIOR quedó
    embebido, y recortando ahí. Itera hasta estabilizar por si más de una
    sección consecutiva quedó sin número (encadenadas).

    `titulos` es la lista de (num, patrón) a usar — _TITULOS_CNJ (14
    secciones, prompt 5) o _TITULOS_TRIBUNAL (19 secciones, prompt 2).
    """
    cambio = True
    intentos = 0
    while cambio and intentos < 20:
        cambio = False
        intentos += 1
        for num in list(secs.keys()):
            contenido = secs.get(num, "")
            if not contenido:
                continue
            mejor = None  # (posición, num_siguiente, match)
            for num_sig, pat in titulos:
                if num_sig <= num:
                    continue
                m = _re.search(_PATRON_TITULO_CNJ.format(pat.pattern), contenido, _re.IGNORECASE)
                if m and (mejor is None or m.start() < mejor[0]):
                    mejor = (m.start(), num_sig, m)
            if mejor:
                _, num_sig, m = mejor
                cola = contenido[m.end():].strip()
                secs[num] = contenido[:m.start()].strip()
                if cola and not (secs.get(num_sig) or "").strip():
                    secs[num_sig] = cola
                    cambio = True
    return secs


def _parsear_secciones_cnj(texto: str) -> dict:
    """
    Parsea el blob de extracción CNJ.
    Prueba el formato numerado primero; si falla, extrae por títulos literales del prompt 5.
    """
    resultado = _parsear_secciones(texto)
    if resultado:
        return _recortar_secciones_embebidas(resultado)

    # Fallback: buscar posición de cada título conocido y extraer el contenido
    # ENTRE el fin de un título y el INICIO del siguiente (no su fin), para no
    # arrastrar el título de la sección siguiente dentro del contenido.
    posiciones = []
    for num, pat in _TITULOS_CNJ:
        # Buscar el título como encabezado de línea (opcionalmente precedido de asteriscos/# y número)
        m = _re.search(
            _PATRON_TITULO_CNJ.format(pat.pattern),
            texto, _re.IGNORECASE
        )
        if m:
            posiciones.append((m.start(), m.end(), num))

    posiciones.sort()
    resultado = {}
    for idx, (_ini_tit, fin_tit, num) in enumerate(posiciones):
        fin = posiciones[idx + 1][0] if idx + 1 < len(posiciones) else len(texto)  # INICIO del siguiente título
        contenido = texto[fin_tit:fin].strip()
        if contenido:
            resultado[num] = contenido
    return resultado


def _clasificar_decision_casacion(texto: str) -> str:
    """
    Normaliza la sección 11 (Decisión Final CNJ) a uno de los 3 términos del
    catálogo decisiones_finales_casacion ("Se casó" / "No se casó" / "Se
    casó parcialmente").

    OJO: "se acepta el recurso... y se declara nulo el fallo recurrido"
    (nulidad + remisión al tribunal de origen para que rehaga el proceso)
    NO cuenta como "Se casó" — a diferencia de una casación propiamente
    dicha, la CNJ no resuelve el fondo, solo devuelve el proceso por un
    defecto procesal. Antes esta función SÍ lo clasificaba como "Se casó"
    (razonamiento equivocado, corregido tras revisar un caso real donde
    el resultado de SATJE — la fuente autorizada — decía lo contrario).
    Por eso "acepta el recurso"/"declara nulo"/"declara la nulidad" NO
    están en la lista de disparadores de "Se casó": son compatibles con
    varios desenlaces distintos, no alcanzan solos como evidencia.
    """
    t = _normalizar(texto)
    if not t:
        return ""
    # Negativo primero — "no casa"/"no acepta" contienen la palabra positiva.
    if any(p in t for p in [
        "no casa", "no se casa", "no acepta", "no se acepta",
        "rechaza el recurso", "improcedente", "inadmite el recurso",
    ]):
        return "No se casó"
    if any(p in t for p in ["parcialmente", "en parte"]):
        return "Se casó parcialmente"
    if any(p in t for p in ["casa la sentencia", "casa el fallo", "con lugar el recurso"]):
        return "Se casó"
    return ""


def _split_jurisprudencia(blob: str) -> tuple:
    """
    Divide el blob de jurisprudencia en (jurisprudencia_citada, criterio_sala).
    El blob tiene estructura:
      [encabezado]
      1. Jurisprudencia mencionada...: [citas]
      2. Criterio del tribunal...: [análisis]

    El modelo a veces antepone el título del prompt completo (ej. "EXTRACCIÓN
    DE JURISPRUDENCIA Y CRITERIOS DE LOS JUECES DE LA CORTE NACIONAL DE
    JUSTICIA") antes de "1. Jurisprudencia...". Por eso se busca esa sección
    SIN anclar al inicio del string — todo lo que la precede se descarta,
    sea cual sea el preámbulo que haya escrito el modelo.
    """
    if not blob:
        return "", ""
    # Sin ancla a inicio de línea (frágil: depende de que el modelo haya
    # puesto exactamente un \n antes) y tolerando markdown tanto ANTES como
    # DESPUÉS del número (ej. "**1.** Jurisprudencia...", "1.**Jurisprudencia**")
    # — antes solo se toleraban asteriscos antes del número, y un "1.**Jurisprudencia"
    # sin espacio tras el punto no matcheaba, dejando el título y el "1." pegados
    # al contenido en vez de recortarse.
    m1 = _re.search(
        r'[ \t#\*]*1\.[ \t\*]*Jurisprudencia[^\n]*\n?',
        blob, _re.IGNORECASE
    )
    resto = blob[m1.end():] if m1 else blob
    # Localizar donde empieza la sección 2 (Criterio) dentro de lo que queda.
    # El patrón maneja variantes como: "2.", "### *2.", "**2.**", "2.**Criterio**", etc.
    m2 = _re.search(
        r'[ \t#\*]*2\.[ \t\*]*Criterio[^\n]*\n?',
        resto, _re.IGNORECASE
    )
    if m2:
        parte1 = resto[:m2.start()].strip()
        parte2 = resto[m2.end():].strip()
        return _canonicalizar_no_consta(_limpiar_md(parte1)), _canonicalizar_no_consta(_limpiar_md(parte2))
    return _canonicalizar_no_consta(_limpiar_md(resto)), ""


def _canonicalizar_no_consta(texto: str) -> str:
    """
    El prompt pide responder ÚNICAMENTE "No consta en la sentencia." cuando
    no hay información — pero el modelo a veces igual agrega una explicación
    de por qué no consta (ej. "No consta en la sentencia. (A pesar de que se
    analizan normas...)"). Esa aclaración no fue pedida y no debería quedar
    en el campo: si el texto empieza así, se corta ahí.
    """
    t = (texto or "").strip()
    if t.lower().startswith("no consta en la sentencia"):
        return "No consta en la sentencia."
    return t


def _extraer_administracion_catalogo(clasificacion: str) -> Optional[str]:
    """
    Saca 'SRI'/'SENAE'/'GAD' de la línea 'Catálogo Aplicable:' del prompt 4.
    Varias categorías (Recurso de revisión, Acción coactiva, Reclamos
    administrativos, etc.) existen en Strapi una vez por cada administración
    ("Recurso de revisión SRI", "... SENAE", "... GAD") pero el catálogo que
    lee la IA las lista sin ese sufijo — sin esto, el lookup por substring
    encuentra las 3 variantes y se queda con la primera que devuelva Strapi,
    sin relación con la administración real de la causa.
    """
    import re
    palabras = list(MATERIA.ADMINISTRACIONES.keys())
    if MATERIA.PALABRA_CATALOGO:
        palabras.append(MATERIA.PALABRA_CATALOGO)
    if not palabras:
        return None
    m = re.search(r'(?i)cat[aá]logo\s+aplicable\s*:?\s*\n?\s*(\**)\s*(' + "|".join(map(re.escape, palabras)) + r')\b', clasificacion)
    return m.group(2).upper() if m else None


def _parsear_nombres_categorias(clasificacion: str) -> list:
    """
    Extrae nombres de categorías y subcategorías del blob de clasificación.
    Devuelve lista de strings de nombres para buscar en Strapi.
    """
    import re
    nombres = []
    lines = clasificacion.splitlines()
    def _valido(nombre):
        # "No aplica (…)" y frases largas NO son nombres de categoría: son la
        # justificación que escribe el modelo. El tope cubre el nombre más largo
        # del catálogo (antes 70 descartaba subcategorías reales).
        return (nombre and not nombre.lower().startswith(("no aplica", "n/a", "no consta"))
                and len(nombre) <= 130)

    for i, line in enumerate(lines):
        limpia = re.sub(r'[\*#_•○●]', '', line).strip().rstrip(':').strip()
        # Nombre en la misma línea: "Categoría 7: Contratación pública" /
        # "Subcategoría: Reajuste de precios" (así responde Qwen a menudo).
        en_linea = re.match(r'(?i)(?:sub)?categor[ií]a\s*\d*\s*:\s*(.+)$', limpia)
        if en_linea:
            nombre = re.sub(r'^\d+[\.\)]\s*', '', en_linea.group(1)).strip()
            if _valido(nombre):
                nombres.append(nombre)
            continue
        # Detectar línea "Categoría N:" o "Subcategoría:"
        if re.match(r'(?i)(?:sub)?categor[ií]a\s*\d*\s*$', limpia):
            # El nombre está en la siguiente línea no vacía
            for j in range(i + 1, min(i + 4, len(lines))):
                next_limpia = re.sub(r'[\*#_•○●]', '', lines[j]).strip()
                if not next_limpia:
                    continue
                # Saltar si es otra cabecera de sección
                if re.match(r'(?i)(?:sub)?categor[ií]a|problemát|catálogo', next_limpia):
                    break
                # Quitar prefijos tipo "5. " o "21. "
                nombre = re.sub(r'^\d+[\.\)]\s*', '', next_limpia).strip()
                if _valido(nombre):
                    nombres.append(nombre)
                break
    return nombres


def _es_procuraduria(texto: str) -> bool:
    return "procurad" in _normalizar(texto)


def _primer_abogado(texto: str) -> str:
    """De "A (representante del SECOB); B (delegado del Procurador…)" devuelve "A":
    un solo nombre, sin la acotación, y el de la entidad antes que el de la
    Procuraduría. Antes se creaba un abogado con toda la línea como nombre."""
    partes = [p.strip() for p in (texto or "").split(";") if p.strip()]
    if not partes:
        return texto or ""
    elegido = next((p for p in partes if not _es_procuraduria(p)), partes[0])
    return _re.sub(r"\s*\([^)]*\)", "", elegido).strip()


_RE_REPRESENTANTE = _re.compile(
    r'(?i)procurad|representante|apoderad|gerente|en calidad|delegad|director|alcalde|prefect|s[ií]ndic')


def _actor_de_partes(partes: str) -> str:
    """Primer actor que la IA lista en la sección 5 ("Actor (Demandante): A; B")."""
    m = _re.search(r'(?im)^[\s•\-\*]*actor(?:es)?\s*(?:\([^)]*\))?\s*:\s*(.+)$', partes or "")
    if not m:
        return ""
    primero = m.group(1).split(";")[0].strip(" .")
    return "" if "no consta" in _normalizar(primero) else primero


def _entidad_demandada(partes: str, demandado_satje: str) -> str:
    """Entidad demandada para enlazar en Strapi.

    SATJE suele poner primero a la Procuraduría General del Estado, que
    interviene por ley en todas las causas contra el Estado; la entidad real
    es la que la IA lista en la sección 5 ("Demandado (Entidad Pública): A; B").
    Se toma la primera que no sea la Procuraduría. Si no hay otra, queda la
    Procuraduría (fue demandada como tal)."""
    m = _re.search(r'(?im)^[\s•\-\*]*demandad[oa]s?\s*(?:\([^)]*\))?\s*:\s*(.+)$', partes or "")
    candidatas = [c.strip(" .") for c in _re.split(r';', m.group(1))] if m else []
    candidatas = [c for c in candidatas if c and "no consta" not in _normalizar(c)]
    for c in candidatas:
        if not _es_procuraduria(c):
            return c
    if demandado_satje and not _es_procuraduria(demandado_satje):
        return demandado_satje
    return candidatas[0] if candidatas else demandado_satje


def _split_pruebas(contenido: str) -> tuple:
    """Divide la sección 13 en (pruebas_actor, pruebas_demandado)."""
    m_actor = _re.search(r'(?:Por el Actor|Actor)\s*:', contenido, _re.IGNORECASE)
    m_dem   = _re.search(r'(?:Por el Demandado|Demandado)\s*:', contenido, _re.IGNORECASE)
    if m_actor and m_dem and m_actor.start() < m_dem.start():
        actor = contenido[m_actor.end():m_dem.start()].strip()
        dem   = contenido[m_dem.end():].strip()
        return actor, dem
    return contenido, ""


# Caches por proceso (se llenan en la primera consulta)
_CACHE_DECISION_FINAL: list = []
_CACHE_DECISION_CASACION: list = []
_CACHE_DECISION_APELACION: list = []
_CACHE_TIPO_ACCION: list = []
_CACHE_ESTADO_JUICIO: list = []
_CACHE_CAUSALES: list = []
_CACHE_ENTIDADES: list = []
_CACHE_JUECES: list = []
# Catálogo de actores para reusar la escritura canónica al insertar (#12).
# clave_fuerte -> nombre canónico (la variante más usada ya existente).
import threading as _threading
_CACHE_ACTORES_CANON: dict = {}
_CACHE_ACTORES_LISTO: bool = False
_LOCK_ACTORES = _threading.Lock()

STRAPI_URL   = os.environ.get("STRAPI_URL", "http://localhost:1337")
STRAPI_TOKEN = os.environ.get("STRAPI_API_TOKEN", "")

_TIMEOUT = 30


class StrapiClient:

    def __init__(self):
        self._headers = {
            "Authorization": f"Bearer {STRAPI_TOKEN}",
            "Content-Type":  "application/json",
        }

    def guardar_causa(self, fila: dict) -> Optional[dict]:
        """
        Crea o actualiza una causa en Strapi.
        `fila` es el dict que devuelve procesar_causa() del extractor.
        """
        numero = fila.get("Número de juicio", "")
        if not numero:
            return None

        payload = self._mapear_causa(fila)

        # Buscar si ya existe
        existente = self._buscar_causa(numero)
        if existente:
            doc_id = existente["documentId"]
            return self._put(f"/api/causas/{doc_id}", payload)
        else:
            return self._post("/api/causas", payload)

    def _cargar_catalogo_actores(self) -> None:
        """Carga una sola vez por proceso el mapa clave_fuerte -> nombre
        canónico a partir de los actor_normalizado ya existentes en la base."""
        global _CACHE_ACTORES_LISTO
        with _LOCK_ACTORES:
            if _CACHE_ACTORES_LISTO:
                return
            from collections import defaultdict
            conteo: dict = defaultdict(lambda: defaultdict(int))
            page = 1
            try:
                while True:
                    resp = httpx.get(
                        f"{STRAPI_URL}/api/causas",
                        params={
                            "fields[0]": "actor_normalizado",
                            "pagination[page]": page,
                            "pagination[pageSize]": 500,
                        },
                        headers=self._headers, timeout=_TIMEOUT,
                    )
                    body = resp.json()
                    datos = body.get("data", []) or []
                    for d in datos:
                        nom = (d.get("actor_normalizado") or "").strip()
                        if nom:
                            conteo[_clave_actor(nom)][nom] += 1
                    pag = body.get("meta", {}).get("pagination", {})
                    if not datos or page >= pag.get("pageCount", 0):
                        break
                    page += 1
                for clave, variantes in conteo.items():
                    if not clave:
                        continue
                    # Canónico = la variante más usada; en empate, la que
                    # conserva tildes (más correcta) y luego la más larga (razón
                    # social más completa).
                    _CACHE_ACTORES_CANON[clave] = max(
                        variantes.items(),
                        key=lambda kv: (kv[1], _n_tildes(kv[0]), len(kv[0])),
                    )[0]
            except Exception as e:
                print(f"  [WARN cargar_catalogo_actores] {e}")
            _CACHE_ACTORES_LISTO = True

    def _canonizar_actor(self, nombre: Optional[str]) -> Optional[str]:
        """Reutiliza la escritura canónica ya existente para el mismo actor, en
        vez de crear una variante nueva por cada diferencia de escritura (#12).
        Si el actor no existe aún, registra esta escritura como canónica para
        que las siguientes causas del mismo proceso la reutilicen."""
        if not nombre or not nombre.strip():
            return nombre
        self._cargar_catalogo_actores()
        clave = _clave_actor(nombre)
        if not clave:
            return nombre
        existente = _CACHE_ACTORES_CANON.get(clave)
        if existente:
            return existente
        _CACHE_ACTORES_CANON[clave] = nombre
        return nombre

    def _lookup_decision_final(self, texto: str) -> Optional[str]:
        """Devuelve documentId del decision-final (Con lugar / Sin lugar /
        Parcialmente con lugar) que corresponde a la decisión del AI o al texto."""
        global _CACHE_DECISION_FINAL
        if not texto or "no consta" in texto.lower():
            return None
        objetivo = _clasificar_decision_tribunal(texto)
        if not objetivo:
            return None
        if not _CACHE_DECISION_FINAL:
            try:
                resp = httpx.get(f"{STRAPI_URL}/api/decision-finals",
                                 headers=self._headers, timeout=_TIMEOUT)
                _CACHE_DECISION_FINAL = resp.json().get("data", [])
            except Exception as e:
                print(f"  [WARN lookup decision_final] {e}")
                return None
        # Match por igualdad normalizada contra el catálogo (el objetivo ya es
        # uno de los 3 términos canónicos, así que no cae en la trampa de
        # substring de _mejor_coincidencia).
        for e in _CACHE_DECISION_FINAL:
            if _normalizar((e.get("attributes", e)).get("nombre", "")) == objetivo:
                return e.get("documentId") or str(e.get("id", ""))
        return None

    def _lookup_decision_final_apelacion(self, texto: str) -> Optional[str]:
        """Devuelve documentId del decision-final-apelacion que coincide con el texto."""
        global _CACHE_DECISION_APELACION
        if not texto or "no consta" in texto.lower():
            return None
        if not _CACHE_DECISION_APELACION:
            try:
                resp = httpx.get(f"{STRAPI_URL}/api/decisiones-finales-apelacion",
                                 headers=self._headers, timeout=_TIMEOUT)
                _CACHE_DECISION_APELACION = resp.json().get("data", [])
            except Exception as e:
                print(f"  [WARN lookup decision_final_apelacion] {e}")
                return None
        texto_n = _normalizar(texto)
        match = _mejor_coincidencia(texto_n, _CACHE_DECISION_APELACION)
        return (match.get("documentId") or str(match.get("id", ""))) if match else None

    def _lookup_decision_final_casacion(self, texto: str) -> Optional[str]:
        """Devuelve documentId del decision-final-casacion que coincide con el texto."""
        global _CACHE_DECISION_CASACION
        if not texto or "no consta" in texto.lower():
            return None
        if not _CACHE_DECISION_CASACION:
            try:
                resp = httpx.get(f"{STRAPI_URL}/api/decisiones-finales-casacion",
                                 headers=self._headers, timeout=_TIMEOUT)
                _CACHE_DECISION_CASACION = resp.json().get("data", [])
            except Exception as e:
                print(f"  [WARN lookup decision_final_casacion] {e}")
                return None
        texto_n = _normalizar(texto)
        # _clasificar_decision_casacion devuelve la etiqueta de tributario
        # ("Se casó"…); en las demás materias el catálogo usa las frases de
        # MATERIA.DECISIONES_CASACION ("Casa la sentencia."…), en el mismo orden.
        canon = dict(zip(("se caso", "no se caso", "se caso parcialmente"),
                         MATERIA.DECISIONES_CASACION))
        match = _mejor_coincidencia(texto_n, _CACHE_DECISION_CASACION)
        if texto_n in canon and (not match or _normalizar(match.get("nombre", "")) != texto_n):
            match = _mejor_coincidencia(_normalizar(canon[texto_n]), _CACHE_DECISION_CASACION) or match
        return (match.get("documentId") or str(match.get("id", ""))) if match else None

    # ── Lookups relacionales ──────────────────────────────────────────────────

    @staticmethod
    def _parsear_jueces_seccion(contenido: str) -> tuple:
        """
        Parsea la sección de jueces (sección 3 tribunal, sección 2 CNJ).
        Retorna (nombre_ponente, [nombres_todos_jueces]).
        """
        ponente = ""
        todos: list = []
        for line in contenido.splitlines():
            line = line.strip().lstrip("●•-* \t")
            if not line:
                continue
            n_line = _normalizar(line)
            if "no consta" in n_line or n_line in ("ninguno", ""):
                continue
            if _re.search(r'(?:ponente|juez[a]?\s+de\s+la\s+causa)\s*(?:\([^)]*\))?\s*:', line, _re.IGNORECASE):
                nombre = _re.sub(r'.*?(?:ponente|juez[a]?\s+de\s+la\s+causa)\s*(?:\([^)]*\))?\s*:\s*', '', line, flags=_re.IGNORECASE).strip()
                nombre = _re.sub(r'[\*#_]', '', nombre).strip().rstrip('.')
                if nombre and "no consta" not in _normalizar(nombre):
                    ponente = nombre
                    todos.append(nombre)
            # "Otros Jueces (máximo dos):" — Qwen a veces copia la acotación del prompt.
            elif _re.search(r'otros?\s*jueces?\s*(?:\([^)]*\))?\s*:', line, _re.IGNORECASE):
                parte = _re.sub(r'.*?jueces?\s*(?:\([^)]*\))?\s*:\s*', '', line, flags=_re.IGNORECASE).strip()
                parte = _re.sub(r'[\*#_]', '', parte).strip()
                if parte and "no consta" not in _normalizar(parte):
                    for n in _RE_SEP_JUECES.split(parte):
                        n = (n or "").strip().rstrip('.').strip()
                        if n and "no consta" not in _normalizar(n):
                            todos.append(n)
        return ponente, todos

    def _lookup_or_create_juez(self, nombre: str) -> Optional[str]:
        """
        Busca juez por nombre (por conjunto de palabras, no substring
        literal — ver _tokens_nombre); lo crea si no existe. Auto-corrige
        nombres con markdown. Cachea todos los jueces (son ~80, no vale la
        pena paginar) para poder comparar variantes de orden/honorífico
        contra el catálogo completo en vez de un $containsi que solo
        encuentra coincidencias literales.
        """
        if not nombre:
            return None
        nombre_limpio = _re.sub(r'[\*#_]', '', nombre).strip().strip('.')
        nombre_limpio = _sin_honorifico(nombre_limpio)          # sin "Ab."/"Dr."
        nombre_limpio = normalizar_case_nombre(nombre_limpio)  # #3: no todo mayúsculas
        n = _normalizar(nombre_limpio)
        # "No hubo voto salvado." / "No aplica (juez unipersonal)." los escribe
        # resolucion/ai/controles.py: no son nombres de juez.
        if "no consta" in n or n in ("ninguno", "") or n.startswith(("no hubo", "no aplica")):
            return None
        tokens = _tokens_nombre(nombre_limpio)
        if not tokens:
            return None

        global _CACHE_JUECES
        if not _CACHE_JUECES:
            try:
                resp = httpx.get(
                    f"{STRAPI_URL}/api/jueces",
                    params={"pagination[pageSize]": 500},
                    headers=self._headers, timeout=_TIMEOUT,
                )
                _CACHE_JUECES = resp.json().get("data", [])
            except Exception as e:
                print(f"  [WARN juez cache] {e}")
                _CACHE_JUECES = []

        mejor = None
        for e in _CACHE_JUECES:
            attrs = e.get("attributes", e)
            stored = attrs.get("nombre", "")
            # No enganchar a registros concatenados (2+ jueces en un nombre): son
            # basura de extracciones viejas y cualquier juez individual "cabe"
            # dentro de ellos por subconjunto de palabras.
            if _es_multi_persona(stored):
                continue
            if _misma_persona(tokens, _tokens_nombre(stored)):
                # Preferir la variante encontrada con MÁS palabras (nombre
                # más completo) si hay más de un match posible.
                if mejor is None or len(_tokens_nombre(stored)) > len(_tokens_nombre((mejor.get("attributes", mejor)).get("nombre", ""))):
                    mejor = e
        if mejor:
            doc_id = mejor.get("documentId") or str(mejor.get("id", ""))
            stored = (mejor.get("attributes", mejor)).get("nombre", "")
            # Guardar la variante más completa/limpia: más palabras, o el record
            # existente traía markdown u honorífico ("Ab. …", "Dr. …").
            if (len(tokens) > len(_tokens_nombre(stored)) or "*" in stored
                    or "#" in stored or _RE_HONORIFICO.match(stored)):
                try:
                    self._put(f"/api/jueces/{doc_id}", {"data": {"nombre": nombre_limpio[:255]}})
                    print(f"  [FIX juez] Nombre actualizado: {stored!r} → {nombre_limpio!r}")
                except Exception:
                    pass
            return doc_id
        try:
            result = self._post("/api/jueces", {"data": {"nombre": nombre_limpio[:255]}})
            doc_id = (result.get("data") or {}).get("documentId") if result else None
            if doc_id:
                _CACHE_JUECES.append({"documentId": doc_id, "attributes": {"nombre": nombre_limpio[:255]}})
            return doc_id
        except Exception as e:
            print(f"  [WARN juez lookup/create '{nombre[:40]}'] {e}")
            return None

    def _lookup_or_create_abogado(self, nombre: str) -> Optional[str]:
        """Busca abogado por nombre limpio; lo crea si no existe. Mismo patrón que juez."""
        if not nombre:
            return None
        from config.pdf_utils import limpiar_nombre_abogado
        nombre = _primer_abogado(nombre)
        nombre_limpio = limpiar_nombre_abogado(nombre)  # honoríficos/cargos → '' (#4)
        if not nombre_limpio:
            return None
        nombre_limpio = normalizar_case_nombre(nombre_limpio)  # #3: no todo mayúsculas
        n = _normalizar(nombre_limpio)
        if "no consta" in n or n in ("ninguno", ""):
            return None
        try:
            resp = httpx.get(
                f"{STRAPI_URL}/api/abogados",
                params={"filters[nombre][$containsi]": nombre_limpio[:60]},
                headers=self._headers, timeout=_TIMEOUT,
            )
            data = resp.json().get("data", [])
            # Exacto primero: $containsi también trae registros viejos que tenían
            # toda la línea ("Nombre (cargo); Otro (cargo)") como nombre.
            data.sort(key=lambda d: _normalizar((d.get("attributes", d)).get("nombre", "")) != n)
            if data:
                doc_id = data[0].get("documentId") or str(data[0].get("id", ""))
                stored = (data[0].get("attributes", data[0])).get("nombre", "")
                # Corregir el registro si venía con honorífico/markdown ("Ab. …")
                # o con la línea completa de varios abogados.
                if stored != nombre_limpio and (_RE_HONORIFICO.match(stored) or "*" in stored or "#" in stored
                                                or ";" in stored or "(" in stored):
                    try:
                        self._put(f"/api/abogados/{doc_id}", {"data": {"nombre": nombre_limpio[:255]}})
                        print(f"  [FIX abogado] {stored!r} → {nombre_limpio!r}")
                    except Exception:
                        pass
                return doc_id
            result = self._post("/api/abogados", {"data": {"nombre": nombre_limpio[:255]}})
            return (result.get("data") or {}).get("documentId") if result else None
        except Exception as e:
            print(f"  [WARN abogado lookup/create '{nombre[:40]}'] {e}")
            return None

    def _lookup_or_create_entidad(self, nombre: str, adm_hint: Optional[str] = None) -> Optional[str]:
        """
        Busca entidad demandada por nombre; la crea si no existe.

        El "Demandado" que trae SATJE suele ser el cargo + nombre del
        representante legal en turno (ej. "directora general del servicio
        de rentas internas- ec. marisol andrade hernandez"), no el nombre
        limpio de la entidad. Por eso el match NO puede ser
        $containsi(nombre_catalogo, texto_largo) — eso pregunta si el
        nombre corto del catálogo CONTIENE el texto largo, que nunca pasa,
        y termina creando una entidad duplicada por cada representante
        legal distinto que aparezca con el tiempo. Se compara en ambas
        direcciones contra el catálogo completo (como decision_final).
        """
        if not nombre:
            return None
        # "No consta" se chequea sobre el texto CRUDO (el canonizador siempre
        # devuelve una de las 3 entidades, incluso GAD por defecto).
        n_raw = _normalizar(nombre)
        if "no consta" in n_raw or n_raw in ("ninguno", ""):
            return None
        # Mapear el demandado a una de las 3: SRI, SENAE o GAD. Si el texto es
        # ambiguo, se usa la administración de la clasificación (adm_hint).
        nombre_final = _canonizar_entidad(nombre, adm_hint)
        n = _normalizar(nombre_final)
        global _CACHE_ENTIDADES
        if not _CACHE_ENTIDADES:
            try:
                # Todas las páginas: con más de 500 entidades se creaban duplicados.
                pagina, _CACHE_ENTIDADES = 1, []
                while True:
                    resp = httpx.get(
                        f"{STRAPI_URL}/api/entidades",
                        params={"pagination[pageSize]": 500, "pagination[page]": pagina},
                        headers=self._headers, timeout=_TIMEOUT,
                    )
                    lote = resp.json().get("data", [])
                    _CACHE_ENTIDADES += lote
                    if len(lote) < 500:
                        break
                    pagina += 1
            except Exception as e:
                print(f"  [WARN entidad cache] {e}")
                _CACHE_ENTIDADES = []
        # Primero una entidad del catálogo contenida en el texto (la más larga),
        # que gana incluso a un registro idéntico con cargo + representante;
        # luego el idéntico. Ya no al revés (texto contenido en el catálogo):
        # "Contraloría General del Estado" caía en "Directora Nacional de
        # Recursos de Revisión de la Contraloría…"; ahora se crea la limpia.
        contenidas, exacta = [], None
        for e in _CACHE_ENTIDADES:
            nombre_n = _normalizar((e.get("attributes", e)).get("nombre", ""))
            if nombre_n == n:
                exacta = e
            elif nombre_n and nombre_n in n:
                contenidas.append((len(nombre_n), e))
        elegida = max(contenidas, key=lambda x: x[0])[1] if contenidas else exacta
        if elegida:
            return elegida.get("documentId") or str(elegida.get("id", ""))
        try:
            result = self._post("/api/entidades", {"data": {"nombre": nombre_final[:255]}})
            doc_id = (result.get("data") or {}).get("documentId") if result else None
            if doc_id:
                _CACHE_ENTIDADES.append({"documentId": doc_id, "nombre": nombre_final[:255]})
            return doc_id
        except Exception as e:
            print(f"  [WARN entidad create '{nombre_final[:40]}'] {e}")
            return None

    def _lookup_tribunal(self, nombre: str) -> Optional[str]:
        """Busca tribunal por nombre. Retorna documentId o None."""
        if not nombre:
            return None
        try:
            # Nombre completo e igualdad primero: con un prefijo de 60 letras,
            # "…Contencioso Administrativo con sede en Quito" caía en Guayaquil
            # y Ambato en Portoviejo (mismos 60 primeros caracteres).
            data = []
            for filtro in ("$eqi", "$containsi"):
                resp = httpx.get(
                    f"{STRAPI_URL}/api/tribunales",
                    params={f"filters[nombre][{filtro}]": nombre.strip()},
                    headers=self._headers, timeout=_TIMEOUT,
                )
                data = resp.json().get("data", [])
                if data:
                    break
            if len(data) != 1:
                if len(data) > 1:
                    print(f"  [WARN tribunal lookup '{nombre[:60]}'] {len(data)} coincidencias — sin enlazar")
                return None
            return data[0].get("documentId") or str(data[0].get("id", ""))
        except Exception as e:
            print(f"  [WARN tribunal lookup '{nombre[:40]}'] {e}")
            return None

    def _lookup_tipo_accion(self, texto: str) -> Optional[str]:
        """Lookup fuzzy de tipo de acción. Retorna documentId o None."""
        global _CACHE_TIPO_ACCION
        if not texto or "no consta" in _normalizar(texto):
            return None
        if not _CACHE_TIPO_ACCION:
            try:
                resp = httpx.get(f"{STRAPI_URL}/api/tipo-de-accions",
                                 headers=self._headers, timeout=_TIMEOUT)
                _CACHE_TIPO_ACCION = resp.json().get("data", [])
            except Exception as e:
                print(f"  [WARN tipo_accion cache] {e}")
                return None
        texto_n = _normalizar(texto)
        match = _mejor_coincidencia(texto_n, _CACHE_TIPO_ACCION)
        return (match.get("documentId") or str(match.get("id", ""))) if match else None

    def _lookup_estado_juicio(self, texto: str) -> Optional[str]:
        """Lookup fuzzy de estado del juicio. Retorna documentId o None."""
        global _CACHE_ESTADO_JUICIO
        if not texto or "no consta" in _normalizar(texto):
            return None
        if not _CACHE_ESTADO_JUICIO:
            try:
                resp = httpx.get(f"{STRAPI_URL}/api/estado-del-juicios",
                                 headers=self._headers, timeout=_TIMEOUT)
                _CACHE_ESTADO_JUICIO = resp.json().get("data", [])
            except Exception as e:
                print(f"  [WARN estado_juicio cache] {e}")
                return None
        texto_n = _normalizar(texto)
        match = _mejor_coincidencia(texto_n, _CACHE_ESTADO_JUICIO)
        return (match.get("documentId") or str(match.get("id", ""))) if match else None

    def _lookup_or_create_categoria(self, nombre: str, administracion: Optional[str] = None) -> Optional[str]:
        """
        Busca una categoría del CATÁLOGO CURADO por nombre. NO crea categorías
        nuevas (igual que las causales): si el nombre no matchea el catálogo se
        OMITE — así no se ensucia el catálogo con texto del modelo ("No aplica…",
        justificaciones). Prioridad de match: exacto > exacto+sufijo de
        administración (SRI/SENAE/GAD) > único candidato. Si hay varios y ninguno
        exacto, se omite (ambiguo) en vez de adivinar mal.
        """
        if not nombre:
            return None
        n = nombre.strip()
        if not n or n.lower().startswith(("no aplica", "n/a", "no consta")):
            return None
        try:
            resp = httpx.get(
                f"{STRAPI_URL}/api/categorias",
                params={"filters[nombre][$containsi]": n, "pagination[pageSize]": 20},
                headers=self._headers, timeout=_TIMEOUT,
            )
            items = resp.json().get("data", [])
        except Exception as e:
            print(f"  [WARN categorias lookup] '{n}': {e}")
            return None
        if not items:
            print(f"  [DBG categoría] '{n}' sin match en catálogo — omitida (no se crea)")
            return None

        def nom(it):   return (it.get("attributes", it)).get("nombre", "").strip()
        def docid(it): return it.get("documentId") or str(it.get("id", ""))
        low = n.lower()
        adm = (administracion or "").strip().upper()

        for it in items:                                   # 1. exacto
            if nom(it).lower() == low:
                return docid(it)
        if adm:                                            # 2. exacto + sufijo admin
            for it in items:
                s = nom(it)
                if s.upper().endswith(" " + adm) and s[: -(len(adm) + 1)].strip().lower() == low:
                    return docid(it)
        if len(items) == 1:                                # 3. único candidato
            return docid(items[0])
        print(f"  [DBG categoría] '{n}' ambigua ({len(items)} matches, admin={adm or '-'}) — omitida")
        return None

    def _lookup_categorias(self, clasificacion: str) -> list:
        """
        Extrae nombres de categorías/subcategorías del texto de clasificación (prompt 4)
        y retorna lista de documentIds (buscando o creando en Strapi si no existen).
        """
        if not clasificacion:
            return []
        nombres = _parsear_nombres_categorias(clasificacion)
        administracion = _extraer_administracion_catalogo(clasificacion)
        ids = []
        for nombre in nombres:
            doc_id = self._lookup_or_create_categoria(nombre, administracion)
            if doc_id and doc_id not in ids:
                ids.append(doc_id)
        # Clasificación COMPLETA: si se asignó una subcategoría (hoja), incluir
        # también su categoría padre y ancestros — no dejar solo la hoja.
        return self._con_ancestros_categoria(ids)

    def _con_ancestros_categoria(self, ids: list) -> list:
        """Agrega a la lista los documentIds de las categorías PADRE (y ancestros)
        de las categorías dadas, para registrar la clasificación completa."""
        if not ids:
            return ids
        completos = list(ids)
        params = {
            "fields[0]": "nombre",
            "pagination[pageSize]": 100,
            "populate[categoria_padre][populate][categoria_padre]"
            "[populate][categoria_padre][fields][0]": "nombre",
        }
        for i, d in enumerate(ids):
            params[f"filters[documentId][$in][{i}]"] = d
        try:
            resp = httpx.get(f"{STRAPI_URL}/api/categorias", params=params,
                             headers=self._headers, timeout=_TIMEOUT)
            for it in resp.json().get("data", []):
                padre = (it.get("attributes", it) or {}).get("categoria_padre")
                while padre:
                    pid = padre.get("documentId") or str(padre.get("id", ""))
                    if pid and pid not in completos:
                        completos.append(pid)
                    padre = padre.get("categoria_padre")
        except Exception as e:
            print(f"  [WARN ancestros categoria] {e}")
        return completos

    def _lookup_causales(self, texto_causales: str) -> list:
        """
        Busca los documentIds de las causales de casación (Art. 268 COGEP)
        mencionadas en la sección 8 de la extracción CNJ. Catálogo cerrado
        (5 causales fijas, curadas a mano en Strapi) — a diferencia de
        categorías/abogados, acá NO se crean causales nuevas.
        """
        if not texto_causales:
            return []
        numeros = set(_re.findall(r'causal(?:es)?\s*(?:n[.º°]?\s*)?(\d)', texto_causales, _re.IGNORECASE))
        # Formato ordinal en prosa ("causal primera", "causal quinta") también se usa en la práctica.
        _ORDINALES = {"primera": "1", "segunda": "2", "tercera": "3", "cuarta": "4", "quinta": "5"}
        for palabra, num in _ORDINALES.items():
            if _re.search(rf'causal(?:es)?\s+{palabra}\b', texto_causales, _re.IGNORECASE):
                numeros.add(num)
        if not numeros:
            return []
        global _CACHE_CAUSALES
        if not _CACHE_CAUSALES:
            try:
                resp = httpx.get(
                    f"{STRAPI_URL}/api/causales-invocadas",
                    params={"pagination[pageSize]": 50},
                    headers=self._headers, timeout=_TIMEOUT,
                )
                _CACHE_CAUSALES = resp.json().get("data", [])
            except Exception as e:
                print(f"  [WARN lookup causales] {e}")
                return []
        ids = []
        for e in _CACHE_CAUSALES:
            attrs = e.get("attributes", e)
            nombre = (attrs.get("nombre") or "").strip()
            m = _re.match(r'(?i)causal\s*(\d)', nombre)
            if m and m.group(1) in numeros:
                doc_id = e.get("documentId") or str(e.get("id", ""))
                if doc_id not in ids:
                    ids.append(doc_id)
        return ids

    def _lookup_or_create_recurrente(self, nombre: str) -> Optional[str]:
        """Busca un recurrente por nombre (containsi). Si no existe, lo crea."""
        nombre = _limpiar_nombre_recurrente(nombre)
        if not nombre or "no consta" in nombre.lower():
            return None
        try:
            resp = httpx.get(
                f"{STRAPI_URL}/api/recurrentes",
                params={"filters[nombre][$containsi]": nombre, "pagination[pageSize]": 3},
                headers=self._headers, timeout=_TIMEOUT,
            )
            items = resp.json().get("data", [])
            if items:
                return items[0].get("documentId") or str(items[0].get("id", ""))
        except Exception as e:
            print(f"  [WARN recurrente lookup] '{nombre}': {e}")
            return None
        try:
            resp2 = httpx.post(
                f"{STRAPI_URL}/api/recurrentes",
                json={"data": {"nombre": nombre, "publishedAt": _now_iso()}},
                headers=self._headers, timeout=_TIMEOUT,
            )
            if resp2.is_success:
                return resp2.json().get("data", {}).get("documentId")
            print(f"  [WARN recurrente create] '{nombre}': HTTP {resp2.status_code}")
        except Exception as e:
            print(f"  [WARN recurrente create] '{nombre}': {e}")
        return None

    def _resolver_recurrente(self, texto_recurrente: str, actor: str, demandado: str) -> Optional[str]:
        """
        Sección 7 (Recurrente) suele decir "la parte actora"/"el SRI" en vez
        del nombre propio — se resuelve al nombre real ya conocido de la
        causa cuando el texto solo indica de qué lado viene, para no crear
        entradas ambiguas tipo "El actor" en el catálogo de recurrentes.
        """
        texto = (texto_recurrente or "").strip()
        if not texto or "no consta" in texto.lower():
            return None
        texto_n = _normalizar(texto)
        if "actor" in texto_n or "demandante" in texto_n:
            nombre = actor
        elif "demandad" in texto_n:
            nombre = demandado
        else:
            nombre = texto
        return self._lookup_or_create_recurrente(nombre.strip()) if nombre else None

    # ── Causa principal ───────────────────────────────────────────────────────

    def _mapear_causa(self, fila: dict) -> dict:
        """Mapea campos del extractor a campos de Strapi."""
        # Parsear secciones del blob de extracción (prompt 2, 19 secciones)
        # sec 2=tribunal, 3=jueces, 4=fecha, 5=partes, 6=tipo acción, 7=cuantía,
        # 8=detalle cuantía, 9=acto impugnado, 10=otros actos, 11=objeto controversia,
        # 12=antecedentes, 13=pruebas, 14=args actor, 15=args demandado,
        # 16=decisión final, 17=disposiciones adicionales, 18=voto salvado, 19=arg voto
        # _recortar_secciones_embebidas rescata secciones cuyo número el modelo
        # omitió (quedan pegadas al final de la sección anterior) — mismo
        # problema que en CNJ, ver _TITULOS_TRIBUNAL.
        secs = _recortar_secciones_embebidas(
            _parsear_secciones(fila.get("_extraccion", "")), _TITULOS_TRIBUNAL
        )

        def _no_consta(v: str) -> bool:
            s = (v or "").strip().lower()
            return not s or s.startswith("no consta")

        # Cuantía, su detalle y objeto de controversia: se leen directo de sus
        # secciones (7, 8, 11) del blob de extracción ya generado, en vez de
        # confiar en la llamada JSON aparte (_extraer_campos_qwen/_SCHEMA_CAMPOS)
        # que a veces devuelve vacío/"No consta" aunque el dato SÍ está en el
        # texto (esa llamada se mantiene solo como respaldo, y para
        # tipo_accion/decision_final que sí necesitan el valor normalizado
        # a uno de los enums del catálogo).
        cuantia_seccion = _limpiar_md(secs.get(7, ""))
        cuantia_raw = cuantia_seccion if not _no_consta(cuantia_seccion) else (fila.get("Cuantía", "") or "")
        cuantia_num = _parsear_cuantia(cuantia_raw)

        detalle_cuantia_seccion = _limpiar_md(secs.get(8, ""))
        detalle_de_cuantia = (
            detalle_cuantia_seccion if not _no_consta(detalle_cuantia_seccion)
            else (cuantia_raw if cuantia_raw else "")
        )

        objeto_seccion = _limpiar_md(secs.get(11, ""))
        objeto_controversia = (
            objeto_seccion if not _no_consta(objeto_seccion)
            else (fila.get("Objeto Controversia", "") or "")
        )

        pruebas_actor, pruebas_dem = _split_pruebas(secs.get(13, ""))
        jurispruden_citada, criterio_sala = _split_jurisprudencia(fila.get("_jurisprudencia", ""))

        # Relaciones simples (lookup en colecciones de catálogo)
        # Decisión: fuente primaria la llamada JSON (enum), pero si viene vacía o
        # "No consta" se cae a la sección 16 del blob de extracción (que sí trae
        # "Declara (parcialmente) con lugar / sin lugar"). _clasificar_decision_
        # tribunal entiende ambas formas.
        _decision_txt = fila.get("Decisión Final", "")
        if _no_consta(_decision_txt):
            _decision_txt = secs.get(16, "")
        decision_doc_id    = self._lookup_decision_final(_decision_txt)
        tipo_accion_doc_id = self._lookup_tipo_accion(fila.get("Tipo de Acción", ""))
        tribunal_doc_id    = self._lookup_tribunal(fila.get("Tribunal", ""))
        # Administración determinada por la clasificación de contenido (prompt 4,
        # que lee el catálogo estructurado por entidad). Se usa como respaldo de
        # la entidad cuando el texto de la demandada es ambiguo, para que entidad
        # y categorías queden consistentes.
        adm_clasif = _extraer_administracion_catalogo(fila.get("Categorización", ""))
        entidad_doc_id     = self._lookup_or_create_entidad(
            _entidad_demandada(secs.get(5, ""), fila.get("Demandado", "")), adm_clasif)
        estado_doc_id      = self._lookup_estado_juicio(fila.get("Estado de Juicio", ""))

        # Jueces: sección 3. El Tribunal lo integran 3 jueces = ponente + 2.
        # "causa_jueces" (Otros jueces) son los OTROS, sin el ponente; se
        # deduplica por persona (variantes/orden/honorífico) y se topa en 2 (#1).
        sec3_raw = secs.get(3, "")
        ponente_nombre, todos_nombres = self._parsear_jueces_seccion(sec3_raw)
        ponente_doc_id = self._lookup_or_create_juez(ponente_nombre)
        pon_tokens = _tokens_nombre(ponente_nombre)
        otros_nombres = _dedup_jueces(
            [n for n in todos_nombres if not _misma_persona(_tokens_nombre(n), pon_tokens)],
            limite=2,
        )
        print(f"  [DBG sec3] {sec3_raw[:120]!r} → ponente={ponente_nombre!r} otros={otros_nombres}")
        vistos_ids = {ponente_doc_id} if ponente_doc_id else set()
        todos_doc_ids = []
        for n in otros_nombres:
            d = self._lookup_or_create_juez(n)
            if d and d not in vistos_ids:
                vistos_ids.add(d)
                todos_doc_ids.append(d)

        # Voto salvado: sección 18 → primera línea no vacía, sin markdown
        voto_raw = _re.sub(r'[\*#_]', '', secs.get(18, "")).strip().splitlines()
        voto_nombre = next((l.strip() for l in voto_raw if l.strip()), "")
        voto_salvado_doc_id = self._lookup_or_create_juez(voto_nombre)

        # Abogados: parseados por config.pdf_utils.buscar_abogados (sección 5)
        # en resolucion/ai/tribunal.py, propagados hasta fila.
        abogado_actor_doc_id     = self._lookup_or_create_abogado(fila.get("Abogado del Actor", ""))
        abogado_demandado_doc_id = self._lookup_or_create_abogado(fila.get("Abogado del Demandado", ""))

        # Actor: se normaliza y luego se CANONIZA contra los actores ya
        # existentes, para reutilizar la misma escritura y no crear una variante
        # nueva por cada diferencia de puntuación/tildes (#12).
        # Si SATJE trae al representante ("Procurador Común del Consorcio…",
        # "Procuradores Judiciales del Prefecto…"), se usa la parte que nombra
        # la IA en la sección 5 ("Actor (Demandante): Consorcio H&T").
        actor_ia = _actor_de_partes(secs.get(5, ""))
        if actor_ia and _RE_REPRESENTANTE.search(fila.get("Actor", "")):
            from resolucion.clasificar_actor import clasificar_tipo_actor, normalizar_nombre_actor
            fila["Tipo de Actor"] = clasificar_tipo_actor(actor_ia)
            fila["Actor Normalizado"] = normalizar_nombre_actor(actor_ia, fila["Tipo de Actor"])
        _actor_norm = normalizar_case_nombre(fila.get("Actor Normalizado", "")) or None
        _actor_disp = self._canonizar_actor(_actor_norm or normalizar_case_nombre(fila.get("Actor", "")))
        _actor_norm = self._canonizar_actor(_actor_norm) if _actor_norm else None

        data: dict = {
            "juicio":                       fila.get("Número de juicio", ""),
            # Sin esto el CMS ponía su default heredado de tributaria
            # ("Tribunal de lo Contencioso Tributario").
            "instancia":                    MATERIA.NOMBRE_PRIMERA_INSTANCIA,
            "fecha_de_emision":             _parsear_fecha_iso(secs.get(4, "")),
            # El actor es la parte (la compañía cuando comparece un representante):
            # se usa el nombre normalizado (extrae la empresa) como display, no el
            # string crudo del representante que trae SATJE.
            "actor":                        _actor_disp,
            "tipo_actor":                   fila.get("Tipo de Actor", "") or None,
            "actor_normalizado":            _actor_norm,
            "resumen_del_caso":             _limpiar_md(fila.get("Resumen Ejecutivo", "")),
            "cuantia":                      cuantia_num,
            "detalle_de_cuantia":           detalle_de_cuantia,
            "objeto_de_controversia":       objeto_controversia,
            MATERIA.CAMPO_STRAPI_SEC9:      _limpiar_md(secs.get(9, "")),
            MATERIA.CAMPO_STRAPI_SEC10:     _limpiar_md(secs.get(10, "")),
            "antecedentes":                 _limpiar_md(secs.get(12, "")),
            "pruebas_del_actor":            _limpiar_md(pruebas_actor),
            "pruebas_del_demandado":        _limpiar_md(pruebas_dem),
            "argumentos_del_actor":         _limpiar_md(secs.get(14, "")),
            "argumentos_del_demandado":     _limpiar_md(secs.get(15, "")),
            "medidas_adicionales":          _limpiar_md(secs.get(17, "")),
            "argumento_juez_salvado":       _limpiar_md(secs.get(19, "")),
            "jurisprudencia_mencionada":    jurispruden_citada,
            "criterio_sala_para_resolver":  criterio_sala,
            "observaciones":                fila.get("Observaciones", ""),
            "publishedAt":                  _now_iso(),
        }

        # Vincular PDF de sentencia tribunal si ya fue subido
        file_id_pdf = fila.get("_file_id_pdf_tribunal")
        if file_id_pdf:
            data["file_pdf"] = file_id_pdf
        if decision_doc_id:
            data["decision_final"]    = decision_doc_id
        if tipo_accion_doc_id:
            data["tipo_de_accion"]    = tipo_accion_doc_id
        if tribunal_doc_id:
            data["causa_tribunal"]    = tribunal_doc_id
        if entidad_doc_id:
            data["entidad_demandada"] = entidad_doc_id
        if estado_doc_id:
            data["estado_del_juicio"] = estado_doc_id
        # Relaciones de contenido extraído: se ESCRIBEN SIEMPRE (con `set`/`null`)
        # para que una re-extracción REEMPLACE lo anterior en vez de acumular.
        # (Con `connect` Strapi suma relaciones → quedaban jueces/abogados viejos.)
        data["juez"]              = ponente_doc_id or None
        data["causa_jueces"]      = {"set": todos_doc_ids}
        data["voto_salvado"]      = voto_salvado_doc_id or None
        data["abogado_actor"]     = abogado_actor_doc_id or None
        data["abogado_demandado"] = abogado_demandado_doc_id or None
        if fila.get("_fecha_inicio_caso"):
            data["fecha_inicio_caso"]    = fila["_fecha_inicio_caso"]
        if fila.get("_fecha_sentencia_caso"):
            data["fecha_sentencia_caso"] = fila["_fecha_sentencia_caso"]
        if fila.get("_duracion_dias") is not None:
            data["duracion_dias"]        = fila["_duracion_dias"]

        # Categorías tributarias (manyToMany) — `set` reemplaza el set completo.
        categoria_ids = self._lookup_categorias(fila.get("Categorización", ""))
        data["categorias"] = {"set": categoria_ids or []}

        return {"data": data}

    # ── Casación CNJ ──────────────────────────────────────────────────────────

    def guardar_casacion(self, fila: dict, causa_doc_id: str) -> Optional[dict]:
        """Crea o actualiza el registro de casación vinculado a una causa."""
        if not causa_doc_id:
            return None

        extraccion  = fila.get("_extraccion_cnj", "") or ""
        jurispruden = fila.get("_jurisprudencia_cnj", "") or ""

        # Parsear secciones del blob de extracción CNJ (prompt 5, 14 secciones)
        # sec 2=jueces, 3=fecha emisión, 4=tribunal origen, 5=antecedentes,
        # 6=decisión tribunal distrital, 7=recurrente, 8=causales,
        # 9=args recurrente, 10=args contraparte, 11=decisión final,
        # 12=medidas adicionales, 13=voto salvado, 14=arg voto
        # Usa parser por título como fallback cuando Qwen omite los números
        secs = _parsear_secciones_cnj(extraccion)

        # Sección 3: Fecha de emisión (formato ISO YYYY-MM-DD para Strapi)
        fecha_emision = _parsear_fecha_iso(secs.get(3, ""))
        if not fecha_emision and fila.get("_cnj_sin_fondo"):
            # Auto de inadmisión/desistimiento sin análisis IA: la fecha es la
            # de la actuación en SATJE.
            fecha_emision = fila.get("_fecha_sentencia_cnj") or None

        # Decisión final de casación (Casa / No casa / Casa parcialmente).
        # Fuente PRIMARIA: el tipo de actuación que SATJE le puso a la
        # resolución (detectar_sentencia_cnj, en resolucion/satje/corte.py) —
        # viene del propio sistema judicial ("SENTENCIA CASANDO SENTENCIA...",
        # "NO CASAR SENTENCIA...", etc.), no es una interpretación de IA sobre
        # texto libre. Fallback: sección 11 de la extracción CNJ, por si el
        # dato de SATJE no fue sustantivo (inadmisión, desistimiento, etc.).
        resultado_satje = fila.get("Resultado en Corte", "")
        texto_decision = _limpiar_md(secs.get(11, "") or "")
        primera_linea_decision = next(
            (l.strip() for l in texto_decision.splitlines() if l.strip()), ""
        )
        # El prompt 5 cierra la sección 11 con la frase canónica ("Casa la
        # sentencia."); si está, manda sobre el párrafo resolutivo, donde
        # "acepta parcialmente la demanda" se leía como casación parcial.
        canonicas = {_normalizar(c): c for c in MATERIA.DECISIONES_CASACION}
        linea_canonica = next((canonicas[_normalizar(l)] for l in reversed(texto_decision.splitlines())
                               if _normalizar(l) in canonicas), "")
        if linea_canonica:
            primera_linea_decision = linea_canonica
        from resolucion.satje.corte import DESCRIPCIONES_TERMINALES_SIN_FONDO
        # Una frase canónica propia que no es Casa/No casa/Casa parcialmente
        # ("Inadmite el recurso de casación.") se guarda tal cual: si pasara
        # por _clasificar_decision_casacion se volvería "No se casó" y mezclaría
        # en la jurimetría las inadmisiones con los rechazos de fondo.
        canonica_propia = linea_canonica if linea_canonica in MATERIA.DECISIONES_CASACION[3:] else ""
        decision_clasificada = (
            DESCRIPCIONES_TERMINALES_SIN_FONDO.get(resultado_satje)
            or _clasificar_decision_casacion(resultado_satje)
            or canonica_propia
            or _clasificar_decision_casacion(primera_linea_decision)
        )
        decision_doc_id = self._lookup_decision_final_casacion(
            decision_clasificada or primera_linea_decision
        )

        # Jueces sección 2 CNJ: "jueces" son los OTROS (sin el ponente), dedup + tope 2.
        ponente_nombre, todos_nombres = self._parsear_jueces_seccion(secs.get(2, ""))
        ponente_doc_id = self._lookup_or_create_juez(ponente_nombre)
        pon_tokens_cnj = _tokens_nombre(ponente_nombre)
        otros_nombres = _dedup_jueces(
            [n for n in todos_nombres if not _misma_persona(_tokens_nombre(n), pon_tokens_cnj)],
            limite=2,
        )
        vistos_ids_cnj = {ponente_doc_id} if ponente_doc_id else set()
        todos_doc_ids = []
        for n in otros_nombres:
            d = self._lookup_or_create_juez(n)
            if d and d not in vistos_ids_cnj:
                vistos_ids_cnj.add(d)
                todos_doc_ids.append(d)

        # Voto salvado casación: sección 13 → primera línea no vacía, sin markdown
        voto_raw_cnj = _re.sub(r'[\*#_]', '', secs.get(13, "")).strip().splitlines()
        voto_nombre_cnj = next((l.strip() for l in voto_raw_cnj if l.strip()), "")
        voto_salvado_doc_id = self._lookup_or_create_juez(voto_nombre_cnj)

        # Sección 7: Recurrente (quién interpuso la casación)
        recurrente_doc_id = self._resolver_recurrente(
            secs.get(7, ""), fila.get("Actor", ""), fila.get("Demandado", "")
        )

        # Sección 8: Causal o causales invocadas (Art. 268 COGEP) — siempre
        # debería haber al menos una si hay casación.
        causal_ids = self._lookup_causales(secs.get(8, ""))

        cnj_citada, cnj_criterio = _split_jurisprudencia(jurispruden)

        # #15: Antecedentes del caso en casación. NUNCA se reutiliza el resumen
        # ejecutivo (es un dato distinto). Cadena de respaldo para que el campo
        # jamás quede vacío:
        #   1) Sección 5 del prompt 5 (antecedentes CNJ) — fuente primaria.
        #   2) Sección 12 del prompt 2 = antecedentes del TRIBUNAL de la misma
        #      causa (trasfondo procesal real, tampoco es el resumen).
        #   3) Reseña factual mínima armada con la metadata (recurrente +
        #      tribunal de origen), como último recurso.
        antecedentes_cas = _limpiar_md(secs.get(5, ""))
        if not antecedentes_cas.strip():
            secs_trib = _parsear_secciones(fila.get("_extraccion", "") or "")
            antecedentes_cas = _limpiar_md(secs_trib.get(12, ""))
        if not antecedentes_cas.strip():
            _primera = lambda s: next(
                (l.strip() for l in _limpiar_md(s).splitlines() if l.strip()), ""
            )
            recurrente_txt = _primera(secs.get(7, "")) or (fila.get("Actor", "") or "").strip()
            origen_txt = _primera(secs.get(4, ""))
            frase = "Recurso de casación"
            if recurrente_txt:
                frase += f" interpuesto por {recurrente_txt}"
            if origen_txt:
                frase += f" respecto de la causa resuelta por {origen_txt}"
            antecedentes_cas = frase + "."

        data: dict = {
            "causa":                       causa_doc_id,
            "instancia":                   "Corte Nacional de Justicia",
            "antecedentes":                antecedentes_cas,
            "argumentos_del_recurrente":   _limpiar_md(secs.get(9, "") or extraccion or "Pendiente"),
            "argumentos_contraparte":      _limpiar_md(secs.get(10, "")),
            "criterio_sala_para_resolver": cnj_criterio or "Pendiente",
            "jurisprudencia_mencionada":   cnj_citada,
            "medidas_adicionales":         _limpiar_md(secs.get(12, "")),
            "argumento_juez_salvado":      _limpiar_md(secs.get(14, "")),
            "observaciones":               fila.get("Completitud CNJ", ""),
        }

        # fecha_de_emision solo si se parseó correctamente (campo date → null causa 400)
        if fecha_emision:
            data["fecha_de_emision"] = fecha_emision

        # Vincular PDF de sentencia CNJ si ya fue subido
        file_id_cnj = fila.get("_file_id_pdf_cnj")
        if file_id_cnj:
            data["file_pdf"] = file_id_cnj
        if decision_doc_id:
            data["decision_final"] = decision_doc_id
        # Relaciones de contenido: `set`/`null` para que la re-extracción REEMPLACE.
        data["juez"]             = ponente_doc_id or None
        data["jueces"]           = {"set": todos_doc_ids}
        data["voto_salvado"]     = voto_salvado_doc_id or None
        data["recurrentes"]      = {"set": [recurrente_doc_id] if recurrente_doc_id else []}
        data["causal_invocadas"] = {"set": causal_ids or []}
        if fila.get("_fecha_inicio_cnj"):
            data["fecha_inicio_cnj"]    = fila["_fecha_inicio_cnj"]
        if fila.get("_fecha_sentencia_cnj"):
            data["fecha_sentencia_cnj"] = fila["_fecha_sentencia_cnj"]
        if fila.get("_duracion_dias_cnj") is not None:
            data["duracion_dias_cnj"]   = fila["_duracion_dias_cnj"]
        # Categorías del catálogo del RECURSO (prompt 4 sobre esta sentencia).
        # Solo si la fila trae la clasificación: un guardado sin IA (solo
        # descarga, auto sin fondo) no borra las que ya tenga el registro.
        if "_categorizacion_cnj" in fila:
            data["categorias"] = {"set": self._lookup_categorias(fila["_categorizacion_cnj"]) or []}

        payload = {"data": data}

        existente = self._buscar_casacion(causa_doc_id)
        if existente:
            doc_id = existente["documentId"]
            return self._put(f"/api/casaciones/{doc_id}", payload)
        else:
            return self._post("/api/casaciones", payload)

    def _buscar_casacion(self, causa_doc_id: str) -> Optional[dict]:
        try:
            resp = httpx.get(
                f"{STRAPI_URL}/api/casaciones",
                params={"filters[causa][documentId][$eq]": causa_doc_id,
                        "fields[0]": "documentId"},
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            data = resp.json().get("data", [])
            return data[0] if data else None
        except Exception:
            return None

    def guardar_apelacion(self, fila: dict, causa_doc_id: str) -> Optional[dict]:
        """Crea o actualiza el registro de APELACIÓN (Corte Provincial) vinculado a una causa.
        Espejo de guardar_casacion; content-type `apelaciones`, catálogo `decisiones-finales-apelacion`."""
        if not causa_doc_id:
            return None

        extraccion  = fila.get("_extraccion_apel", "") or ""
        jurispruden = fila.get("_jurisprudencia_apel", "") or ""

        # Parsear secciones del blob de extracción CNJ (prompt 11, 14 secciones)
        # sec 2=jueces, 3=fecha emisión, 4=tribunal origen, 5=antecedentes,
        # 6=decisión tribunal distrital, 7=recurrente, 8=causales,
        # 9=args recurrente, 10=args contraparte, 11=decisión final,
        # 12=medidas adicionales, 13=voto salvado, 14=arg voto
        # Usa parser por título como fallback cuando Qwen omite los números
        secs = _parsear_secciones_cnj(extraccion)

        # Sección 3: Fecha de emisión (formato ISO YYYY-MM-DD para Strapi)
        fecha_emision = _parsear_fecha_iso(secs.get(3, ""))

        # Decisión final de casación (Casa / No casa / Casa parcialmente).
        # Fuente PRIMARIA: el tipo de actuación que SATJE le puso a la
        # resolución (detectar_sentencia_cnj, en resolucion/satje/corte.py) —
        # viene del propio sistema judicial ("SENTENCIA CASANDO SENTENCIA...",
        # "NO CASAR SENTENCIA...", etc.), no es una interpretación de IA sobre
        # texto libre. Fallback: sección 11 de la extracción CNJ, por si el
        # dato de SATJE no fue sustantivo (inadmisión, desistimiento, etc.).
        resultado_satje = fila.get("Resultado en Apelación", "")
        texto_decision = _limpiar_md(secs.get(11, "") or "")
        primera_linea_decision = next(
            (l.strip() for l in texto_decision.splitlines() if l.strip()), ""
        )
        # El prompt 11 cierra la sección 11 con la frase canónica ("Confirma la
        # sentencia."); si está, manda sobre el párrafo resolutivo, donde frases
        # como "no se advierte nulidad" o "reforma la cuantía" confundían.
        canonicas = {_normalizar(c): c for c in MATERIA.DECISIONES_APELACION}
        linea_canonica = next((canonicas[_normalizar(l)] for l in reversed(texto_decision.splitlines())
                               if _normalizar(l) in canonicas), "")
        if linea_canonica:
            primera_linea_decision = linea_canonica
        decision_clasificada = (
            _clasificar_decision_apelacion(resultado_satje)
            or _clasificar_decision_apelacion(primera_linea_decision)
        )
        decision_doc_id = self._lookup_decision_final_apelacion(
            decision_clasificada or primera_linea_decision
        )

        # Jueces sección 2 CNJ: "jueces" son los OTROS (sin el ponente), dedup + tope 2.
        ponente_nombre, todos_nombres = self._parsear_jueces_seccion(secs.get(2, ""))
        ponente_doc_id = self._lookup_or_create_juez(ponente_nombre)
        pon_tokens_cnj = _tokens_nombre(ponente_nombre)
        otros_nombres = _dedup_jueces(
            [n for n in todos_nombres if not _misma_persona(_tokens_nombre(n), pon_tokens_cnj)],
            limite=2,
        )
        vistos_ids_cnj = {ponente_doc_id} if ponente_doc_id else set()
        todos_doc_ids = []
        for n in otros_nombres:
            d = self._lookup_or_create_juez(n)
            if d and d not in vistos_ids_cnj:
                vistos_ids_cnj.add(d)
                todos_doc_ids.append(d)

        # Voto salvado casación: sección 13 → primera línea no vacía, sin markdown
        voto_raw_cnj = _re.sub(r'[\*#_]', '', secs.get(13, "")).strip().splitlines()
        voto_nombre_cnj = next((l.strip() for l in voto_raw_cnj if l.strip()), "")
        voto_salvado_doc_id = self._lookup_or_create_juez(voto_nombre_cnj)

        # Sección 7: Recurrente (quién interpuso la casación)
        recurrente_doc_id = self._resolver_recurrente(
            secs.get(7, ""), fila.get("Actor", ""), fila.get("Demandado", "")
        )

        # Sección 8: Causal o causales invocadas (Art. 268 COGEP) — siempre
        # debería haber al menos una si hay casación.
        causal_ids = self._lookup_causales(secs.get(8, ""))

        cnj_citada, cnj_criterio = _split_jurisprudencia(jurispruden)

        # #15: Antecedentes del caso en casación. NUNCA se reutiliza el resumen
        # ejecutivo (es un dato distinto). Cadena de respaldo para que el campo
        # jamás quede vacío:
        #   1) Sección 5 del prompt 5 (antecedentes CNJ) — fuente primaria.
        #   2) Sección 12 del prompt 2 = antecedentes del TRIBUNAL de la misma
        #      causa (trasfondo procesal real, tampoco es el resumen).
        #   3) Reseña factual mínima armada con la metadata (recurrente +
        #      tribunal de origen), como último recurso.
        antecedentes_cas = _limpiar_md(secs.get(5, ""))
        if not antecedentes_cas.strip():
            secs_trib = _parsear_secciones(fila.get("_extraccion", "") or "")
            antecedentes_cas = _limpiar_md(secs_trib.get(12, ""))
        if not antecedentes_cas.strip():
            _primera = lambda s: next(
                (l.strip() for l in _limpiar_md(s).splitlines() if l.strip()), ""
            )
            recurrente_txt = _primera(secs.get(7, "")) or (fila.get("Actor", "") or "").strip()
            origen_txt = _primera(secs.get(4, ""))
            frase = "Recurso de apelación"
            if recurrente_txt:
                frase += f" interpuesto por {recurrente_txt}"
            if origen_txt:
                frase += f" respecto de la causa resuelta por {origen_txt}"
            antecedentes_cas = frase + "."

        data: dict = {
            "causa":                       causa_doc_id,
            "instancia":                   "Corte Provincial de Justicia",
            "antecedentes":                antecedentes_cas,
            "argumentos_del_recurrente":   _limpiar_md(secs.get(9, "") or extraccion or "Pendiente"),
            "argumentos_contraparte":      _limpiar_md(secs.get(10, "")),
            "criterio_sala_para_resolver": cnj_criterio or "Pendiente",
            "jurisprudencia_mencionada":   cnj_citada,
            "medidas_adicionales":         _limpiar_md(secs.get(12, "")),
            "argumento_juez_salvado":      _limpiar_md(secs.get(14, "")),
            "observaciones":               fila.get("Completitud Apelación", ""),
        }

        # fecha_de_emision solo si se parseó correctamente (campo date → null causa 400)
        if fecha_emision:
            data["fecha_de_emision"] = fecha_emision

        # Vincular PDF de sentencia CNJ si ya fue subido
        file_id_cnj = fila.get("_file_id_pdf_apel")
        if file_id_cnj:
            data["file_pdf"] = file_id_cnj
        if decision_doc_id:
            data["decision_final"] = decision_doc_id
        # Relaciones de contenido: `set`/`null` para que la re-extracción REEMPLACE.
        data["juez"]             = ponente_doc_id or None
        data["jueces"]           = {"set": todos_doc_ids}
        data["voto_salvado"]     = voto_salvado_doc_id or None
        data["recurrentes"]      = {"set": [recurrente_doc_id] if recurrente_doc_id else []}
        data["causal_invocadas"] = {"set": causal_ids or []}
        if fila.get("_fecha_inicio_apel"):
            data["fecha_inicio_apel"]    = fila["_fecha_inicio_apel"]
        if fila.get("_fecha_sentencia_apel"):
            data["fecha_sentencia_apel"] = fila["_fecha_sentencia_apel"]
        if fila.get("_duracion_dias_apel") is not None:
            data["duracion_dias_apel"]   = fila["_duracion_dias_apel"]
        # Categorías del catálogo del RECURSO (prompt 4 sobre esta sentencia).
        # Solo si la fila trae la clasificación: un guardado sin IA (solo
        # descarga, auto sin fondo) no borra las que ya tenga el registro.
        if "_categorizacion_apel" in fila:
            data["categorias"] = {"set": self._lookup_categorias(fila["_categorizacion_apel"]) or []}

        payload = {"data": data}

        # Buscar la APELACIÓN existente (antes buscaba la casación por error:
        # al re-extraer, PUT a /api/apelaciones con el id de la casación → 404,
        # y sin casación creaba una apelación duplicada).
        existente = self._buscar_apelacion(causa_doc_id)
        if existente:
            doc_id = existente["documentId"]
            return self._put(f"/api/apelaciones/{doc_id}", payload)
        else:
            return self._post("/api/apelaciones", payload)

    def _buscar_apelacion(self, causa_doc_id: str) -> Optional[dict]:
        try:
            resp = httpx.get(
                f"{STRAPI_URL}/api/apelaciones",
                params={"filters[causa][documentId][$eq]": causa_doc_id,
                        "fields[0]": "documentId"},
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            data = resp.json().get("data", [])
            return data[0] if data else None
        except Exception:
            return None

    def actualizar_actor(self, numero: str, tipo_actor: str, actor_normalizado: str) -> bool:
        """Actualiza solo los campos de actor en una causa existente. Retorna True si OK."""
        existente = self._buscar_causa(numero)
        if not existente:
            return False
        doc_id = existente["documentId"]
        result = self._put(f"/api/causas/{doc_id}", {
            "data": {
                "tipo_actor":        tipo_actor or None,
                "actor_normalizado": actor_normalizado or None,
            }
        })
        return result is not None

    def listar_actores_agrupados(self) -> list[dict]:
        """
        Devuelve lista de actores únicos con su tipo y conteo de causas,
        ordenados por conteo DESC. Itera todas las páginas de Strapi.
        """
        from collections import defaultdict
        conteos: dict[str, dict] = defaultdict(lambda: {"tipo": None, "count": 0, "actor_raw": ""})

        page = 1
        total_pages = None
        while True:
            try:
                resp = httpx.get(
                    f"{STRAPI_URL}/api/causas",
                    params={
                        "filters[actor_normalizado][$notNull]": "true",
                        "fields[0]": "juicio",
                        "fields[1]": "actor_normalizado",
                        "fields[2]": "tipo_actor",
                        "fields[3]": "actor",
                        "pagination[page]": page,
                        "pagination[pageSize]": 100,
                    },
                    headers=self._headers,
                    timeout=_TIMEOUT,
                )
                body = resp.json()
            except Exception as e:
                print(f"  [WARN Strapi listar_actores_agrupados p{page}] {e}")
                break

            causas = body.get("data", [])
            pag = body.get("meta", {}).get("pagination", {})
            if total_pages is None:
                total_pages = pag.get("pageCount", 1)

            for item in causas:
                attrs = item.get("attributes", item)
                norm = (attrs.get("actor_normalizado") or "").strip()
                if not norm:
                    continue
                tipo = attrs.get("tipo_actor") or ""
                raw  = attrs.get("actor") or ""
                entry = conteos[norm]
                entry["count"] += 1
                if not entry["tipo"]:
                    entry["tipo"] = tipo
                if not entry["actor_raw"]:
                    entry["actor_raw"] = raw

            if page >= total_pages:
                break
            page += 1

        result = [
            {"actor_normalizado": k, "tipo_actor": v["tipo"], "count": v["count"]}
            for k, v in conteos.items()
        ]
        result.sort(key=lambda x: x["count"], reverse=True)
        return result

    def listar_causas_actor(self, actor_normalizado: str,
                             page: int = 1, page_size: int = 25) -> dict:
        """Devuelve causas de un actor normalizado específico."""
        try:
            resp = httpx.get(
                f"{STRAPI_URL}/api/causas",
                params={
                    "filters[actor_normalizado][$eq]": actor_normalizado,
                    "fields[0]": "juicio",
                    "fields[1]": "actor",
                    "fields[2]": "actor_normalizado",
                    "fields[3]": "tipo_actor",
                    "fields[4]": "resumen_del_caso",
                    "pagination[page]": page,
                    "pagination[pageSize]": page_size,
                    "sort[0]": "juicio:asc",
                },
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            return resp.json()
        except Exception as e:
            print(f"  [WARN Strapi listar_causas_actor] {e}")
            return {"data": [], "meta": {"pagination": {"pageCount": 0, "total": 0}}}

    def listar_causas_sin_clasificar(self, page: int = 1, page_size: int = 100) -> dict:
        """Devuelve causas con actor pero sin tipo_actor, para el backfill."""
        try:
            resp = httpx.get(
                f"{STRAPI_URL}/api/causas",
                params={
                    "filters[actor][$notNull]": "true",
                    "filters[tipo_actor][$null]": "true",
                    "fields[0]": "documentId",
                    "fields[1]": "juicio",
                    "fields[2]": "actor",
                    "pagination[page]": page,
                    "pagination[pageSize]": page_size,
                },
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            return resp.json()
        except Exception as e:
            print(f"  [WARN Strapi listar_causas_sin_clasificar] {e}")
            return {"data": [], "meta": {"pagination": {"pageCount": 0}}}

    def _buscar_causa(self, numero: str) -> Optional[dict]:
        try:
            resp = httpx.get(
                f"{STRAPI_URL}/api/causas",
                params={"filters[juicio][$eq]": numero, "fields[0]": "documentId"},
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            data = resp.json().get("data", [])
            return data[0] if data else None
        except Exception:
            return None

    def actualizar_jurisprudencia_causa(self, numero: str, jurisprudencia_mencionada: str,
                                         criterio_sala_para_resolver: str) -> bool:
        """
        Backfill de jurisprudencia — actualiza SOLO esos dos campos en una
        causa ya existente, sin tocar el resto del registro (mismo patrón
        que actualizar_actor).
        """
        existente = self._buscar_causa(numero)
        if not existente:
            return False
        doc_id = existente["documentId"]
        result = self._put(f"/api/causas/{doc_id}", {
            "data": {
                "jurisprudencia_mencionada":   jurisprudencia_mencionada or None,
                "criterio_sala_para_resolver": criterio_sala_para_resolver or None,
            }
        })
        return result is not None

    def actualizar_jurisprudencia_casacion(self, numero: str, jurisprudencia_mencionada: str,
                                            criterio_sala_para_resolver: str) -> bool:
        """Ídem actualizar_jurisprudencia_causa, para la casación vinculada a la causa."""
        causa = self._buscar_causa(numero)
        if not causa:
            return False
        casacion = self._buscar_casacion(causa["documentId"])
        if not casacion:
            return False
        doc_id = casacion["documentId"]
        result = self._put(f"/api/casaciones/{doc_id}", {
            "data": {
                "jurisprudencia_mencionada":   jurisprudencia_mencionada or None,
                "criterio_sala_para_resolver": criterio_sala_para_resolver or None,
            }
        })
        return result is not None

    # ── PDF upload ────────────────────────────────────────────────────────────

    def subir_archivo(self, contenido: bytes, nombre: str,
                      mime: str = "application/octet-stream") -> Optional[str]:
        """Sube cualquier archivo a la media library de Strapi. Devuelve la URL o None."""
        url, _ = self._subir_archivo_raw(contenido, nombre, mime)
        return url

    def _subir_archivo_raw(self, contenido: bytes, nombre: str,
                           mime: str = "application/octet-stream") -> tuple:
        """Sube archivo. Retorna (url, file_id) — ambos None si falla."""
        try:
            resp = httpx.post(
                f"{STRAPI_URL}/api/upload",
                headers={"Authorization": f"Bearer {STRAPI_TOKEN}"},
                files={"files": (nombre, contenido, mime)},
                timeout=60,
            )
            data = resp.json()
            if isinstance(data, list) and data:
                url = data[0].get("url", "")
                if url and url.startswith("/"):
                    url = STRAPI_URL.rstrip("/") + url
                file_id = data[0].get("id")
                return (url or None, file_id)
            else:
                print(f"  [WARN Strapi upload {nombre}] HTTP {resp.status_code} — respuesta: {str(data)[:300]}")
        except Exception as e:
            print(f"  [WARN Strapi upload {nombre}] {type(e).__name__}: {e}")
        return None, None

    def subir_pdf(self, pdf_bytes: bytes, nombre: str) -> Optional[str]:
        """Sube un PDF a la media library de Strapi. Devuelve la URL o None."""
        return self.subir_archivo(pdf_bytes, nombre, "application/pdf")

    def subir_pdf_con_id(self, pdf_bytes: bytes, nombre: str) -> tuple:
        """Sube un PDF. Retorna (url, file_id) para poder vincular el archivo a un campo."""
        return self._subir_archivo_raw(pdf_bytes, nombre, "application/pdf")

    def subir_docx(self, docx_bytes: bytes, nombre: str) -> Optional[str]:
        """Sube un Word (.docx) a la media library de Strapi. Devuelve la URL o None."""
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return self.subir_archivo(docx_bytes, nombre, mime)

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict) -> Optional[dict]:
        try:
            resp = httpx.post(
                f"{STRAPI_URL}{path}",
                json=payload,
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            if not resp.is_success:
                raise RuntimeError(
                    f"HTTP {resp.status_code} en POST {path}: {resp.text[:500]}"
                )
            return resp.json()
        except Exception as e:
            print(f"  [WARN Strapi POST {path}] {e}")
            raise

    def _put(self, path: str, payload: dict) -> Optional[dict]:
        try:
            resp = httpx.put(
                f"{STRAPI_URL}{path}",
                json=payload,
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            if not resp.is_success:
                raise RuntimeError(
                    f"HTTP {resp.status_code} en PUT {path}: {resp.text[:500]}"
                )
            return resp.json()
        except Exception as e:
            print(f"  [WARN Strapi PUT {path}] {e}")
            raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parsear_cuantia(texto: str) -> Optional[float]:
    """Extrae el número de un string de cuantía como '$ 50.000,00'. None si no parsea."""
    import re
    if not texto:
        return None
    # Primer número con separadores: apóstrofe de millones ("2'955.797,07"),
    # espacio de miles ("18 653,62") y punto o coma decimal ("1,595.28").
    m = re.search(r"\d{1,3}(?:[ ' .,]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d+(?:[.,]\d{1,2})?(?!\d)", texto)
    if not m:
        return None
    num = re.sub(r"[ ' ]", "", m.group())
    seps = [i for i, c in enumerate(num) if c in ".,"]
    if seps:
        ult = seps[-1]
        # El último separador es decimal si lo siguen 1–2 dígitos; si lo siguen 3, es de miles.
        if len(num) - ult - 1 <= 2:
            num = num[:ult].replace(".", "").replace(",", "") + "." + num[ult + 1:]
        else:
            num = num.replace(".", "").replace(",", "")
    try:
        return float(num)
    except ValueError:
        return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _parsear_fecha_iso(texto: str) -> Optional[str]:
    """
    Extrae una fecha en formato YYYY-MM-DD desde texto en español.
    Ejemplos: '22 de abril de 2019', '2019-04-22', '22/04/2019'.
    Retorna None si no se puede parsear.
    """
    import re
    if not texto:
        return None
    MESES = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    }
    # "22 de abril de 2019" o "22 de abril del 2019"
    m = re.search(r'(\d{1,2})\s+de\s+(\w+)\s+del?\s+(\d{4})', texto, re.IGNORECASE)
    if m:
        dia, mes_str, año = m.group(1), m.group(2).lower(), m.group(3)
        mes = MESES.get(mes_str)
        if mes:
            return f"{año}-{mes:02d}-{int(dia):02d}"
    # "2019-04-22" o "22/04/2019"
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', texto)
    if m:
        return m.group(0)
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})', texto)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None
