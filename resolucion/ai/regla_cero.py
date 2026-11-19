"""Regla cero en código — lo que no consta en la sentencia no se publica.

Mismo módulo en todas las materias de La Corte Dice (laboral lo tiene dentro de
controles.py desde el PR #42). Nació de los pilotos laborales v12/v13: la IA
restaba montos por su cuenta ($2.276,89 donde la sentencia fija $410,52),
agregaba pedidos típicos que nadie hizo (costas, intereses, recargos) y guardaba
como ficha válida el mensaje de error de la API («402 sin créditos»).

Tres controles deterministas, sin IA:
- exigir_sin_error_api: si un campo es un error de la API, se lanza y la causa
  cuenta como fallida; la ficha no se genera ni se guarda.
- montos_sin_respaldo: todo monto en dólares del resumen y de la ficha debe estar
  en la sentencia (en cifras o como miles en letras); si no, se quita la viñeta
  u oración y la ficha queda en «Parcial - revisar».
- pedidos_tipicos_no_pedidos: costas, honorarios, intereses y recargos solo
  quedan como pretensión si la sentencia los relaciona con lo demandado.
"""
import re
import unicodedata

_RE_ENCABEZADO = re.compile(r"^[ \t]*\**(?:#{1,3}\s*)?\**(\d+)\.[ \t]+[^\n]*", re.M)
_RE_DOLARES = re.compile(r"(?:\$|usd|us\$)\s*(\d[\d.,]*\d|\d)|(\d[\d.,]*\d)\s*(?:usd|dolares)", re.I)
_RE_PEDIDO_TIPICO = re.compile(r"costas|honorarios|intereses|recargo|triple", re.I)
_UNIDADES = ("un dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince "
             "dieciseis diecisiete dieciocho diecinueve veinte").split()
_DECENAS = {30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta", 70: "setenta",
            80: "ochenta", 90: "noventa"}
NO_CONSTA = "No consta en la sentencia."


class FichaConErrorAPI(RuntimeError):
    """La IA devolvió un error (p. ej. 402 sin créditos) en lugar de contenido."""


def _sin_tildes(texto: str) -> str:
    t = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _oraciones(parrafo: str) -> list:
    return [o.strip() for o in re.split(r"(?<=[.;])\s+(?=[A-ZÁÉÍÓÚÑ¿¡\"“])", parrafo) if o.strip()]


def _entero(monto: str) -> str:
    return re.sub(r"\D", "", re.sub(r"[.,]\d{2}$", "", monto))


def _miles_en_letras(entero: int) -> str:
    """15000 → "quince mil" (solo miles exactos hasta 99 000)."""
    if entero % 1000 or not 1000 <= entero < 100000:
        return ""
    k = entero // 1000
    if k <= 20:
        return "mil" if k == 1 else _UNIDADES[k - 1] + " mil"
    d, u = (k // 10) * 10, k % 10
    return f"{_DECENAS[d]}{' y ' + _UNIDADES[u - 1] if u else ''} mil"


def _monto_consta(valor: str, digitos: str, norm: str) -> bool:
    solo, entero = re.sub(r"\D", "", valor), _entero(valor)
    if len(solo) < 4 or solo in digitos or entero in digitos:
        return True
    letras = _miles_en_letras(int(entero)) if entero.isdigit() else ""
    return bool(letras) and letras in norm


def exigir_sin_error_api(resultado: dict) -> None:
    for campo in ("resumen", "extraccion", "jurisprudencia", "categorias"):
        texto = (resultado.get(campo) or "").lstrip()
        if texto.startswith("[Error"):
            raise FichaConErrorAPI(f"{campo}: {texto[:160]}")


def montos_sin_respaldo(resultado: dict, sentencia: str, ajustes: list, senales: list,
                        campos=("resumen", "extraccion")) -> None:
    digitos = re.sub(r"[\s.,]", "", sentencia or "")
    if not digitos:          # modo visión: no hay texto contra qué comparar
        return
    norm = _sin_tildes(sentencia)
    quitados = []
    for campo in campos:
        lineas = []
        for linea in (resultado.get(campo) or "").split("\n"):
            malos = [m.group(1) or m.group(2) for m in _RE_DOLARES.finditer(_sin_tildes(linea))]
            malos = [v for v in malos if not _monto_consta(v, digitos, norm)]
            if not malos:
                lineas.append(linea)
                continue
            quitados += malos
            if linea.lstrip().startswith(("•", "-", "*")):
                continue
            quedan = [o for o in _oraciones(linea) if not any(v in _sin_tildes(o) for v in malos)]
            lineas.append(" ".join(quedan) if quedan else ("" if campo == "resumen" else NO_CONSTA))
        texto = "\n".join(lineas)
        if campo == "extraccion":  # sección que quedó sin viñetas
            texto = re.sub(r"(^[ \t]*\**\d+\.[^\n]*:[ \t]*\**[ \t]*\n)(?=[ \t]*\**\d+\.\s)",
                           r"\1" + NO_CONSTA + "\n", texto, flags=re.M)
        resultado[campo] = texto
    if quitados:
        lista = ", ".join(quitados[:5])
        ajustes.append(f"Regla cero: montos sin respaldo en la sentencia quitados: {lista}")
        senales.append(f"Revisar: se quitaron montos que no constan en la sentencia ({lista})")


def _seccion(ext: str, n: int):
    heads = list(_RE_ENCABEZADO.finditer(ext or ""))
    for i, m in enumerate(heads):
        if int(m.group(1)) == n:
            fin = next((h.start() for h in heads[i + 1:] if int(h.group(1)) > n), len(ext))
            return m.end(), fin
    return None


def pedidos_tipicos_no_pedidos(ext: str, n: int, sentencia: str, ajustes: list, senales: list) -> str:
    """Quita de la sección n (pretensiones) las viñetas de costas, honorarios,
    intereses o recargos que la sentencia no relaciona con lo demandado."""
    s = _seccion(ext, n) if n else None
    norm = _sin_tildes(sentencia)
    if not s or not norm.strip():
        return ext
    quitadas, nuevas = 0, []
    for linea in ext[s[0]:s[1]].split("\n"):
        clave = _RE_PEDIDO_TIPICO.search(_sin_tildes(linea))
        if clave:
            k = clave.group(0).lower()
            pedido = re.search(rf"(demand|pretend|pretensi|solicit|reclam|pide|pidi|peticion)\w*[^.]{{0,300}}{k}"
                               rf"|{k}[^.]{{0,120}}(solicit|reclam|demand|pretend|pide|pidi)", norm)
            if not pedido:  # o la sentencia trae el mismo pedido con sus palabras junto a la clave
                raices = [p[:6] for p in re.findall(r"[a-z]{5,}", _sin_tildes(linea))
                          if not _RE_PEDIDO_TIPICO.search(p)]
                pedido = raices and any(
                    sum(r in norm[max(0, m.start() - 250):m.end() + 250] for r in raices) >= max(2, len(raices) * 0.6)
                    for m in re.finditer(k, norm))
            if not pedido:
                quitadas += 1
                continue
        nuevas.append(linea)
    if not quitadas:
        return ext
    cuerpo = "\n".join(nuevas).strip() or NO_CONSTA
    ajustes.append(f"Regla cero: se quitaron {quitadas} pretensiones que la sentencia no recoge como pedidas")
    senales.append("Revisar: se quitaron pretensiones típicas (costas, intereses, recargos) que la sentencia no recoge como pedidas")
    return ext[:s[0]] + "\n" + cuerpo + ("\n\n" if s[1] < len(ext) else "\n") + ext[s[1]:]
