"""Piloto civil v2: sentencias largas (continuación de la extracción) y subcategorías inventadas."""
from resolucion.ai.tribunal import secciones_faltantes, recortar_seccion_incompleta, instruccion_continuacion
from resolucion.ai._cliente import _reubicar_subcategorias


def test_secciones_faltantes_desde_la_primera_que_falta():
    t = "1. Instancia:\nx\n2. Unidad:\ny\n12. Antecedentes:\nz"
    assert secciones_faltantes(t, 5) == [3, 4, 5]
    full = "\n".join(f"{i}. T:" for i in range(1, 20))
    assert secciones_faltantes(full, 19) == []


def test_recorta_la_seccion_cortada_y_pide_desde_ella():
    t = "1. A:\na\n11. Objeto:\nb\n12. Antecedentes:\ntexto cortado a mit"
    assert recortar_seccion_incompleta(t, 13).endswith("b")
    assert "secciones 12 a 19" in instruccion_continuacion(13, 19)


def test_subcategoria_que_no_esta_en_el_catalogo_se_descarta():
    cats = {18: ("Derecho societario", ["Nulidad de resoluciones de junta general"])}
    perfil = [[18, "Derecho societario", ["Nulidad de resoluciones de junta general", "Abuso del derecho y fraude societario"]]]
    assert _reubicar_subcategorias(perfil, cats) == [[18, "Derecho societario", ["Nulidad de resoluciones de junta general"]]]


def test_decision_del_perfil_prefiere_la_demanda_aunque_no_use_la_frase_canonica():
    from resolucion.ai._cliente import _decision_perfil
    t = "Rechaza la reconvención.\nADMINISTRANDO JUSTICIA …, se Acepta la demanda."
    assert _decision_perfil(t, primera_instancia=True) == "Acepta la demanda."
    assert _decision_perfil("Se acepta parcialmente la demanda.", primera_instancia=True) == "Acepta parcialmente la demanda."
