"""
Controles deterministas sobre la ficha que devuelve la IA (auditoría v3).

Los prompts ya piden casi todo esto y el modelo igual lo incumple; aquí se
corrige en código o se marca para revisión ("Parcial - revisar"). El módulo es
genérico: lo que depende de la materia (números de sección, palabras clave,
textos estándar) vive en config/materia.py.

Uso (tribunal.py y corte.py):
    mayoria, voto = separar_voto(texto)              # antes de los prompts
    info = aplicar(resultado, mayoria, voto, nivel, prompts)  # antes del Word y de guardar
    info["senales"] → validacion_problemas;  info["ajustes"] → log

Contrato de formato: las secciones conservan su encabezado "N. Título:" y
ningún texto insertado empieza una línea con "N. " (el parser de Strapi parte
la extracción por esos encabezados).
"""

import re
from resolucion.ai import regla_cero
import unicodedata

from config import materia as MATERIA


def sin_tildes(texto: str) -> str:
    """Minúsculas y sin tildes (ñ → n): la forma en que se escriben los regex de materia.py."""
    t = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


# ── 1. Voto salvado / concurrente ─────────────────────────────────────────────

# Encabezado propio en MAYÚSCULAS ("VOTO SALVADO DE: DR. JUAN PÉREZ"); una
# mención en minúsculas dentro de un considerando no cuenta.
_RE_VOTO = re.compile(r"^[ \t]*VOTO\s+(SALVADO|CONCURRENTE|DE\s+MINOR[IÍ]A)\b([^\n]{0,160})$", re.M)
_RE_VOTO_INTRO = re.compile(
    r"^[\s:.\-–]*(?:DE(?:L|\s+LA)?\b\s*(?:(?:CON)?JUEZ[A]?\b\s*(?:NACIONAL|PROVINCIAL)?\s*)?[:.\-–]?\s*)?", re.I)
_RE_LINEA_JUEZ = re.compile(r"^\s*(?:(?:CON)?JUEZ[A]?(?:\s+PONENTE)?|AUTOR/?A?)\s*:\s*(.+)$", re.I | re.M)
_RE_HONORIFICO = re.compile(r"^(?:(?:dr|dra|ab|abg|abog|mgs|msc|mg|ing|lcdo|lcda|magister|phd)\.?\s+)+", re.I)
_MIN_MAYORIA = 500          # un encabezado al inicio del documento no parte nada
_PARTICULAS = {"de", "del", "la", "las", "los", "y"}


# Cargo pegado al nombre: "LÓPEZ MARÍA, JUEZA NACIONAL", "Pedro Mora (PONENTE)".
_RE_CARGO_FINAL = re.compile(r"\s*(?:,\s*|\s\(?)(?:(?:CON)?JUEZ[A]?\b|PONENTE\b|\(E\)).*$", re.I)


def _palabra_propia(p: str, i: int) -> str:
    if p.lower() in _PARTICULAS and i:
        return p.lower()
    if p.isupper() and len(p) > 1:   # solo las palabras en MAYÚSCULAS; "McAllister" queda igual
        return "-".join(s.capitalize() for s in p.split("-"))
    return p


def _nombre_propio(texto: str) -> str:
    """Nombre del juez sin títulos ("DR.", "Dra.") ni cargo, en Nombre Apellido
    con mayúsculas iniciales (las palabras en MAYÚSCULAS se normalizan)."""
    t = re.sub(r"\s+", " ", re.sub(r"[*_#]", "", texto or "")).strip(" .,:;-–")
    t = _RE_CARGO_FINAL.sub("", _RE_HONORIFICO.sub("", t)).strip(" .,:;-–")
    return " ".join(_palabra_propia(p, i) for i, p in enumerate(t.split()))


def separar_voto(texto: str) -> tuple:
    """Parte la sentencia en el primer encabezado de voto salvado/concurrente.

    Devuelve (mayoria, voto). `voto` es None si no hay encabezado (el texto no
    cambia) o {"votos": [(nombre, "salvado"|"concurrente")], "texto": str}.
    """
    encabezados = [m for m in _RE_VOTO.finditer(texto or "") if m.start() >= _MIN_MAYORIA]
    if not encabezados:
        return texto, None
    votos = []
    for m in encabezados:
        tipo = "concurrente" if m.group(1).upper() == "CONCURRENTE" else "salvado"
        nombre = _RE_VOTO_INTRO.sub("", m.group(2)).strip()
        if not nombre:   # "VOTO SALVADO" solo: el nombre suele venir en "JUEZ PONENTE:" debajo
            sig = _RE_LINEA_JUEZ.search(texto[m.end():m.end() + 300])
            nombre = sig.group(1) if sig else ""
        votos.append((_nombre_propio(nombre), tipo))
    inicio = encabezados[0].start()
    return texto[:inicio].rstrip(), {"votos": votos, "texto": texto[inicio:]}


_RE_FIRMA = re.compile(r"\bFirmado\s+(?:electr[oó]nicamente\s+)?por\b|\bC=EC\b", re.I)
# Inicio de oración: punto final y mayúscula ("fs. 169" o "dispone: “…" no lo son).
_RE_INICIO_ORACION = re.compile(r"[.!?]\s+(?=[A-ZÁÉÍÓÚÑ¿¡])")
# Número de página suelto ("3", "Página 3 de 10", "3/10").
_RE_PAGINA = re.compile(r"^\s*(?:p[aá]g(?:ina)?\.?\s*)?\d{1,3}(?:\s*(?:de|/)\s*\d{1,3})?\s*$", re.I)
# Restos del sello de firma electrónica que quedan antes de "Firmado por".
_RE_RESTO_FIRMA = re.compile(r"^\s*(?:firmado|por)(?:\s+(?:firmado|por))*\s*$", re.I)
# Palabra partida por guion al final de la línea ("moti-\nvación").
_RE_GUION_FINAL = re.compile(r"([A-Za-zÁÉÍÓÚÑáéíóúñü])-[ \t]*\n[ \t]*([a-záéíóúñü])")
# Palabra partida antes de una vocal tildada o ñ por la extracción del PDF
# ("art ículo", "Contrataci ón", "se ñalando"); no se unen las palabras reales
# que empiezan así.
_RE_TILDE_PARTIDA = re.compile(r"(?<=[A-Za-zñ])[ \t](?=([áéíóúñ][a-záéíóúñ]*)\b)")
_PALABRAS_CON_TILDE_INICIAL = {
    "á", "é", "ó", "ú", "él", "ámbito", "ámbitos", "área", "áreas", "árbitro", "árbitros", "ánimo",
    "ápice", "índice", "índices", "índole", "ínfimo", "ínfima", "íntegro", "íntegra", "íntegramente",
    "íntimo", "íntima", "ítem", "ítems", "órgano", "órganos", "óbice", "óptimo", "óptima", "último",
    "última", "últimos", "últimas", "único", "única", "únicos", "únicas", "únicamente", "éxito",
    "época", "épocas", "ética", "ético", "éticos", "éticas", "énfasis"}


def _unir_tilde_partida(texto: str) -> str:
    return _RE_TILDE_PARTIDA.sub(lambda m: m.group(0) if m.group(1) in _PALABRAS_CON_TILDE_INICIAL else "",
                                 texto)


def _limpiar_voto(cuerpo: str) -> str:
    """Quita del voto los saltos de página, los números de página, las palabras
    partidas y el bloque final de firmas (nombres y cargos en MAYÚSCULAS)."""
    t = (cuerpo or "").replace("\f", "\n").replace("\u00ad", "-")
    t = _RE_GUION_FINAL.sub(r"\1\2", t)
    lineas = [l for l in t.split("\n") if not _RE_PAGINA.match(l)]
    # Bloque de firmas: desde el final, líneas cortas sin minúsculas ("TRUJILLO
    # VELASCO PAULINA SALOME", "JUEZ DEL TRIBUNAL…", "(PONENTE)", "DR. …"); una
    # oración final en MAYÚSCULAS ("DESECHA LA DEMANDA.") termina en punto y se queda.
    while lineas and (not lineas[-1].strip() or _RE_RESTO_FIRMA.match(lineas[-1])
                      or (not re.search(r"[a-záéíóúñü]", lineas[-1]) and len(lineas[-1].split()) <= 10
                          and not lineas[-1].rstrip().endswith((".", ";", ":")))):
        lineas.pop()
    return _unir_tilde_partida("\n".join(lineas))


def extracto_voto(texto_voto: str, max_palabras: int) -> str:
    """Extracto del voto en un solo párrafo, sin la línea de encabezado ni las firmas.

    Elección (sin llamada a la IA, determinista): si el voto pasa del límite se
    toman las ÚLTIMAS `max_palabras`, que es donde está el análisis de la
    discrepancia y la parte resolutiva; los antecedentes que repiten los de la
    mayoría ("VISTOS", "Respetados…") quedan al inicio y se descartan. El corte
    arranca siempre en un inicio de oración: el de la oración en que cae el
    corte o, si completarla agrega más de un cuarto del límite, el siguiente.
    """
    cuerpo = (texto_voto or "").split("\n", 1)[1] if "\n" in (texto_voto or "") else ""
    firma = _RE_FIRMA.search(cuerpo)
    if firma:
        cuerpo = cuerpo[:firma.start()]
    palabras = _limpiar_voto(cuerpo).split()
    if len(palabras) <= max_palabras:
        return " ".join(palabras)
    texto = " ".join(palabras)
    corte = len(" ".join(palabras[:-max_palabras])) + 1
    inicios = [m.end() for m in _RE_INICIO_ORACION.finditer(texto)
               if not _RE_ABREVIATURA.search(texto[:m.start() + 1])]
    atras = max((p for p in inicios if p < corte), default=0)
    adelante = next((p for p in inicios if p >= corte), None)
    # Se completa la oración cortada si eso agrega hasta un cuarto del límite; si no, se salta a la siguiente.
    inicio = atras if adelante is None or len(texto[atras:corte].split()) <= max_palabras // 4 else adelante
    return ("[…] " if inicio else "") + texto[inicio:]


# ── Secciones de la extracción ───────────────────────────────────────────────

# Mismo criterio que worker/strapi_client._parsear_secciones.
_RE_ENCABEZADO = re.compile(r"^[ \t]*\**(?:#{1,3}\s*)?\**(\d+)\.[ \t]+[^\n]*", re.M)
_VACIOS = ("no consta", "no existe", "no hubo", "no aplica", "ninguno", "ninguna", "n/a", "no se registra")


def _vacio(texto: str) -> bool:
    t = sin_tildes(re.sub(r"[*_#•●\-–\s]+", " ", texto or "")).strip(' ."“”')
    return not t or t.startswith(_VACIOS)


def _ubicar(ext: str, n: int):
    """(inicio, fin, título, cuerpo) de la sección n, o None. El título llega
    hasta los dos puntos; lo que el modelo escribió después en la misma línea
    es parte del cuerpo."""
    heads = [(int(m.group(1)), m) for m in _RE_ENCABEZADO.finditer(ext or "")]
    for i, (num, m) in enumerate(heads):
        if num != n:
            continue
        siguientes = [h for k, h in heads[i + 1:] if k == n + 1] or [h for k, h in heads[i + 1:] if k > n]
        fin = siguientes[0].start() if siguientes else len(ext)
        linea = m.group(0)
        dos_puntos = linea.find(":")
        titulo = linea[:dos_puntos + 1] if dos_puntos != -1 else linea
        resto = linea[dos_puntos + 1:] if dos_puntos != -1 else ""
        return m.start(), fin, titulo.rstrip(), (resto + ext[m.end():fin]).strip()
    return None


def _cuerpo(ext: str, n) -> str:
    s = _ubicar(ext, n) if n else None
    return s[3] if s else None


def _reemplazar(ext: str, n, nuevo: str) -> str:
    s = _ubicar(ext, n) if n else None
    if not s:
        return ext
    ini, fin, titulo, _ = s
    return ext[:ini] + titulo + "\n" + nuevo.strip() + ("\n\n" if fin < len(ext) else "\n") + ext[fin:]


def _ultima_linea(texto: str) -> str:
    lineas = [l.strip(" *•") for l in (texto or "").splitlines() if l.strip(" *•")]
    return lineas[-1] if lineas else ""


# ── 2. Normalizaciones de forma ──────────────────────────────────────────────

_RE_ACOTACION = re.compile(r"[ \t]*\(m[aá]ximo\s+[^)\n]{1,15}\)", re.I)
_RE_OTROS_JUECES = re.compile(r"^[^\n:]*otros?\s+jueces?\b[^:\n]*:[ \t]*([^\n]*)\n?", re.I | re.M)
_RE_FIN_ORACION = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ\"“«])")
_RE_MENCION_VOTO = re.compile(r"votos?\s+(salvados?|concurrentes?|de\s+minor)|disiden", re.I)


def _quitar_otros_jueces(ext: str, n, siempre: bool) -> str:
    """Borra la línea "Otros Jueces" cuando no hay otros jueces (juez o auto unipersonal)."""
    cuerpo = _cuerpo(ext, n)
    if cuerpo is None:
        return ext

    def _sub(m):
        valor = sin_tildes(m.group(1))
        return "" if (siempre and _vacio(valor)) or valor.strip(" *").startswith("no aplica") else m.group(0)

    nuevo = _RE_OTROS_JUECES.sub(_sub, cuerpo)
    return ext if nuevo == cuerpo else _reemplazar(ext, n, nuevo)


def _contar(texto: str) -> int:
    return len(re.findall(r"\S+", texto or ""))


# Abreviaturas que no cierran oración ("el Dr. Pérez", "la Res. 12-2021").
_RE_ABREVIATURA = re.compile(r"\b(?:Dr|Dra|Ab|Abg|Sr|Sra|Srta|Ing|Lcdo|Lcda|Mgs|No|Nro|Núm|Art|Arts|Res|Inc|Lit)\.$")


def _oraciones(parrafo: str) -> list:
    partes = []
    for p in _RE_FIN_ORACION.split(parrafo):
        if partes and _RE_ABREVIATURA.search(partes[-1]):
            partes[-1] += " " + p
        elif p.strip():
            partes.append(p.strip())
    return partes


# Oración que se apoya en la anterior: conector, pronombre o verbo sin sujeto
# ("Asimismo…", "Esta última…", "Señalan…"). Se quita junto con la anterior o no se quita.
_RE_DEPENDIENTE = re.compile(
    r"^\W*(?:asimismo|ademas|adicionalmente|igualmente|tambien|finalmente|ello|por ello|por ende|"
    r"por (?:lo )?tanto|por (?:su|otra) parte|en consecuencia|consecuentemente|sin embargo|no obstante|"
    r"de igual (?:forma|manera)|del mismo modo|en ese sentido|"
    r"(?:est|es)[aeo]s?|aquel(?:la|lo)?s?|dich[oa]s?|tal(?:es)?|ambos|ambas|el mismo|la misma|sus?|quien(?:es)?|"
    r"senal\w*|indic\w*|sostuv\w*|sostien\w*|aleg\w*|argument\w*|manifest\w*|agreg\w*|afirm\w*|"
    r"adujo|aducen?|invoc\w*|expres\w*|refut\w*|insist\w*)\b")
# Oración que identifica a las partes o lo que se pidió.
_RE_IDENTIFICA = re.compile(
    r"\b(?:actor|actora|actores|accionantes?|demandantes?|demandad[oa]s?|recurrentes?|casacionista|"
    r"apelantes?|interpus\w*|propus\w*|demando|impugn\w*|pretend\w*|pretension\w*|"
    r"objeto (?:de la controversia|del litigio|del proceso|de la demanda|del juicio))\b")


def recortar_palabras(texto: str, maximo: int) -> str:
    """Recorta el resumen a `maximo` palabras quitando oraciones del medio.

    El prompt 1 pone la identificación del caso al inicio y la decisión al
    final. Nunca se tocan: el primer párrafo (o, en un resumen de un solo
    párrafo, la primera oración y las que nombran a las partes o la pretensión
    entre las cinco primeras), el último párrafo y las tres últimas oraciones.
    Una oración que empieza con un conector o un pronombre sin antecedente sale
    solo junto con la anterior. Se quitan oraciones desde el inicio del bloque
    intermedio (antecedentes) hasta entrar en el límite; si aun así no entra,
    el texto queda como está y aplicar() agrega una señal de revisión.
    """
    total = _contar(texto)
    if total <= maximo:
        return texto
    parrafos = [p.strip() for p in re.split(r"\n\s*\n", texto.strip()) if p.strip()]
    oraciones = [(i, o) for i, p in enumerate(parrafos) for o in _oraciones(p)]
    ultimo = len(parrafos) - 1
    identifica = [k for k in range(1, min(5, len(oraciones))) if _RE_IDENTIFICA.search(sin_tildes(oraciones[k][1]))]
    protegidas = set(range(max(identifica, default=0) + 1)) | set(range(max(0, len(oraciones) - 3), len(oraciones)))
    if ultimo > 0:
        protegidas |= {k for k, (i, _) in enumerate(oraciones) if i in (0, ultimo)}
    unidades = []
    for k, (_, o) in enumerate(oraciones):
        if unidades and _RE_DEPENDIENTE.match(sin_tildes(o)):
            unidades[-1].append(k)
        else:
            unidades.append([k])
    quitar = set()
    for unidad in unidades:
        if total <= maximo:
            break
        if protegidas.isdisjoint(unidad):
            quitar.update(unidad)
            total -= sum(_contar(oraciones[k][1]) for k in unidad)
    # Si ni quitando todo lo no protegido entra, igual se entrega lo más corto
    # posible: aplicar() agrega la señal de revisión.
    salida = {}
    for k, (i, o) in enumerate(oraciones):
        if k not in quitar:
            salida.setdefault(i, []).append(o)
    return "\n\n".join(" ".join(v) for _, v in sorted(salida.items()))


def _quitar_oraciones(texto: str, patron) -> str:
    partes = _RE_FIN_ORACION.split(texto or "")
    quedan = [p for p in partes if not patron.search(p)]
    return texto if len(quedan) == len(partes) else " ".join(quedan).strip()


# ── Función principal ────────────────────────────────────────────────────────

# Oración suelta "No consta en la sentencia." al final de un párrafo del resumen.
_RE_NO_CONSTA_SUELTA = re.compile(r"(?<=[.!?])[ \t]*No consta en la sentencia\.?[ \t]*$", re.I | re.M)


# Hora pegada a una fecha ("6 de diciembre de 2024, a las 15h30"): la ficha
# lleva solo la fecha (puerta civil v6: 24 fichas de apelación y casación).
_RE_HORA = re.compile(r"(\d{4})\s*,?\s*(?:a\s+)?las?\s+\d{1,2}\s*(?:[h:]\s*\d{2})?(?:\s*(?:minutos|horas|h)\b)?(?:\s*[:h]\s*\d{2})?", re.I)


_RE_LINEA_FECHA = re.compile(r"(?im)^(\s*(?:[^\n:]{0,60}fecha[^\n:]{0,60}:\s*)?\d{1,2}\s+de\s+\w+\s+(?:de|del)\s+\d{4}[^\n]*)$")


def sin_hora(texto: str) -> str:
    """Quita la hora solo en las líneas que son una fecha (no en citas)."""
    return _RE_LINEA_FECHA.sub(lambda m: _RE_HORA.sub(r"\1", m.group(1)), texto)


def aplicar(resultado: dict, sentencia: str, voto: dict = None, nivel: str = "tribunal",
            prompts: list = None) -> dict:
    """Aplica los controles sobre resultado["resumen"|"extraccion"|"jurisprudencia"|
    "categorias"|"tipos_defecto"] (in place). `sentencia` es el texto de mayoría;
    `prompts`, los textos de los prompts que se cargaron para esta ficha.

    Devuelve {"ajustes": [str], "senales": [str]}: los ajustes van al log; las
    señales a validacion_problemas (no corrigen, marcan "Parcial - revisar").
    """
    secs = MATERIA.SECCIONES_CONTROLES["tribunal" if nivel == "tribunal" else "corte"]
    norm = sin_tildes(sentencia)
    ajustes, senales = [], []
    ext = _RE_ACOTACION.sub("", resultado.get("extraccion") or "")
    resumen = resultado.get("resumen") or ""

    unipersonal = nivel == "tribunal" and MATERIA.PRIMERA_INSTANCIA_UNIPERSONAL
    auto_unipersonal = nivel != "tribunal" and (
        "inadmite" in sin_tildes(_ultima_linea(_cuerpo(ext, secs.get("decision")) or ""))
        or bool(re.search(r"otros?\s+jueces?[^:\n]*:\s*\**\s*no aplica", sin_tildes(_cuerpo(ext, secs.get("jueces")) or ""))))
    ext = _quitar_otros_jueces(ext, secs.get("jueces"), siempre=unipersonal)

    ext, resumen = _llenar_voto(ext, resumen, secs, voto, unipersonal, auto_unipersonal, norm, ajustes, senales)
    if norm.strip() and "reconven" not in norm:
        ext, resumen = _sin_reconvencion(ext, resumen, secs, ajustes)
    if nivel == "apelacion":
        ext = _revoca_si_invierte(ext, secs, norm, ajustes)
    if nivel == "tribunal":
        ext, resumen = _posicion_demandada(ext, resumen, secs, norm, ajustes)
        ext = _cuantia_neutra(ext, secs.get("cuantia"), norm, ajustes)

    sin_suelta = _RE_NO_CONSTA_SUELTA.sub("", resumen).rstrip()
    if sin_suelta != resumen.rstrip():
        resumen = sin_suelta
        ajustes.append("Resumen: se quitó la frase suelta «No consta en la sentencia.»")
    maximo = MATERIA.MAX_PALABRAS_RESUMEN
    recortado = recortar_palabras(resumen, maximo)
    if recortado != resumen:
        ajustes.append(f"Resumen recortado a {maximo} palabras (oraciones intermedias)")
    if _contar(recortado) > maximo:
        senales.append(f"Revisar: el resumen tiene {_contar(recortado)} palabras (máximo {maximo}); "
                       "no se recortó para no perder el inicio ni la decisión")
    resultado["resumen"] = recortado
    if resultado.get("extraccion"):
        sin_h = sin_hora(ext)
        if sin_h != ext:
            ajustes.append("Fecha: se quitó la hora")
        resultado["extraccion"] = sin_h

    if norm.strip() and prompts:
        _datos_del_prompt(resultado, prompts, norm + " " + sin_tildes((voto or {}).get("texto")),
                          ajustes, senales)
    _jurisprudencia_sin_corchetes(resultado, ajustes, senales)
    if norm.strip():   # sin texto (modo visión) no hay contra qué comparar
        _jurisprudencia_sin_respaldo(resultado, norm + " " + sin_tildes((voto or {}).get("texto")),
                                     ajustes, senales)
        _compuertas(resultado, norm, ajustes)
        senales += _senales(resultado, secs, nivel, norm, sin_tildes((voto or {}).get("texto")))
        from resolucion.ai import fidelidad
        fidelidad.aplicar(resultado, sentencia + "\n" + ((voto or {}).get("texto") or ""), ajustes, senales)
    if nivel == "tribunal" and resultado.get("extraccion"):
        resultado["extraccion"] = regla_cero.pedidos_tipicos_no_pedidos(
            resultado["extraccion"], MATERIA.SECCIONES_CONTROLES["tribunal"].get("pretensiones"),
            sentencia, ajustes, senales)
    regla_cero.montos_sin_respaldo(resultado, (sentencia or "") + " " + ((voto or {}).get("texto") or ""),
                                   ajustes, senales)
    return {"ajustes": ajustes, "senales": senales}


def _llenar_voto(ext, resumen, secs, voto, unipersonal, auto_unipersonal, norm, ajustes, senales):
    n_voto, n_arg = secs.get("voto"), secs.get("voto_arg")
    if voto:
        lineas = []
        for nombre, tipo in voto["votos"]:
            lineas.append(nombre or "No consta en la sentencia el nombre del juez que emitió el voto.")
            if tipo == "concurrente":
                lineas.append("(voto concurrente)")
        ext = _reemplazar(ext, n_voto, "\n".join(lineas))
        ext = _reemplazar(ext, n_arg, extracto_voto(voto["texto"], MATERIA.MAX_PALABRAS_VOTO_SALVADO))
        ajustes.append(f"Voto separado de la mayoría: {'; '.join(n for n, _ in voto['votos'])}")
        if any(t == "salvado" for _, t in voto["votos"]):
            unanime = re.compile(r"por\s+unanimidad", re.I)
            ext, resumen = unanime.sub("por mayoría", ext), unanime.sub("por mayoría", resumen)
        return ext, resumen

    resumen = _quitar_oraciones(resumen, _RE_MENCION_VOTO)
    cuerpo = _cuerpo(ext, n_voto)
    if cuerpo is None:
        return ext, resumen
    if not _vacio(cuerpo):
        if norm.strip() and not unipersonal and not re.search(_RE_HAY_VOTO, norm):
            # Puerta civil v2: sin ninguna mención de voto salvado en la sentencia,
            # lo que el modelo puso ahí era el alegato de una parte o una cita.
            ext = _reemplazar(ext, n_voto, "No hubo voto salvado.")
            if n_arg:
                ext = _reemplazar(ext, n_arg, "No hubo voto salvado.")
            ajustes.append("Voto salvado: la sentencia no menciona ninguno; se quitó el contenido de la ficha")
        elif norm.strip():
            senales.append("Revisar: la ficha nombra un voto salvado y no se detectó su encabezado en la sentencia")
        return ext, resumen
    frase = ("No aplica (juez unipersonal)." if unipersonal
             else "No aplica (auto unipersonal)." if auto_unipersonal
             else "No hubo voto salvado.")
    ext = _reemplazar(ext, n_voto, frase)
    if _vacio(_cuerpo(ext, n_arg) or ""):
        ext = _reemplazar(ext, n_arg, frase)
    return ext, resumen


_RE_HAY_VOTO = r"voto (salvado|disidente|concurrente|de minoria)|salvo (mi|el) voto|salvamento de voto|disiento"

# Reconvención inexistente (puerta civil v2: 6 fichas inventaban una, o cortaban el
# tipo de acción en "con reconvención por"). Si la sentencia no usa la palabra, se
# quitan del texto de la ficha la cola del tipo de acción y los bloques rotulados.
_RE_COLA_RECONV = re.compile(r",?\s*con\s+reconvenci[oó]n(\s+por)?[^\n]*", re.I)
_RE_ROTULO_RECONV = re.compile(r"^\W*(?:pretensiones de la |excepciones a la |cuant[ií]a de la )?reconvenci[oó]n\s*:", re.I)
_RE_VINETA = re.compile(r"^\s*(?:[•●▪◦\-–*]|\d+[.)])\s+")


def _quitar_bloques_reconvencion(texto: str) -> tuple:
    lineas, fuera, dentro = texto.split("\n"), [], False
    quitadas = 0
    for l in lineas:
        if _RE_ROTULO_RECONV.match(l):
            dentro, quitadas = True, quitadas + 1
            continue
        if dentro and (not l.strip() or _RE_VINETA.match(l)) and not re.match(r"^\s*\d+\.\s+[A-ZÁÉÍÓÚ]", l):
            continue
        dentro = False
        fuera.append(l)
    return "\n".join(fuera), quitadas


def _sin_reconvencion(ext, resumen, secs, ajustes):
    tipo = _cuerpo(ext, secs.get("tipo_accion")) if secs.get("tipo_accion") else None
    if tipo and _RE_COLA_RECONV.search(tipo):
        ext = _reemplazar(ext, secs["tipo_accion"], _RE_COLA_RECONV.sub("", tipo).rstrip(" ,;"))
        ajustes.append("Tipo de acción: se quitó 'con reconvención', que la sentencia no menciona")
    ext, n = _quitar_bloques_reconvencion(ext)
    if n:
        ajustes.append(f"Se quitaron {n} bloques de reconvención: la sentencia no menciona ninguna")
    oraciones = re.split(r"(?<=[.!?])\s+", resumen)
    limpio = " ".join(o for o in oraciones if not re.search(r"reconven", o, re.I))
    if limpio != resumen.strip() and limpio:
        resumen = limpio
        ajustes.append("Resumen: se quitaron las oraciones sobre una reconvención que la sentencia no menciona")
    return ext, resumen


# Apelación que invierte el resultado de primera instancia (puerta civil v3,
# 01613-2020-00108: "ACEPTA el recurso y declara con lugar la demanda" sobre una
# demanda rechazada; la ficha decía "Reforma la sentencia."). Si la decisión de
# primera instancia y la parte resolutiva de la Sala dicen lo contrario sobre la
# demanda, la frase es "Revoca la sentencia.".
_RE_D_ACEPTA = re.compile(r"\b(?:acept\w*|con lugar)\b[^.;]{0,40}\bla (?:demanda|accion)\b|\bla demanda\b[^.;]{0,20}\bcon lugar\b")
_RE_D_RECHAZA = re.compile(r"\b(?:rechaz\w*|desech\w*|sin lugar|niega)\b[^.;]{0,40}\bla (?:demanda|accion)\b|\bla demanda\b[^.;]{0,20}\bsin lugar\b")


def _sentido(texto: str) -> str:
    a, r = _RE_D_ACEPTA.search(texto), _RE_D_RECHAZA.search(texto)
    if a and not r:
        return "acepta"
    if r and not a:
        return "rechaza"
    return ""


def _revoca_si_invierte(ext, secs, norm, ajustes):
    previa = sin_tildes(_cuerpo(ext, secs.get("decision_previa")) or "")
    decision = _cuerpo(ext, secs.get("decision")) or ""
    if not previa or not decision or sin_tildes(_ultima_linea(decision)).startswith("revoca"):
        return ext
    # La transcripción de la parte resolutiva en la propia ficha (sección de
    # decisión) es la fuente principal: el texto de la sentencia que llega aquí
    # puede venir recortado sin el "ADMINISTRANDO JUSTICIA" (piloto v4: el
    # control no se aplicó en el worker sobre 01613-2020-00108).
    resolutiva = sin_tildes(decision)
    if not _sentido(resolutiva):
        inicio = norm.find("administrando justicia")
        resolutiva = norm[inicio:inicio + 1500] if inicio >= 0 else ""
    antes, despues = _sentido(previa), _sentido(resolutiva)
    if antes and despues and antes != despues and "nulidad" not in resolutiva[:400]:
        lineas = decision.rstrip().split("\n")
        if re.match(r"(confirma|reforma|declara la nulidad)\b", sin_tildes(lineas[-1].strip(" *•"))):
            lineas[-1] = "Revoca la sentencia."
        else:  # la última línea es la transcripción: se conserva y se agrega la frase
            lineas.append("Revoca la sentencia.")
        ext = _reemplazar(ext, secs["decision"], "\n".join(lineas))
        ajustes.append(f"Decisión de apelación: primera instancia {antes} la demanda y la Sala {despues}; se cambió a 'Revoca la sentencia.'")
    return ext


# ── 4. Posición de la parte demandada ────────────────────────────────────────

_NORMATIVA_VACIA = "NORMATIVA APLICADA:\nNo consta normativa aplicada atribuible a esta parte en la sentencia."


def _no_contesto_demandada(norm):
    """Primera frase de rebeldía o falta de contestación cuyo sujeto es la parte
    demandada. None si la sentencia recoge una contestación o excepciones de la
    demandada, o si la frase habla de un tercero (la Procuraduría que "no ha
    comparecido a proceso")."""
    if re.search(MATERIA.RE_CONTESTO_DEMANDADA, norm):
        return None
    for m in re.finditer(MATERIA.RE_NO_CONTESTO, norm):
        sujeto = re.split(r"[.;:]", norm[max(0, m.start() - 150):m.start()])[-1]
        if not re.search(MATERIA.RE_SUJETO_TERCERO, sujeto):
            return m
    return None


def _posicion_demandada(ext, resumen, secs, norm, ajustes):
    no_contesto = _no_contesto_demandada(norm)
    negativa = re.search(MATERIA.RE_NEGATIVA_PURA, norm)
    if not (no_contesto or negativa):
        return ext, resumen
    if no_contesto:   # tiene prioridad sobre la negativa pura y simple
        texto = MATERIA.TEXTO_NO_CONTESTO
        if negativa and "negativa" not in texto:
            texto += " Su falta de contestación se tiene como negativa pura y simple."
        if re.search(MATERIA.RE_NO_COMPARECIO_AUDIENCIA, norm):
            texto += " " + MATERIA.TEXTO_NO_COMPARECIO
        if "por negada" not in norm:
            negada = re.compile(r"se\s+(tuvo|tiene)\s+por\s+negada\s+la\s+demanda", re.I)
            ext = negada.sub(r"se \1 como negativa de los fundamentos de hecho y de derecho", ext)
            resumen = negada.sub(r"se \1 como negativa de los fundamentos de hecho y de derecho", resumen)
    else:
        texto = MATERIA.TEXTO_NEGATIVA_PURA
    # Si la sentencia sí recoge argumentos propios de la demandada, se respeta
    # lo extraído salvo que el modelo haya dejado la sección vacía.
    atribuye = re.search(MATERIA.RE_ATRIBUCION_DEMANDADA, norm)
    for clave, nuevo in (("excepciones", f"• {texto}"), ("arg_demandado", f"• {texto}\n\n{_NORMATIVA_VACIA}")):
        cuerpo = _cuerpo(ext, secs.get(clave))
        if cuerpo is not None and (not atribuye or _vacio(cuerpo)) and cuerpo.strip() != nuevo:
            ext = _reemplazar(ext, secs[clave], nuevo)
            ajustes.append(f"Sección {secs[clave]}: texto estándar de la demandada")
    return ext, resumen


# ── 5. Cuantía ────────────────────────────────────────────────────────────────

_RE_VINETA_INICIAL = re.compile(r"^\s*(?:[•●\-–*]\s*)*")
# Lo que puede ir antes de la fórmula para que siga siendo "inicial" ("La", "El valor de la").
_RE_PREFIJO_FORMULA = re.compile(r"^\s*(?:(?:el|la|los|las|un|una|de|del|valor|monto|total|cuantia)\s+)*$")
# Conector entre la fórmula y el valor ("se fija en", "asciende a", ":").
_RE_CONECTOR_CUANTIA = re.compile(r"(?:se\s+)?fij\w*\s+en|asciende\s+a|ascendi\w*\s+a|es\s+de|:")
_RE_INICIO_VALOR = re.compile(r"\d|usd|us\$|\$|dolar")
_RE_SUMA_DE = re.compile(r"\s*(?:(?:la|el)\s+)?(?:suma|cantidad|valor|monto)\s+de\b|\s*:")


def _sin_tildes_alineado(texto: str) -> str:
    """sin_tildes() carácter por carácter: mismo largo que el original, para
    cortar el original con las posiciones halladas en la versión normalizada."""
    return "".join((sin_tildes(c) or c)[:1] for c in texto)


# Monto: "34.164,36", "18 653,62", "3,365.28", "96.246.00", "1770".
_RE_MONTO = re.compile(r"\d{1,3}(?:[., ]\d{3})+(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?")


def _entero(monto: str) -> str:
    """Parte entera del monto, solo dígitos: "34.164,36" y "34164.36" → "34164"."""
    return re.sub(r"\D", "", re.sub(r"[.,]\d{2}$", "", monto))


def _rotulo_cuantia(resto, norm, detalle):
    """Rótulo para el monto de la línea, con su concepto: "Cuantía fijada en la
    demanda:" si la sentencia lo llama cuantía (en el COGEP la cuantía la fija
    el actor en la demanda: "fijando la cuantía en", "la cuantía la fija en"); si no, la
    etiqueta de la viñeta del detalle (sección 8) que lleva el mismo monto; si
    no hay ninguna, el rótulo neutro."""
    m = _RE_MONTO.search(resto)
    entero = _entero(m.group(0)) if m else ""
    if len(entero) < 3:
        return MATERIA.ROTULO_CUANTIA_NEUTRO
    for x in _RE_MONTO.finditer(norm):
        antes = norm[max(0, x.start() - 100):x.start()]
        if _entero(x.group(0)) == entero and "cuantia" in antes:
            return MATERIA.ROTULO_CUANTIA_DEMANDA
    for linea in (detalle or "").split("\n"):
        et = re.match(r"^[\s•●\-–*]*([^:\n\d]{3,80}):\s*(.+)$", linea)
        if et and any(_entero(x) == entero for x in _RE_MONTO.findall(et.group(2))):
            return et.group(1).strip() + ":"
    return MATERIA.ROTULO_CUANTIA_NEUTRO


def _cuantia_neutra(ext, n, norm, ajustes):
    """Si la línea empieza con una fórmula ("La cuantía se fija en", "asciende
    a") que la sentencia no usa, se reemplaza solo esa fórmula por un rótulo
    con el concepto del monto (_rotulo_cuantia) y se conserva el resto de la
    línea tal cual: el monto no se extrae ni se reescribe (así no se corta
    "18 653,62 USD" ni "USD. 620.229,41")."""
    cuerpo = _cuerpo(ext, n)
    if not cuerpo:
        return ext
    sig = _ubicar(ext, n + 1)
    detalle = sig[3] if sig and "detalle" in sin_tildes(sig[2]) else ""
    lineas = cuerpo.split("\n")
    for i, linea in enumerate(lineas):
        ln = _sin_tildes_alineado(linea)
        ini = _RE_VINETA_INICIAL.match(linea).end()
        for patron in MATERIA.FORMULAS_CUANTIA:
            m = re.search(patron, ln)
            if not m or re.search(patron, norm):
                continue
            if not _RE_PREFIJO_FORMULA.match(ln[ini:m.start()]):
                break   # la fórmula no es inicial: la línea queda igual
            cola = ln[m.end():]
            valor = _RE_INICIO_VALOR.search(cola)
            conectores = list(_RE_CONECTOR_CUANTIA.finditer(cola[:valor.start() if valor else len(cola)]))
            fin = m.end() + (conectores[-1].end() if conectores else 0)
            suma = _RE_SUMA_DE.match(ln, fin)
            resto = linea[suma.end() if suma else fin:].strip()
            if not resto:
                break
            lineas[i] = f"{linea[:ini]}{_rotulo_cuantia(resto, norm, detalle)} {resto}"
            ajustes.append(f"Cuantía: fórmula «{patron}» no está en la sentencia")
            break
    nuevo = "\n".join(lineas)
    return ext if nuevo == cuerpo else _reemplazar(ext, n, nuevo)


# ── Jurisprudencia: precedentes cuyo número no está en la sentencia ──────────

# Número de resolución, juicio o sentencia: "232-2002", "17711-2014-0352", "1158-17-EP/21".
_GUIONES = "-\u00ad\u2010\u2011\u2012\u2013\u2014/"   # el PDF de SATJE usa el guion suave (U+00AD)
_RE_NUM_PRECEDENTE = re.compile(rf"\b\d{{1,6}}(?:[ \t]*[{_GUIONES}][ \t]*(?:\d{{1,6}}|[a-z]{{2,3}})\b)+", re.I)
_RE_VINETA_JURIS = re.compile(r"^\s*[•●]")


def _canon_numero(num: str) -> str:
    return "-".join(p.lstrip("0") or "0" for p in re.split(rf"\s*[{_GUIONES}]\s*", num.strip().upper()))


def _jurisprudencia_sin_respaldo(resultado, norm, ajustes, senales):
    """Quita de la sección 1 de la jurisprudencia las viñetas cuya identificación
    cita números de resolución, juicio o sentencia y ninguno aparece en la
    sentencia (p. ej. los del ejemplo del prompt 3 copiados por el modelo)."""
    juris = resultado.get("jurisprudencia") or ""
    s = _ubicar(juris, 1)
    if not s:
        return
    en_sentencia = " " + " ".join(_canon_numero(x) for x in _RE_NUM_PRECEDENTE.findall(norm)) + " "

    def _consta(num):
        return re.search(rf"[\s-]{re.escape(num)}[\s-]", en_sentencia)

    quedan, quitados = [], []
    for bloque in re.split(r"(?m)^(?=[ \t]*[•●])", s[3]):
        ident = bloque.strip().split("\n", 1)[0]
        nums = [_canon_numero(x) for x in _RE_NUM_PRECEDENTE.findall(sin_tildes(ident))]
        if _RE_VINETA_JURIS.match(bloque) and nums and not any(_consta(n) for n in nums):
            quitados.append(ident.strip(" •●\t*"))
        else:
            quedan.append(bloque)
    if not quitados:
        return
    nuevo = "".join(quedan).strip()
    if not re.search(r"[•●]", nuevo):
        nuevo = "No consta en la sentencia."
    resultado["jurisprudencia"] = _reemplazar(juris, 1, nuevo)
    for q in quitados:
        ajustes.append(f"Jurisprudencia: se quitó «{q}» (su número no está en la sentencia)")
        senales.append(f"Revisar: se quitó de la jurisprudencia «{q}» porque su número no aparece en la sentencia")


# ── Datos copiados de los ejemplos de los prompts ────────────────────────────

# Nombre propio u órgano: dos o más palabras con mayúscula inicial, con partículas
# entre ellas ("Carlos Enrique Ocaña Palacios", "Primera Sala de lo Civil y Mercantil
# de la Corte Suprema de Justicia").
_RE_FRASE_PROPIA = re.compile(r"\b[A-ZÁÉÍÓÚÑ]\w*(?:[ \t]+(?:(?:de|del|la|las|los|lo|y|e)[ \t]+)*[A-ZÁÉÍÓÚÑ]\w*)+")
# Rótulo o título del prompt ("Juez Ponente:", "11. Decisión Final", "Normativa
# Aplicada" en su propia línea): es la estructura de la ficha, no un dato.
_RE_TRAS_ROTULO = re.compile(r"[ \t]*(?:\([^)\n]{0,40}\))?[ \t]*:")
_RE_ANTES_TITULO = re.compile(r"(?:^|\n)[ \t]*\d+\.[ \t]*$")
_RE_ANTES_LINEA = re.compile(r"(?:^|\n)[ \t]*$")
_RE_TRAS_LINEA = re.compile(r"[ \t]*(?:\n|$)")
# Número de 3 o más cifras ("289", "39.672,37" → 672) o marcador de ejemplo ("NNN-AAAA").
_RE_ID_PROMPT = re.compile(r"\b(?:\d{3,}|N{3,}|A{4})\b")
# Prefijo que se conserva al vaciar una línea: viñeta y rótulo ("• Abogado del Actor: ").
_RE_PREFIJO_LINEA = re.compile(r"^([ \t]*(?:[•●\-–*][ \t]*)*)((?:[^:\n“\"«]{1,50}:[ \t]*)?)")
_VENTANA_FRASE = 400
_RAIZ = 6


def _sin_ceros(numero: str) -> str:
    return numero.lstrip("0") or "0"


def _significativas(frase: str) -> list:
    return [p for p in frase.split() if len(p) > 2 and p not in _PARTICULAS]


def _frase_en(frase: str, norm: str) -> bool:
    """La frase consta en la sentencia si la raíz (6 letras) de cada palabra
    significativa aparece a menos de _VENTANA_FRASE letras de las demás: la
    sentencia puede decir "CORTE NACIONAL DE JUSTICIA. SALA ESPECIALIZADA DE LO
    CIVIL", "Procurador General" o "Estados Unidos de Norteamérica"."""
    raices = [p[:_RAIZ] for p in _significativas(frase)]
    if not all(r in norm for r in raices):
        return False
    rara = min(raices, key=norm.count)
    for m in re.finditer(re.escape(rara), norm):
        ventana = norm[max(0, m.start() - _VENTANA_FRASE):m.end() + _VENTANA_FRASE]
        if all(r in ventana for r in raices):
            return True
    return False


def _datos_del_prompt(resultado, prompts, norm, ajustes, senales):
    """Quita del resumen (oración) y de la extracción y la jurisprudencia (línea)
    los nombres propios, órganos, números y marcadores ("NNN-AAAA") que están en
    el texto de los prompts cargados y no en la sentencia: el modelo los copió
    de un ejemplo. Una línea con rótulo ("• Abogado del Actor:") o de la
    jurisprudencia (identificación, órgano, fecha y cita) conserva su lugar con
    "No consta en la sentencia"; las demás líneas se quitan."""
    texto = "\n".join(p for p in prompts if p)
    frases, rotulos = set(), set()
    for m in _RE_FRASE_PROPIA.finditer(texto):
        antes = texto[max(0, m.start() - 12):m.start()]
        f = re.sub(r"\s+", " ", sin_tildes(m.group(0)))
        n = len(_significativas(f))
        # Una línea sola de hasta tres palabras es un título; un órgano es más largo.
        if (_RE_TRAS_ROTULO.match(texto, m.end()) or _RE_ANTES_TITULO.search(antes)
                or (n <= 3 and _RE_ANTES_LINEA.search(antes) and _RE_TRAS_LINEA.match(texto, m.end()))):
            rotulos.add(f)   # si una vez es rótulo ("NORMATIVA APLICADA:"), nunca es un dato
        elif n >= 2:
            frases.add(f)
    frases -= rotulos
    re_frases = re.compile(r"\b(?:" + "|".join(re.escape(f).replace(r"\ ", r"\s+")
                                                for f in sorted(frases, key=len, reverse=True)) + r")\b",
                           re.I) if frases else None
    ids = {_sin_ceros(x) for x in _RE_ID_PROMPT.findall(texto)}
    numeros = {_sin_ceros(x) for x in re.findall(r"\d+", norm)}
    consta = {}

    def _ajeno(t):
        for x in _RE_ID_PROMPT.findall(t):
            if _sin_ceros(x) in ids and _sin_ceros(x) not in numeros:
                return x
        # Con la mayúscula de la ficha: "Corte Suprema" es un órgano; "voto salvado", no.
        plano = "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")
        for m in (re_frases.finditer(plano) if re_frases else ()):
            frase = re.sub(r"\s+", " ", m.group(0).lower())
            if not m.group(0)[0].isupper():
                continue
            if frase not in consta:
                consta[frase] = _frase_en(frase, norm)
            if not consta[frase]:
                return t[m.start():m.end()] if len(plano) == len(t) else m.group(0)
        return None

    quitados = []

    def _lineas(texto_campo, rellenar):
        salida, seccion = [], []

        def _cerrar():
            if None in seccion and not any(l.strip() for l in seccion if l is not None):
                seccion[:] = ["No consta en la sentencia."]
            salida.extend(l for l in seccion if l is not None)
            seccion.clear()

        for linea in texto_campo.split("\n"):
            if _RE_ENCABEZADO.match(linea):
                _cerrar()
                salida.append(linea)
                continue
            vineta, rotulo = _RE_PREFIJO_LINEA.match(linea).groups()
            dato = _ajeno(linea[len(vineta) + len(rotulo):])
            if not dato:
                seccion.append(linea)
                continue
            quitados.append((dato, linea.strip()))
            seccion.append(f"{vineta}{rotulo}No consta en la sentencia" if rotulo or rellenar else None)
        _cerrar()
        return "\n".join(salida)

    resumen = resultado.get("resumen") or ""
    parrafos = []
    for p in re.split(r"\n\s*\n", resumen):
        quedan = []
        for o in _oraciones(p):
            dato = _ajeno(o)
            if dato:
                quitados.append((dato, o))
            else:
                quedan.append(o)
        if quedan:
            parrafos.append(" ".join(quedan))
    if quitados:
        resultado["resumen"] = "\n\n".join(parrafos)
    if resultado.get("extraccion"):
        resultado["extraccion"] = _lineas(resultado["extraccion"], rellenar=False)
    juris = resultado.get("jurisprudencia") or ""
    s = _ubicar(juris, 1)
    nuevo = _lineas(s[3], rellenar=True) if s else None
    if s and nuevo != s[3]:
        resultado["jurisprudencia"] = _reemplazar(juris, 1, nuevo)
    for dato, linea in quitados:
        ajustes.append(f"Dato del ejemplo del prompt «{dato}»: se quitó «{linea[:120]}»")
        senales.append(f"Revisar: la ficha copiaba «{dato}» del ejemplo del prompt y la sentencia no lo menciona")


# Marcador entre corchetes que imita el ejemplo del prompt ("Resolución No. [No. 6]",
# "Fecha: [fecha]"); se respetan "[…]", "[...]", "[sic]" y "[número de identificación omitido]".
# "[27]" (nota al pie) tampoco es un marcador: el marcador lleva letras.
_RE_CORCHETE = re.compile(r"\[(?!\s*(?:…|\.\.\.|sic\b|[^\]\n]*omitid))(?=[^\]\n]*[^\W\d_])([^\]\n]{1,80})\]", re.I)


def _jurisprudencia_sin_corchetes(resultado, ajustes, senales):
    """Sección 1 de la jurisprudencia, líneas de identificación, órgano y fecha:
    un marcador con datos ("[No. 6]") se desenvuelve; uno sin datos ("[fecha]")
    pasa a "No consta en la sentencia". Las líneas con comillas (el fragmento
    citado) no se tocan: ahí "[énfasis añadido]" es del texto original."""
    juris = resultado.get("jurisprudencia") or ""
    s = _ubicar(juris, 1)
    if not s:
        return

    def _linea(l):
        if re.search(r"[“”\"«»]", l):
            return l
        l = _RE_CORCHETE.sub(lambda m: m.group(1).strip() if re.search(r"\d", m.group(1))
                             else "No consta en la sentencia", l)
        return re.sub(r"\bNo\.\s+No\.", "No.", l)

    nuevo = "\n".join(_linea(l) for l in s[3].split("\n"))
    if nuevo == s[3]:
        return
    resultado["jurisprudencia"] = _reemplazar(juris, 1, nuevo)
    ajustes.append("Jurisprudencia: se quitaron marcadores entre corchetes")
    senales.append("Revisar: la jurisprudencia tenía marcadores entre corchetes copiados del ejemplo del prompt")


# ── 3. Compuertas por palabra clave ──────────────────────────────────────────

_RE_CATEGORIA = re.compile(r"^\W*Categor[ií]a\s*\[?(\d+)", re.I)
_RE_SUBCATEGORIA = re.compile(r"^\W*Subcategor[ií]a", re.I)


def _nombre_sub(linea: str) -> str:
    return sin_tildes(linea.split(":", 1)[1] if ":" in linea else "").strip(" .*")


def _compuertas(resultado, norm, ajustes):
    """Quita categorías (número) o subcategorías (nombre) y códigos del prompt 7:
    COMPUERTAS_PALABRA_CLAVE cuando la palabra clave NO está en la sentencia;
    COMPUERTAS_EXCLUYENTES (opcional) cuando SÍ está y no aparece la salvedad."""
    codigos, categorias = set(), set()
    for cods, cats, patron in MATERIA.COMPUERTAS_PALABRA_CLAVE:
        if not re.search(patron, norm, re.S):
            codigos |= cods
            categorias |= cats
    for cods, cats, patron, salvo in getattr(MATERIA, "COMPUERTAS_EXCLUYENTES", []):
        if re.search(patron, norm, re.S) and not (salvo and re.search(salvo, norm, re.S)):
            codigos |= cods
            categorias |= cats
    subcategorias = {sin_tildes(c) for c in categorias if isinstance(c, str)}

    lineas, saltando, quitadas = [], False, []
    for linea in (resultado.get("categorias") or "").split("\n"):
        m = _RE_CATEGORIA.match(linea)
        if m:
            saltando = int(m.group(1)) in categorias
            if saltando:
                quitadas.append(linea.strip())
        elif _RE_SUBCATEGORIA.match(linea):
            if not saltando and _nombre_sub(linea) in subcategorias:
                quitadas.append(linea.strip(" •*"))
                continue
        else:
            saltando = False
        if not saltando:
            lineas.append(linea)
    if quitadas:
        resultado["categorias"] = "\n".join(lineas)
        ajustes.append("Categorías sin palabra clave en la sentencia: " + "; ".join(quitadas))

    tipos = resultado.get("tipos_defecto") or ""
    fuera = [l for l in tipos.split("\n") if (l.strip().strip("-•* ").split() or [""])[0].upper() in codigos]
    if fuera:
        resultado["tipos_defecto"] = "\n".join(l for l in tipos.split("\n") if l not in fuera)
        ajustes.append("Puntos controvertidos sin palabra clave en la sentencia: " + "; ".join(l.strip() for l in fuera))


# ── 6. Señales "revisar" ─────────────────────────────────────────────────────

_RE_NIEGA = re.compile(r"\b(se niega|niegan?|negad[oa]s?|sin lugar|no ha lugar|improcedente|se rechaza|rechazan?)\b")
_RE_FECHA_RO = re.compile(r"^\W*fecha\s*:\s*(r\.?\s*o\.?(\s|\b)|registro oficial)", re.I | re.M)


def _senales(resultado, secs, nivel, norm, norm_voto) -> list:
    senales = []
    ext = resultado.get("extraccion") or ""

    if nivel == "tribunal":
        decision = sin_tildes(_ultima_linea(_cuerpo(ext, secs.get("decision")) or ""))
        if decision.startswith("acepta la demanda"):
            inicio = max(norm.rfind("administrando justicia"), norm.rfind("resuelve"))
            resolutiva = norm[inicio:inicio + 4000] if inicio >= 0 else ""
            for m in _RE_NIEGA.finditer(resolutiva):
                # Solo la cláusula del verbo: hasta el punto o punto y coma vecino.
                antes = re.split(r"[.;]", resolutiva[max(0, m.start() - 60):m.start()])[-1]
                despues = re.split(r"[.;]", resolutiva[m.end():m.end() + 150])[0]
                ventana = antes + m.group(0) + despues
                if not re.search(MATERIA.NEGATIVAS_QUE_NO_CUENTAN, ventana):
                    senales.append("Revisar: la decisión es 'Acepta la demanda' pero la parte resolutiva niega algún rubro")
                    break

    ficha = sin_tildes((resultado.get("resumen") or "") + "\n" + ext)
    for patron in MATERIA.NOMBRES_VIGILADOS:
        m = re.search(patron, ficha)
        if m and not re.search(patron, norm + " " + norm_voto):
            senales.append(f"Revisar: la ficha usa «{m.group(0)}» y la sentencia no lo menciona")

    # Señales por patrón (opcional): (regex sobre una sección de la ficha, regex
    # que la sentencia contiene, regex que no contiene o None, mensaje).
    for clave, en_ficha, presente, ausente, mensaje in getattr(MATERIA, "SENALES_SECCION", []):
        cuerpo = sin_tildes(_cuerpo(ext, secs.get(clave)) or "")
        if (cuerpo and re.search(en_ficha, cuerpo) and re.search(presente, norm)
                and not (ausente and re.search(ausente, norm))):
            senales.append(mensaje)

    n_ro = len(_RE_FECHA_RO.findall(resultado.get("jurisprudencia") or ""))
    if n_ro:
        senales.append(f"Revisar: {n_ro} precedente(s) con fecha del Registro Oficial y sin fecha del fallo")
    return senales
