"""
Clasificación y normalización de actores (demandantes) en causas tributarias.

Clasifica el campo Actor como 'empresa' o 'persona_natural' usando reglas léxicas —
sin IA, con cobertura >99% sobre datos reales del SATJE/Strapi.
"""

import re


# ── Sufijos corporativos ───────────────────────────────────────────────────────
# Busca sufijos jurídicos en cualquier parte del string.
# Se usa search() no match(). Orden: más específico primero.

_SUFIJOS = (
    # Ecuador / Latinoamérica
    r"S\.A\.S\.", r"S\.A\.", r"S\.A\b",
    r"C\.A\.", r"C\.A\b",
    r"Cía\.?\s*Ltda\.?", r"CIA\.?\s*LTDA\.?",
    r"C\.L\.", r"C\.L\b",
    r"C\.E\.M\.", r"C\.E\.M\b",
    r"S\.C\.C\.", r"S\.C\.C\b",
    r"S\.C\.", r"S\.C\b",
    r"Ltda\.", r"LTDA\b",
    r"S\.C\.S\.",
    # Extranjeros frecuentes
    r"L\.L\.C\.", r"LLC\b",
    r"S\.R\.L\.", r"SRL\b",
    r"B\.V\.",
    r"GMBH\b", r"GmbH\b",
    r"\bAG\b", r"A\.G\.",
    r"\bPLC\b", r"P\.L\.C\.",
    r"Limited\b", r"\bLtd\.", r"\bLTD\b",
    r"\bInc\.", r"\bINC\b",
    r"\bCorp\.", r"\bCORP\b",
    r"N\.V\.",
    r"S\.\s*de\s*R\.L\.",   # México: S. de R.L. de C.V.
    r"R\.L\.\s*de\s*C\.V\.",
    r"\bE\.P\.", r"\bE\.P\b", r"\bEP\b",
    r"\bB\.V\b",
    # Variantes SATJE raras
    r"\bC\.AGIF\b", r"\bAGIF\b",
    # Sufijos de países anglófonos
    r"\bCo\.\s*Ltd\.", r"\bCo\.\s*LTD\.",
    r"\bPte\.\s*Ltd\.", r"\bPTE\s+LTD\b",
    r"\bBV\b",
    # Banco / Sucursal extranjera
    r"Sucursal\s+Ecuador",
)
_RE_SUFIJO = re.compile("|".join(_SUFIJOS), re.IGNORECASE)

# Keywords de entidades que no tienen sufijo estándar
_RE_KEYWORD = re.compile(
    r"\bCompañ[ií]a\b|\bCorporaci[oó]n\b|\bSociedad\s+(An[oó]nima|Civil|Comercial)\b"
    r"|\bFideicomiso\b|\bConsorcio\b"
    r"|\bCooperativa\b"
    r"|\bBanco\s"
    r"|\bInstituto\s|\bFundaci[oó]n\s"
    r"|\bClub\s+(Social|Deportivo)"
    r"|\bEmpresa\s+(P[uú]blica|Privada)?"
    r"|\bGobierno\s|\bMunicipio\s|\bMunicipal\s"
    # Términos de empresa en inglés (subsidiarias/extranjeras): Sinohydro
    # Corporation, X Company, Y Incorporated, Z Holdings…
    r"|\bCorporation\b|\bCompany\b|\bIncorporated\b|\bHoldings?\b|\bEnterprises?\b",
    re.IGNORECASE,
)

# Patrón "en calidad de [rol] de la [compañía/empresa/…] NOMBRE"
_RE_REP_DE_EMPRESA = re.compile(
    r"calidad\s+de\s+(su\s+)?(Gerente|Representante\s+Legal|Apoderado|Presidente"
    r"|Liquidador|Director|Rector|Procurador\s+S[ií]ndic[ao])",
    re.IGNORECASE,
)

# Procurador que representa a entidad institucional (Junta, GAD, municipio, etc.)
_RE_PROCURADOR_ENTIDAD = re.compile(
    r"Procurador\s+(Alterno|S[ií]ndic[ao]|General).*(Junta|GAD|Municipio|Gobierno|Petro|IESS)",
    re.IGNORECASE | re.DOTALL,
)

# "por sus propios derechos" → persona natural explícita
_RE_PROPIOS = re.compile(
    r"por\s+sus\s+propios(\s+y\s+personales)?\s+derechos|por\s+derecho\s+propio",
    re.IGNORECASE,
)


# ── Normalización ─────────────────────────────────────────────────────────────

# Prefijos de representante: "[Persona], en calidad de [rol] de la compañía NOMBRE…"
_RE_EXTRAE_EMPRESA = re.compile(
    r"(?:compañ[ií]a|empresa|corporaci[oó]n|sociedad|banco|cooperativa"
    r"|fundaci[oó]n|fideicomiso|consorcio)\s+(.+)",
    re.IGNORECASE,
)

# Extrae la compañía en la estructura "[Persona] [rol] de la compañía NOMBRE"
# (con o sin "en calidad de"). El actor real es NOMBRE, no la persona que la
# representa. Es PRECISO a propósito: exige que un rol de representante preceda
# a "de la compañía/empresa/…", para NO recortar entidades cuyo propio nombre
# ya contiene "Consorcio/Cooperativa/Corporación/Compañía" (ej. "Consorcio
# Petrolero Bloque 16", "CNLC Ecuador Corporación S.A.").
_RE_REP_COMPANIA = re.compile(
    r"(?:en\s+(?:su\s+)?calidad\s+de\s+|como\s+)?"
    r"\b(?:gerentes?|representantes?\s+legal(?:es)?|apoderad[oa]s?"
    r"|president[ea]s?(?:\s+ejecutiv[oa]s?)?"
    r"|director(?:es|as|[ao])?(?:\s+(?:general(?:es)?|ejecutiv[oa]s?))?"
    r"|liquidador(?:es|as|[ao])?"
    r"|procurador(?:es|as|[ao])?\s+(?:judicial(?:es)?|s[ií]ndic[ao]s?))\b"
    r"[^,]*?\b(?:de\s+la|del|de\s+los|de\s+las)\s+"
    r"(?P<cont>compañ[ií]a|empresa|firma|sociedad|corporaci[oó]n|cooperativa"
    r"|fundaci[oó]n|fideicomiso|consorcio|club|banco|instituci[oó]n|organizaci[oó]n)"
    r"\s+(?P<resto>.+)",
    re.IGNORECASE,
)
# Contenedor genérico (no forma parte del nombre) → se descarta y el actor es lo
# que sigue. El resto (Cooperativa/Consorcio/Club/Banco…) SÍ es parte del nombre.
_RE_CONT_GENERICO = re.compile(r"^(?:compañ[ií]a|empresa|firma)$", re.IGNORECASE)

# Colapso SEGURO de siglas escritas con letras sueltas ("S. A." -> "S.A."): solo
# actúa si el compactado es una sigla conocida, para no dañar iniciales como
# "C.M.E." (cuyo compactado no está en el catálogo de siglas).
_RE_LETRAS_ESPACIADAS = re.compile(
    r"\b(?:[A-Za-zÁÉÍÓÚÑ]\.\s+){1,3}[A-Za-zÁÉÍÓÚÑ]\.(?![A-Za-zÁÉÍÓÚÑ])"
)


def _colapsar_siglas_espaciadas(s: str) -> str:
    def _sub(m):
        letras = re.findall(r"[A-Za-zÁÉÍÓÚÑ]", m.group(0))
        if "".join(letras).upper() in _SIGLAS_DOTLESS:
            return "".join(c.upper() + "." for c in letras)
        return m.group(0)
    return _RE_LETRAS_ESPACIADAS.sub(_sub, s)

# Trailing "representada por…" / "a través de…" / "en la persona de…"
_RE_TRAILING_REP = re.compile(
    r",?\s*(representad[ao]\s+por|a\s+trav[eé]s\s+de|quien\s+comparece"
    r"|a\s+su\s+vez|compareciendo|mediante\s+su|en\s+la\s+persona\s+de"
    r"|por\s+intermedio\s+de|por\s+medio\s+de).+$",
    re.IGNORECASE | re.DOTALL,
)

# "Compañía " al inicio como prefijo decorativo ("Compañía ALESSA S.A."), pero
# NO cuando forma parte del nombre ("Compañía de Transportes X").
_RE_COMPANIA_PREFIX = re.compile(r"^Compañ[ií]a\s+(?!de\s|del\s|de\s+la\s)", re.IGNORECASE)

# Trailing legal: "por sus propios derechos", "en calidad de…"
_RE_TRAILING_PERSON = re.compile(
    r",?\s*por\s+sus\s+propios(\s+y\s+personales)?\s+derechos\.?"
    r"|,?\s*por\s+derecho\s+propio\.?"
    r"|,?\s*en\s+su\s+calidad\s+de\s+.+"
    r"|,?\s*en\s+calidad\s+de\s+.+",
    re.IGNORECASE,
)

# Títulos profesionales al inicio
_RE_TITULOS = re.compile(
    r"^(Sr\.?\s*|Sra\.?\s*|Ing\.?\s*|Abg\.?\s*|Dr\.?\s*|Dra\.?\s*"
    r"|Eco\.?\s*|Lcdo\.?\s*|Lcda\.?\s*|Arq\.?\s*|CPA\.?\s*|Lic\.?\s*"
    r"|Ab\.?\s*|Señor\s+|Señora\s+)+",
    re.IGNORECASE,
)


# ── API pública ────────────────────────────────────────────────────────────────

def clasificar_tipo_actor(actor: str) -> str:
    """
    'empresa', 'persona_natural', o 'desconocido' (solo si vacío).
    """
    if not actor or not actor.strip():
        return "desconocido"
    s = actor.strip()

    if _RE_SUFIJO.search(s):
        return "empresa"
    if _RE_KEYWORD.search(s):
        return "empresa"
    if _RE_REP_DE_EMPRESA.search(s):
        return "empresa"
    if _RE_PROCURADOR_ENTIDAD.search(s):
        return "empresa"
    if _RE_PROPIOS.search(s):
        return "persona_natural"
    return "persona_natural"


def normalizar_nombre_actor(actor: str, tipo: str | None = None) -> str:
    """
    Nombre canónico del actor:
    - Empresa: extrae la entidad, quita rep. legal y trailing texto
    - Persona: quita títulos y frases legales
    """
    if not actor or not actor.strip():
        return ""
    if tipo is None:
        tipo = clasificar_tipo_actor(actor)

    # Basura de formato al inicio del nombre (viñetas/markdown/guiones que se
    # colaron desde una extracción defectuosa): nunca es parte del nombre.
    s = re.sub(r'^[\s•●○◦▪▸►·*\-#]+', '', actor).strip()
    s = _colapsar_siglas_espaciadas(s)

    if tipo == "empresa":
        # Caso A: "[Persona] [rol] de la compañía NOMBRE" → el actor es NOMBRE.
        # Si el contenedor es genérico (compañía/empresa/firma) se descarta; si
        # es parte del nombre (Cooperativa/Consorcio/Club…) se conserva.
        m = _RE_REP_COMPANIA.search(s)
        if m:
            cont, resto = m.group("cont"), m.group("resto")
            # "compañía/empresa/firma" es genérico y se descarta SALVO que sea
            # parte del nombre ("Compañía de Transportes X", donde sigue "de").
            generico = bool(_RE_CONT_GENERICO.match(cont)) and not re.match(
                r"(?:de\s|del\s|de\s+la\s|de\s+los\s|de\s+las\s)", resto, re.IGNORECASE
            )
            s = resto if generico else f"{cont} {resto}"

        # Caso B: "Compañía NOMBRE S.A." (keyword como prefijo decorativo).
        s = _RE_COMPANIA_PREFIX.sub("", s)

        # Quitar cola de representación ("representada por…", "en la persona de…").
        s = _RE_TRAILING_REP.sub("", s)
        s = s.strip().rstrip(".").rstrip(",").strip()

    else:
        # Persona natural
        s = _RE_TITULOS.sub("", s)
        s = _RE_TRAILING_PERSON.sub("", s)
        s = s.strip().rstrip(".").rstrip(",").strip()

    # #3/#12: devolver con mayúsculas/minúsculas correctas, para que la
    # denominación canónica sea consistente tanto en la extracción en vivo
    # como en el backfill de re-normalización.
    return normalizar_case_nombre(s)


# ── Normalización de mayúsculas en nombres propios (#3) ─────────────────────────
# SATJE entrega los nombres ÍNTEGRAMENTE en mayúsculas ("OTECEL S.A.",
# "SERVICIO DE RENTAS INTERNAS"). El requerimiento es no presentarlos así, sino
# con mayúsculas/minúsculas correctas, respetando partículas ("de", "la") y
# siglas/sufijos jurídicos que sí van en mayúscula (S.A., SRI, GAD…).

_PARTICULAS_MINUSCULA = {
    "de", "del", "la", "las", "los", "y", "e", "o", "u",
    "en", "a", "al", "con", "por", "para",
}

# Siglas y sufijos que deben conservarse en mayúsculas aunque el nombre se
# recapitalice. Se comparan sin distinguir mayúsculas y sin el paréntesis.
_SIGLAS_MAYUSCULA = {
    "S.A.", "S.A.S.", "C.A.", "C.L.", "C.E.M.", "S.C.", "S.C.C.", "S.C.S.",
    "CIA.", "CÍA.", "LTDA.", "S.R.L.", "SRL", "LLC", "L.L.C.", "B.V.", "BV",
    "N.V.", "AG", "A.G.", "PLC", "P.L.C.", "INC", "INC.", "CORP", "CORP.",
    "LTD", "LTD.", "E.P.", "EP", "GMBH", "C.V.", "CV", "R.L.", "RL",
    "SRI", "SENAE", "GAD", "IESS", "IESS", "ARCOTEL", "CNT", "EMOV",
    "EMAPA", "EPMAPS", "SERCOP", "ANT", "MSP", "MTOP",
}

# Versión sin puntos para comparar tokens de forma robusta: "S.A.", "S.A" y
# "SA" deben reconocerse todos como la misma sigla (el paso previo de
# normalización a veces recorta el punto final del sufijo).
_SIGLAS_DOTLESS = {"".join(c for c in s if c.isalpha()).upper() for s in _SIGLAS_MAYUSCULA}


def _capitalizar_token(p: str) -> str:
    """Pone en mayúscula la primera LETRA del token (no el primer carácter),
    para no romper marcas que empiezan con dígito: '3m' -> '3M', 'otecel' -> 'Otecel'."""
    for i, ch in enumerate(p):
        if ch.isalpha():
            return p[:i] + ch.upper() + p[i + 1:]
    return p


def normalizar_case_nombre(nombre: str) -> str:
    """
    Recapitaliza un nombre propio a Title Case, palabra por palabra:
      · siglas conocidas (S.A., SRI, GAD…) → MAYÚSCULA,
      · partículas ("de", "la"…) que no abren el nombre → minúscula,
      · palabras en TODO-mayúsculas o TODO-minúsculas → capitalizadas,
      · palabras con case ya mixto intencional ("McDonald") → intactas.
    """
    if not nombre:
        return nombre
    s = nombre.strip()
    palabras = s.split()
    out = []
    for i, w in enumerate(palabras):
        nucleo = w.strip("()[].,;:")
        nucleo_dotless = "".join(c for c in nucleo if c.isalpha()).upper()
        # Siglas conocidas (con o sin puntos/paréntesis) → siempre en mayúscula.
        if nucleo_dotless and nucleo_dotless in _SIGLAS_DOTLESS:
            out.append(w.upper())
            continue
        low = w.lower()
        # Partículas en minúscula, salvo si abren el nombre.
        if i > 0 and low in _PARTICULAS_MINUSCULA:
            out.append(low)
            continue
        # Respetar palabras ya con case mixto intencional (ej. "McDonald").
        letras_w = [c for c in w if c.isalpha()]
        if letras_w and any(c.isupper() for c in letras_w) and any(c.islower() for c in letras_w):
            out.append(w)
            continue
        # TODO-mayúsculas o TODO-minúsculas → capitalizar respetando guiones
        # internos ("San-José") y marcas con dígito ("3M" no debe volverse "3m").
        out.append("-".join(_capitalizar_token(p) for p in low.split("-")))
    return " ".join(out)
