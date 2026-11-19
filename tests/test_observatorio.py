"""Observatorio normativo: extracción de citas, señales y decisiones sin IA."""
from scripts.observatorio import Acumulador, citas_de_articulos, decision_por_texto, normalizar


def _citas(s):
    return citas_de_articulos(normalizar(s))


def test_cita_simple_y_lista():
    assert _citas("según el artículo 1453 del Código Civil") == {("Código Civil", "1453")}
    assert _citas("arts. 1561, 1562 y 1576 del Código Civil vigente") == {
        ("Código Civil", "1561"), ("Código Civil", "1562"), ("Código Civil", "1576")}


def test_numeral_e_inciso_no_son_articulos():
    assert _citas("artículo 76 numeral 7 literal l) de la Constitución de la República") == {
        ("Constitución", "76")}
    assert _citas("Art. 332 inciso segundo del COGEP") == {("COGEP", "332")}


def test_miles_con_punto_y_sin_cuerpo():
    assert _citas("artículo 2.392 del Código Civil") == {("Código Civil", "2392")}
    assert _citas("el artículo 89 ibídem") == set()
    assert _citas("el artículo 89 del mismo cuerpo legal") == set()


def test_cogep_por_nombre_completo():
    assert _citas("art. 147 del Código Orgánico General de Procesos") == {("COGEP", "147")}


def test_senales_y_pasajes():
    acc = Acumulador()
    texto = "Existe un vacío legal respecto del plazo; por analogía se aplica la regla."
    acc.agregar("09332-2024-00001", "tribunal", texto, "UNIDAD CIVIL", "2024")
    r = acc.resultado()
    assert r["senales"]["Vacío legal o normativo"] == {"tribunal": 1}
    assert r["senales"]["Aplicación por analogía"] == {"tribunal": 1}
    p = r["pasajes"]["Vacío legal o normativo|tribunal"][0]
    assert "vacío legal" in p["pasaje"] and p["numero"] == "09332-2024-00001"


def test_cc_y_resoluciones():
    acc = Acumulador()
    acc.agregar("x", "corte", "Sentencia 1158-17-EP/21 y la Resolución No. 05-2017 de la "
                "Corte Nacional de Justicia; otra vez 1158-17-EP/21.", None, None)
    r = acc.resultado()
    assert r["cc_mas_citadas"] == [{"sentencia": "1158-17-ep/21", "causas": 1}]
    assert r["resoluciones_cnj_mas_citadas"] == [{"resolucion": "05-2017", "causas": 1}]


def test_decision_por_texto():
    t = normalizar("Considerandos... RESUELVE: rechazar el recurso de apelación y confirmar la sentencia")
    assert decision_por_texto("apelacion", t) == "Confirma la sentencia."
    t = normalizar("ADMINISTRANDO JUSTICIA ... no casa la sentencia recurrida")
    assert decision_por_texto("corte", t) == "No se casó"
    assert decision_por_texto("tribunal", t) is None
