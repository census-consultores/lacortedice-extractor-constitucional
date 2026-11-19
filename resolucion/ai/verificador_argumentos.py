"""Verificación de atribución de los argumentos de las partes en apelación y casación.

Puerta civil v3: en 8 de 21 apelaciones el modelo llenó "Argumentos del
Recurrente" y "Argumentos de la Contraparte" con el análisis de la Sala, con
escritos de primera instancia o con declaraciones de testigos, aunque el prompt
lo prohíbe. Una regla por palabras no los distingue (se probó y daba falsos
positivos), así que se hace una segunda llamada corta: el modelo clasifica cada
viñeta contra la sentencia y solo quedan las que la sentencia atribuye a esa
parte. La verificación solo QUITA viñetas; nunca escribe contenido.
"""
import json
import re

# Puerta v7: la sección 8 (agravios) también traía alegaciones que el recurrente no hizo.
SECCIONES = {8: "Causales o agravios invocados por el recurrente", 9: "Argumentos del Recurrente",
             10: "Argumentos de la Contraparte"}
_RE_VINETA = re.compile(r"^\s*(?:[•●▪◦\-–*]|\d+[.)])\s+")
NO_CONSTA = "No consta en la sentencia."


def _cuerpo(ext: str, n: int):
    m = re.search(rf"(?m)^\**\s*{n}\.\s+[^\n]*\n", ext)
    if not m:
        return None
    sig = re.search(rf"(?m)^\**\s*(?:{n + 1}\.\s|[IVX]+\.\s)", ext[m.end():])
    fin = m.end() + sig.start() if sig else len(ext)
    return m.end(), fin, ext[m.end():fin]


def vinetas(ext: str) -> dict:
    """{seccion: [texto de cada viñeta]} de las secciones 9 y 10."""
    out = {}
    for n in SECCIONES:
        c = _cuerpo(ext, n)
        if c:
            v = [l.strip() for l in c[2].split("\n") if _RE_VINETA.match(l)]
            if v:
                out[n] = v
    return out


def prompt(vin: dict) -> str:
    lista = "\n".join(f"[{n}.{i}] ({SECCIONES[n]}) {_RE_VINETA.sub('', v)}"
                      for n, vs in vin.items() for i, v in enumerate(vs, 1))
    return (
        "Eres un verificador. Abajo hay viñetas que una ficha atribuye a las partes de un "
        "recurso, y después la sentencia que resuelve ese recurso. Para cada viñeta decide, "
        "solo con la sentencia, de quién es lo que dice:\n"
        "- PARTE: la sentencia atribuye ese argumento a esa parte en este recurso (lo alegó, "
        "fundamentó, expuso o contestó en esta instancia).\n"
        "- TRIBUNAL: es análisis, razonamiento o conclusión del tribunal.\n"
        "- OTRO: viene de la demanda, la contestación, la sentencia de primera instancia, "
        "testigos o peritos, y la sentencia no lo atribuye a la parte en este recurso.\n"
        "- NO_CONSTA: no aparece en la sentencia.\n"
        "Para cada PARTE copia en \"cita\" un fragmento LITERAL de la sentencia (8 a 25 palabras, "
        "sin cambiar nada) del pasaje donde la sentencia relata que esa parte lo dijo en este recurso. "
        "Si no encuentras ese pasaje, no es PARTE.\n"
        "Responde SOLO con JSON, una lista sin texto adicional: "
        "[{\"id\": \"9.1\", \"veredicto\": \"PARTE\", \"cita\": \"...\"}, "
        "{\"id\": \"9.2\", \"veredicto\": \"TRIBUNAL\"}, ...]. Una entrada por viñeta.\n\n"
        f"VIÑETAS:\n{lista}\n\nSENTENCIA:\n"
    )


_RE_PAR = re.compile(r'"id"\s*:\s*"?(\d+\.\d+)"?\s*,\s*"veredicto"\s*:\s*"([A-Z_]+)"'
                     r'(?:\s*,\s*"cita"\s*:\s*"((?:[^"\\]|\\.)*)")?', re.I)


def leer_veredictos(salida: str) -> dict:
    """Lee cada id/veredicto/cita por separado: una respuesta cortada por el
    límite de tokens o con texto alrededor (piloto v4: "respuesta ilegible")
    igual entrega los veredictos que alcanzó a escribir. Valor: (veredicto, cita)."""
    return {i: (v.upper(), c) for i, v, c in _RE_PAR.findall(salida or "")}


# ── Comprobación de la cita (puerta civil v6) ─────────────────────────────
# Con la sentencia completa el verificador seguía dando por PARTE viñetas
# armadas con la demanda, el análisis de la Sala o diligencias del tribunal
# (16 de 21 apelaciones). Ahora PARTE exige una cita literal que exista en la
# sentencia y que esté en el bloque del recurso (antes de ella, a menos de
# 2.500 caracteres, se habla del recurso, la apelación o la audiencia de esta
# instancia). Si no, la viñeta sale.
_RE_RECURSO = re.compile(r"recurs|apelac|apelant|recurrent|casacion|adhesi|agravi|audiencia|segunda instancia|"
                         r"fundamentac|contestacion al recurso")
VENTANA = 2500


def _plano(texto: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def cita_valida(cita: str, sentencia_plana: str) -> bool:
    c = _plano(cita).split()
    if len(c) < 6:
        return False
    # Basta un tramo de 6 palabras seguidas de la cita (el modelo a veces
    # corta o une frases); se busca el primero que aparezca.
    for k in range(0, len(c) - 5):
        pos = sentencia_plana.find(" ".join(c[k:k + 6]))
        if pos >= 0:
            return bool(_RE_RECURSO.search(sentencia_plana[max(0, pos - VENTANA):pos + 200]))
    return False


def aplicar(ext: str, vin: dict, veredictos: dict, sentencia: str = None) -> tuple:
    """Quita las viñetas que no son de la parte. Una viñeta sin veredicto se
    conserva (si la verificación falla, la ficha queda como estaba). Con
    `sentencia`, una PARTE sin cita literal ubicada en el bloque del recurso
    también sale (SIN_CITA)."""
    plana = " " + _plano(sentencia) + " " if sentencia else None
    quitadas = []
    for n in sorted(vin, reverse=True):
        c = _cuerpo(ext, n)
        if not c:
            continue
        ini, fin, cuerpo = c
        lineas, i = [], 0
        for l in cuerpo.split("\n"):
            if _RE_VINETA.match(l):
                i += 1
                v, cita = veredictos.get(f"{n}.{i}", ("PARTE", None))
                if v == "PARTE" and plana is not None and cita is not None and not cita_valida(cita, plana):
                    v = "SIN_CITA"
                if v in ("TRIBUNAL", "OTRO", "NO_CONSTA", "SIN_CITA"):
                    quitadas.append((n, v, l.strip()))
                    continue
            lineas.append(l)
        nuevo = "\n".join(lineas)
        if not any(_RE_VINETA.match(l) for l in lineas):
            nuevo = "\n" + NO_CONSTA + "\n\n"
        ext = ext[:ini] + nuevo + ext[fin:]
    return ext, quitadas
