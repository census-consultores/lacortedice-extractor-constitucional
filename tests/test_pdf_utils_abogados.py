"""
Tests de config/pdf_utils.py::buscar_abogados — parsea "Abogado del Actor:"
y "Abogado del Demandado:" de la sección 5 (Partes Procesales) del texto
que ya generó el modelo (prompt_2_extraccion.txt), no del PDF crudo.
Los títulos (Ab., Dra.) se quitan al parsear (limpiar_nombre_abogado).
"""
from config.pdf_utils import buscar_abogados


def test_buscar_abogados_ambos_presentes():
    texto = """
5. Partes Procesales:
●\tActor (Demandante):
TAMAYO MERINO BYRON GUILLERMO
●\tAbogado del Actor:
Ab. Juan Carlos Pérez Solórzano
●\tDemandado (Administración Tributaria):
Director General del Servicio de Rentas Internas
●\tAbogado del Demandado:
Dra. María Fernanda López
6. Tipo de Acción:
Acción de impugnación
"""
    actor, demandado = buscar_abogados(texto)
    assert actor == "Juan Carlos Pérez Solórzano"
    assert demandado == "María Fernanda López"


def test_buscar_abogados_no_consta():
    texto = """
5. Partes Procesales:
●\tActor (Demandante):
Empresa XYZ S.A.
●\tAbogado del Actor:
No consta en la sentencia.
●\tDemandado (Administración Tributaria):
SRI
●\tAbogado del Demandado:
No consta en la sentencia.
6. Tipo de Acción:
"""
    actor, demandado = buscar_abogados(texto)
    assert actor == ""
    assert demandado == ""


def test_buscar_abogados_solo_uno_presente():
    texto = """
5. Partes Procesales:
●\tActor (Demandante):
Juan Pérez
●\tAbogado del Actor:
No consta en la sentencia.
●\tDemandado (Administración Tributaria):
SENAE
●\tAbogado del Demandado:
Ab. Pedro Ramírez
6. Tipo de Acción:
"""
    actor, demandado = buscar_abogados(texto)
    assert actor == ""
    assert demandado == "Pedro Ramírez"


def test_buscar_abogados_texto_vacio():
    assert buscar_abogados("") == ("", "")
    assert buscar_abogados(None) == ("", "")


def test_buscar_abogados_ignora_markdown():
    texto = """
●\tAbogado del Actor:
**Ab. Carlos Vega**
●\tDemandado (Administración Tributaria):
SRI
"""
    actor, _ = buscar_abogados(texto)
    assert actor == "Carlos Vega"
