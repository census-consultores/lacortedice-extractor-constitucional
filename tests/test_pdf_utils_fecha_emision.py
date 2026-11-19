"""
Tests de buscar_fecha_emision (config/pdf_utils.py):
  - Las sentencias casi nunca traen "fecha de emisión: DD/MM/AAAA" en
    formato numérico — lo habitual es la fecha en prosa ("22 de abril
    del 2019"). Antes solo se reconocía el formato numérico, así que el
    checklist marcaba "falta fecha_emision" aunque la IA sí la hubiera
    identificado correctamente en su propia extracción.
"""
from config.pdf_utils import buscar_fecha_emision


def test_formato_numerico():
    assert buscar_fecha_emision("Fecha de emisión: 22/04/2019") == "22/04/2019"


def test_formato_prosa():
    assert buscar_fecha_emision("Fecha de emisión: 22 de abril del 2019") == "22 de abril del 2019"


def test_formato_prosa_sin_del():
    assert buscar_fecha_emision("Fecha de emisión: 22 de abril de 2019") == "22 de abril de 2019"


def test_formato_prosa_con_salto_de_linea():
    texto = "4. Fecha de Emisión:\n22 de abril del 2019\n\n5. Partes Procesales:"
    assert buscar_fecha_emision(texto) == "22 de abril del 2019"


def test_sin_etiqueta_no_encuentra_nada():
    # El PDF crudo rara vez trae la etiqueta "fecha de emisión" — la fecha
    # suele estar en el encabezado sin ese rótulo. Documenta el límite real
    # de esta función: por eso tribunal.py también prueba contra la propia
    # extracción de la IA, que sí sigue el formato pedido en el prompt.
    assert buscar_fecha_emision("Guayaquil, 22 de abril del 2019.\n\nVISTOS:") == ""


def test_vacio():
    assert buscar_fecha_emision("") == ""
    assert buscar_fecha_emision(None) == ""
