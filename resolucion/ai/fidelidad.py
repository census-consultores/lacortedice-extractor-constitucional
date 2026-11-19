"""Verificación de fidelidad: nada de la ficha puede afirmar algo que no esté en la
sentencia (decisión de John, piloto civil v2: "que todo sea real al 100 %").

La IA tiene la regla en los prompts; este módulo la hace cumplir sobre lo que se
puede comprobar mecánicamente contra el texto de la sentencia:

- Citas entre comillas (≥ 8 palabras): deben aparecer literalmente. Si no, se
  retiran y se deja un aviso.
- Montos (USD, US$, $): deben existir en la sentencia (tolerando separadores y
  los errores típicos del OCR). Si no, se marcan "[no verificado en la sentencia]".
- Números de juicio (NNNNN-AAAA-NNNNN): deben existir. Si no, se marcan.

Lo que no se puede comprobar así (paráfrasis de argumentos) queda en manos de la
regla de origen del prompt y de la revisión de abogados.
"""
import re
import unicodedata

AVISO_CITA = "[Cita retirada: no aparece literalmente en la sentencia]"
MARCA = " [no verificado en la sentencia]"
# Solo comillas tipográficas: las rectas aparecen sueltas en nombres ("Assets") y
# capturarían texto que no es cita.
_RE_CITA = re.compile(r"[“«]([^”»\n]{40,}?)[”»]")
# Referencia bibliográfica entre comillas (autor, obra, editorial, página): se
# comprueba que el autor y la obra estén en la sentencia, no la puntuación exacta.
_RE_REFERENCIA = re.compile(r"\b(?:p[aá]gs?\.|p\.\s*\d|editorial|edici[oó]n|tomo|ediar|temis|\b(?:19|20)\d{2}\b)", re.I)
_RE_MONTO = re.compile(r"(?:USD|US\$|\$)\s?\$?\.?\s?(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{2})?)(?!\d)")
_RE_JUICIO = re.compile(r"\b(\d{5}-\d{4}-\d{3,5}[A-Z]?)\b")
UMBRAL_CITA = 0.6
ACTIVA = True   # los tests de otros controles con sentencias sintéticas la apagan (tests/conftest.py)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def _digitos(s: str) -> str:
    """Solo dígitos, con los errores de OCR más comunes corregidos (o→0, l→1)."""
    # También "36,382.1 O": OCR con espacio antes de la O (puerta v8, 17230-2017-07411).
    s = re.sub(r"(?<=\d)[oO](?=[\d.,oO])|(?<=[\d.,])[oO]|(?<=\d)\s[oO]\b", "0", s or "")
    return re.sub(r"\D", "", s)


def cita_presente(cita: str, sent_norm: str) -> bool:
    if _RE_REFERENCIA.search(cita) and len(cita) < 220:
        claves = [p for p in _norm(cita).split() if len(p) > 3 and not p.isdigit()]
        return bool(claves) and sum(p in sent_norm for p in claves) / len(claves) >= 0.7
    w = _norm(cita).split()
    if len(w) < 8:
        return True   # demasiado corta para juzgar; no se toca
    ventanas = [" ".join(w[i:i + 8]) for i in range(0, len(w) - 7, 4)]
    return sum(v in sent_norm for v in ventanas) / len(ventanas) >= UMBRAL_CITA


def monto_presente(monto: str, sent_dig: str) -> bool:
    d = _digitos(monto)
    if len(d) < 3:
        return True
    # Con o sin centavos (la sentencia puede escribir "USD 4.300" y la ficha "4.300,00").
    return d in sent_dig or (d.endswith("00") and d[:-2] in sent_dig)


def verificar(texto: str, sentencia: str, campo: str) -> tuple:
    """Devuelve (texto corregido, [señales])."""
    if not texto or not sentencia:
        return texto, []
    sent_norm, sent_dig = _norm(sentencia), _digitos(sentencia)
    senales = []

    def _cita(m):
        if cita_presente(m.group(1), sent_norm):
            return m.group(0)
        senales.append(f"{campo}: cita no literal retirada («{m.group(1)[:60]}…»)")
        return AVISO_CITA
    texto = _RE_CITA.sub(_cita, texto)

    def _marcar(rx, presente, etiqueta):
        nonlocal texto
        vistos = set()
        for m in list(rx.finditer(texto)):
            v = m.group(1)
            if v in vistos or presente(v):
                continue
            vistos.add(v)
            senales.append(f"{campo}: {etiqueta} {v} no aparece en la sentencia")
            texto = re.sub(rf"({re.escape(m.group(0))})(?!{re.escape(MARCA)})", rf"\1{MARCA}", texto)
    _marcar(_RE_MONTO, lambda v: monto_presente(v, sent_dig), "monto")
    _marcar(_RE_JUICIO, lambda v: _digitos(v) in sent_dig, "número de juicio")
    return texto, senales


_RE_VINETA_VACIA = re.compile(r"^\s*(?:[•●▪◦\-–*]|\d+[.)])?\s*[\s,;:.…]*$")


def sin_avisos(texto: str) -> str:
    """Quita AVISO_CITA del texto que ve el usuario (puerta v7: 18 marcas a la
    vista) y las viñetas que quedan vacías. La señal queda en la puerta."""
    salida = []
    for linea in texto.split("\n"):
        if AVISO_CITA in linea:
            linea = re.sub(r"\s*" + re.escape(AVISO_CITA), "", linea)
            if _RE_VINETA_VACIA.match(linea):
                continue
        salida.append(linea)
    return "\n".join(salida)


def aplicar(resultado: dict, sentencia: str, ajustes: list, senales: list) -> None:
    """Verifica en el sitio resumen, extracción y jurisprudencia de `resultado`."""
    if not ACTIVA:
        return
    for campo in ("resumen", "extraccion", "jurisprudencia"):
        nuevo, s = verificar(resultado.get(campo) or "", sentencia, campo)
        if s:
            resultado[campo] = sin_avisos(nuevo)
            senales.extend(s)
            ajustes.append(f"Fidelidad: {len(s)} dato(s) no verificables en {campo}")
