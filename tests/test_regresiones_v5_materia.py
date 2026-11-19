"""
Auditoría v5 (civil): la compuerta nueva de rebeldía no rompe los casos que ya
funcionaban (09332-2025-00631, 17230-2025-00693), la cuantía de la demanda
(17233-2024-00454) y la sección IV con deliberación del modelo, con los
nombres del catálogo civil.
"""
import re

from resolucion.ai import controles as C
from resolucion.ai._cliente import _perfil_caso_texto, normalizar_clasificacion
import worker.strapi_client as sc

_RELLENO = "Antecedentes del proceso civil. " * 20

_EXT = """6. Tipo de Acción:
Procedimiento
7. Cuantía del Caso:
{cuantia}
8. Detalle de la Cuantía:
• Saldo del capital: USD 11.709,36
10. Excepciones:
• Pago parcial.
15. Argumentos de la Parte Demandada:
• Alegó haber pagado parte de la deuda.
NORMATIVA APLICADA:
• Art. 353 del COGEP
"""


def _aplicar(sentencia, cuantia="No consta en la sentencia."):
    r = {"extraccion": _EXT.format(cuantia=cuantia)}
    C.aplicar(r, _RELLENO + sentencia, None, nivel="tribunal")
    return sc._parsear_secciones(r["extraccion"])


def test_rebeldia_real_sigue_con_el_texto_estandar():
    # 17230-2025-00693: la orden "a fin de que conteste la demanda y/o proponga las excepciones"
    # no es una contestación; la demandada no compareció.
    s_00693 = ("se ha dispuesto que la parte demandada sea citada a fin de que conteste la demanda y/o proponga "
               "las excepciones de las que se crea asistida en el término de quince días. La demandada, en calidad "
               "de deudora principal, no ha comparecido a juicio dentro del término legal concedido.")
    # 09332-2025-00631.
    s_00631 = "Citada la demandada, no ha comparecido al proceso dentro del término concedido."
    for s in (s_00693, s_00631):
        secs = _aplicar(s)
        assert secs[10] == "• La parte demandada no contestó la demanda ni propuso excepciones.", s


def test_demandada_que_propuso_excepciones_no_es_rebelde():
    s = ("La Procuraduría General del Estado no ha comparecido al proceso. La demandada contestó la demanda y "
         "propuso la excepción de pago parcial.")
    secs = _aplicar(s)
    assert secs[10] == "• Pago parcial." and "haber pagado" in secs[15]


def test_00454_cuantia_fijada_en_la_demanda():
    s = "honorarios profesionales de mi abogado patrocinador.- La cuantía la fija en US$ 15.964,96.- Ofrece reconocer"
    assert _aplicar(s, "La cuantía se fija en US$ 15.964,96")[7] == "Cuantía fijada en la demanda: US$ 15.964,96"


_IV_CON_DELIBERACION = """CLASIFICACIÓN DE LA SENTENCIA EN BASE A LA PROBLEMÁTICA
Problemática Principal:
Cobro de un pagaré a la orden con vencimientos sucesivos.
Catálogo Aplicable:
CIVIL
Categoría 7: Mutuo, préstamo y garantías
• Subcategoría: Intereses y usura
Categoría 4: Nulidad, rescisión y simulación
• Subcategoría: Nulidad absoluta
*(Nota: No aplicable. Nadie alegó nulidad. Se elimina.)*
Verificación final:
- La categoría 7.2 está resuelta en el considerando quinto.
- La categoría 4.1 no es aplicable. Se elimina.
Categorías definitivas tras verificación:
Categoría 1:
7. Mutuo, préstamo y garantías
• Subcategoría:
2. Intereses y usura
Categoría 2:"""


def test_seccion_iv_lista_final_sin_deliberacion():
    texto, cats = normalizar_clasificacion(_IV_CON_DELIBERACION)
    assert cats == [(7, "Mutuo, préstamo y garantías", ["Intereses y usura"])]
    assert not re.search(r"Nota|Verificación|Se elimina|Nulidad|Categoría 2:", texto)
    perfil = _perfil_caso_texto("16. Decisión Final:\nAcepta la demanda.\n", _IV_CON_DELIBERACION)
    assert "– 7. Mutuo, préstamo y garantías" in perfil and "Nulidad" not in perfil
