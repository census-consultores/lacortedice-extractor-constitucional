"""
Regresiones de los controles de la ficha detectadas en la auditoría v4
(/home/claude/pilotos/v4/civil/comparacion_civil.md y v4/administrativo, sección 4).
Un test por corrección.
"""
import re
from pathlib import Path

from resolucion.ai import controles as C
import worker.strapi_client as sc

_RELLENO = "Antecedentes del proceso civil. " * 20
# Los montos de los casos de prueba constan en la sentencia (regla cero de montos).
_RELLENO += " Valores del expediente: 96246,00; 620229,41; 1865362; 1595,28; 400206,03; 39672,37; 37034,43. "
_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def _secciones(texto):
    return sc._parsear_secciones(texto)


# ── 1. El recorte del resumen nunca borra la decisión (al final) ─────────────

def test_resumen_largo_conserva_su_ultima_oracion():
    inicio = "La Unidad Judicial con sede en Cuenca emitió sentencia."
    medio = " ".join(f"Antecedente número {i} del proceso con varios datos procesales relevantes." for i in range(60))
    final = ("La jueza aceptó parcialmente la demanda y negó la comisión. "
             "Sin costas ni honorarios.")
    resumen = f"{inicio} {medio} {final}"
    r = {"resumen": resumen}
    info = C.aplicar(r, _RELLENO, None, nivel="tribunal")
    assert len(r["resumen"].split()) <= 350
    assert r["resumen"].startswith(inicio)
    assert r["resumen"].endswith(final)
    assert "Antecedente número 59" in r["resumen"] and "Antecedente número 0 " not in r["resumen"]
    assert not any("máximo 350" in s for s in info["senales"])


# ── 2. Cuantía: se reemplaza solo la fórmula inicial, nunca el monto ────────

_EXT_CUANTIA = """6. Tipo de Acción:
Acción subjetiva
7. Cuantía del Caso:
{linea}
8. Detalle de la Cuantía:
No consta en la sentencia.
"""


def _cuantia(linea, sentencia=_RELLENO):
    r = {"extraccion": _EXT_CUANTIA.format(linea=linea)}
    C.aplicar(r, sentencia, None, nivel="tribunal")
    return _secciones(r["extraccion"])[7]


def test_cuantia_conserva_el_monto_completo():
    # 01803-2021-00274: antes quedaba "USD 18".
    assert _cuantia("La cuantía se fija en 18 653,62 USD") == "Valor indicado en la sentencia: 18 653,62 USD"
    # 17811-2017-00027: "USD." no se reconocía y la fórmula persistía.
    assert _cuantia("La cuantía se fija en USD. 620.229,41 más impuestos") == (
        "Valor indicado en la sentencia: USD. 620.229,41 más impuestos")
    assert _cuantia("• La cuantía asciende a la suma de $1,595.28 mensuales") == (
        "• Valor indicado en la sentencia: $1,595.28 mensuales")
    # Fórmula que no es inicial (hay un concepto antes): la línea no se toca.
    assert _cuantia("Perjuicio mensual que asciende a $1,595.28") == "Perjuicio mensual que asciende a $1,595.28"
    # La sentencia usa la fórmula: no se toca.
    assert _cuantia("La cuantía se fija en 18 653,62 USD", _RELLENO + "La cuantía se fija en 18 653,62 USD") == (
        "La cuantía se fija en 18 653,62 USD")


# ── 3. Demandada que no comparece: prioridad sobre la negativa pura ─────────

_EXT_DEMANDADA = """7. Cuantía del Caso:
No consta
15. Argumentos de la Parte Demandada:
• El acto goza de presunción de legitimidad.
16. Decisión Final:
Acepta la demanda.
"""


def test_demandada_que_no_comparece_a_proceso_no_nego_pura_y_simplemente():
    sentencia = (_RELLENO + "consta la demanda presentada en contra de la deudora quien no\ncomparece a proceso "
                 "pese haber sido citada en legal y debida forma, por lo que su contestación se tendrá "
                 "como negativa pura y simple.")
    r = {"extraccion": _EXT_DEMANDADA}
    C.aplicar(r, sentencia, None, nivel="tribunal")
    s15 = _secciones(r["extraccion"])[15]
    assert s15.startswith("• La parte demandada no contestó la demanda")
    assert "negó pura y simplemente" not in s15

    for frase in ("La parte demandada no compareció pese a estar citada.",
                  "Se la declaró en rebeldía.",
                  "sin que la demandada haya comparecido al proceso."):
        r = {"extraccion": _EXT_DEMANDADA}
        C.aplicar(r, _RELLENO + frase, None, nivel="tribunal")
        assert "no contestó la demanda" in _secciones(r["extraccion"])[15], frase

    # No comparecer a la audiencia no es no contestar.
    r = {"extraccion": _EXT_DEMANDADA}
    C.aplicar(r, _RELLENO + "Se ha negado pura y simplemente la demanda. El actor no compareció a la audiencia.",
              None, nivel="tribunal")
    assert "negó pura y simplemente" in _secciones(r["extraccion"])[15]

    # Si la sentencia atribuye argumentos a la demandada, no se reemplaza.
    r = {"extraccion": _EXT_DEMANDADA}
    C.aplicar(r, _RELLENO + "La demandada no compareció al proceso. La demandada alegó "
              "la presunción de legitimidad del acto.", None, nivel="tribunal")
    assert "presunción de legitimidad" in _secciones(r["extraccion"])[15]


# ── 5. Extracto del voto: sin firmas, saltos de página ni palabras partidas ─

def test_extracto_voto_limpio_y_nombre_normalizado():
    texto = (_RELLENO + "ADMINISTRANDO JUSTICIA, se rechaza la demanda.\n"
             "VOTO SALVADO DEL JUEZ NACIONAL, DR. IVAN RODRIGO LARCO ORTUÑO\n"
             "VISTOS: Disiento de la mayoría porque la moti-\nvación del acto es suficiente conforme el art ículo 100\n"
             "del C ódigo Orgánico Administrativo.\n3\n\x0cEn consecuencia, se debe aceptar la demanda en el ámbito\n"
             "de la única pretensión. Notifíquese.\xad\n\n\n\n"
             "                     DR. IVAN RODRIGO LARCO ORTUÑO\n\n"
             "                     JUEZ NACIONAL (PONENTE)\n\n\n"
             "                     DR. PATRICIO ADOLFO SECAIRA DURANGO\n\n         JUEZ NACIONAL (E)\n\x0c")
    _, voto = C.separar_voto(texto)
    assert voto["votos"] == [("Ivan Rodrigo Larco Ortuño", "salvado")]
    ext = C.extracto_voto(voto["texto"], 1200)
    assert ext == ("VISTOS: Disiento de la mayoría porque la motivación del acto es suficiente conforme el "
                   "artículo 100 del Código Orgánico Administrativo. En consecuencia, se debe aceptar la demanda "
                   "en el ámbito de la única pretensión. Notifíquese.-")
    assert C._nombre_propio("Dra. María LÓPEZ DE LA TORRE, JUEZA NACIONAL") == "María López de la Torre"


# ── 6. Jurisprudencia: precedentes con números que no están en la sentencia ─

def test_precedente_con_numero_ausente_se_quita():
    juris = ("1. Jurisprudencia mencionada en el caso:\n"
             "•\tResolución No. 232-2002\nPrimera Sala de lo Civil y Mercantil\nFecha: R.O. 27 de noviembre de 2002\n"
             "“la posesión que sirve de base…”\n"
             "•\tSentencia dictada en el juicio No. 17711-2014-0352\nCorte Nacional de Justicia\nFecha: No consta\n"
             "“la mera tenencia…”\n"
             "•\tSentencia No. 1158-17-EP/21\nCorte Constitucional\nFecha: No consta\n“la motivación exige…”\n"
             "2. Criterio del tribunal para resolver el caso:\n• “El acto no está motivado” (Considerando 5)\n")
    sentencia = _RELLENO + "Conforme la sentencia No. 1158-17-EP/21 de la Corte Constitucional, el acto no está motivado."
    r = {"jurisprudencia": juris}
    info = C.aplicar(r, sentencia, None, nivel="tribunal")
    assert "232-2002" not in r["jurisprudencia"] and "17711-2014-0352" not in r["jurisprudencia"]
    assert "1158-17-EP/21" in r["jurisprudencia"] and "El acto no está motivado" in r["jurisprudencia"]
    assert sum("se quitó de la jurisprudencia" in s for s in info["senales"]) == 2

    r = {"jurisprudencia": juris}
    C.aplicar(r, _RELLENO + "El acto no está motivado.", None, nivel="tribunal")
    assert r["jurisprudencia"].startswith("1. Jurisprudencia mencionada en el caso:\nNo consta en la sentencia.")


def test_ejemplos_de_los_prompts_de_jurisprudencia_sin_numeros_reales():
    for nombre in ("prompt_3_jurisprudencia.txt", "prompt_6_cnj_jurisprudencia.txt",
                   "prompt_12_apelacion_jurisprudencia.txt"):
        ruta = _PROMPTS / nombre
        if not ruta.exists():
            continue
        texto = ruta.read_text(encoding="utf-8")
        assert not re.search(r"(Resoluci[oó]n|[Jj]uicio|[Ss]entencia) No\.\s*\d", texto), nombre
        assert "nunca copies" in texto.lower(), nombre


# ── 8. Perfil: sin viñeta doble en la cuantía ────────────────────────────────

def test_perfil_cuantia_sin_vineta_doble():
    from resolucion.ai._cliente import _perfil_caso_texto
    ext = ("6. Tipo de Acción:\n• Procedimiento ejecutivo\n7. Cuantía del Caso:\n"
           "• Pretensión de la demanda: USD 5.226,57\n• Intereses: USD 300,00\n"
           "8. Detalle de la Cuantía:\nNo consta\n16. Decisión Final:\nAcepta parcialmente la demanda.\n")
    txt = _perfil_caso_texto(ext, "")
    assert "• Cuantía: Pretensión de la demanda: USD 5.226,57" in txt
    assert "• Tipo de acción: Procedimiento ejecutivo" in txt and "• •" not in txt and ": •" not in txt
