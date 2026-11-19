"""Regla de fidelidad: lo que no está en la sentencia no queda en la ficha sin aviso."""
from resolucion.ai import fidelidad as f

SENT = ("El juzgador considera que la posesión debe ser pública, pacífica e ininterrumpida por más de quince años "
        "para que proceda la prescripción. Se condena al pago de USD 4.300 y de $ 1o.5oo,oo. Juicio 17230-2019-07246.")


def test_cita_literal_se_conserva_y_la_inventada_se_retira():
    t = ('“la posesión debe ser pública, pacífica e ininterrumpida por más de quince años para que proceda”\n'
         '“el demandado actuó de mala fe al ocupar el inmueble sin título alguno durante todo ese tiempo”')
    nuevo, s = f.verificar(t, SENT, "jurisprudencia")
    assert "ininterrumpida por más de quince años" in nuevo
    assert "mala fe al ocupar" not in nuevo and f.AVISO_CITA in nuevo and len(s) == 1


def test_montos_existentes_con_otro_formato_u_ocr_no_se_marcan():
    nuevo, s = f.verificar("Capital USD 4.300,00 y costas USD 10.500,00.", SENT, "extraccion")
    assert s == [] and f.MARCA not in nuevo


def test_monto_y_juicio_inexistentes_se_marcan():
    nuevo, s = f.verificar("Se ordena pagar USD 99.999,00 (juicio 09332-2016-10456).", SENT, "extraccion")
    assert nuevo.count(f.MARCA) == 2 and len(s) == 2


def test_aplicar_registra_senales_para_parcial_revisar():
    r = {"resumen": "Pago de USD 77.777,00.", "extraccion": "", "jurisprudencia": ""}
    aj, se = [], []
    f.aplicar(r, SENT, aj, se)
    assert f.MARCA in r["resumen"] and se and aj


def test_referencia_bibliografica_y_comillas_rectas():
    sent = SENT + " Como enseña Eduardo Álvarez Correa en Obligaciones, Bogotá, Uniandes, 1978, página 90."
    t = ('“Eduardo Álvarez Correa, Obligaciones Bogotá, Uniandes 1978, pág.90.”\n'
         'Los bonos "Assets" indicaban al comprador y no hay ninguna cita aquí con "comillas rectas" largas.')
    nuevo, s = f.verificar(t, sent, "jurisprudencia")
    assert s == [] and nuevo == t
    _, s2 = f.verificar("“Juan Pérez Inventado, Tratado de cosas, Quito, Editorial Falsa, 2001, pág. 3.”", sent, "j")
    assert len(s2) == 1


def test_numero_de_juicio_con_otro_formato_no_se_marca():
    _, s = f.verificar("Juicio 17230-2019-07246.", "Proceso No. 17230 2019 07246 …", "extraccion")
    assert s == []
