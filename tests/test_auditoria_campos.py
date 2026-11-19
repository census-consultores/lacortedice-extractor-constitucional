"""
Tests de _auditoria_campos (resolucion/ai/tribunal.py) — el checklist de
campos faltantes que alimenta worker_causa_datos.faltante_*.

otros_actos/pruebas buscan encabezados ("10. Otros Actos...", "13.
Pruebas...") que son el FORMATO DE LA IA (según el prompt) — nunca
existen literales en el PDF crudo de la sentencia. _auditoria_campos()
debe recibir el texto que escribió la IA (resultado["extraccion"]), no
el PDF original — si no, estos dos campos casi siempre se marcan como
faltantes aunque la IA sí los haya extraído bien.
"""
from resolucion.ai.tribunal import _auditoria_campos

_EXTRACCION_COMPLETA = """1. Instancia: Tribunal Contencioso Tributario

10. Otros Actos Administrativos Relacionados:
* Resolución No. 123, emitida el 5 de enero de 2019 por el SRI.
Contenido o efecto mencionado en la sentencia: determina diferencias en la declaración.

11. Objeto de la Controversia:
Determinar si la glosa es procedente.

13. Pruebas Presentadas:
Por el Actor:
Expediente administrativo. Copias de las facturas emitidas por el proveedor.
Por el Demandado:
Expediente de las liquidaciones de pago por diferencias en la declaración.
"""

_RESULTADO_BASE = {
    "cuantia": "USD 100", "tipo_accion": "Acción de impugnación",
    "decision_final": "Declara con lugar", "objeto_controversia": "z",
    "jurisprudencia": "a" * 120, "categorias": "b" * 30,
    "fecha_emision": "2019-01-01", "abogado_actor": "x", "abogado_demandado": "y",
}


def test_otros_actos_presente_se_detecta_en_la_extraccion_no_en_el_pdf():
    resultado = {**_RESULTADO_BASE, "extraccion": _EXTRACCION_COMPLETA}
    aud = _auditoria_campos(resultado, texto_extraccion=resultado["extraccion"])
    assert aud["otros_actos"] is True


def test_pruebas_presente_se_detecta_en_la_extraccion_no_en_el_pdf():
    resultado = {**_RESULTADO_BASE, "extraccion": _EXTRACCION_COMPLETA}
    aud = _auditoria_campos(resultado, texto_extraccion=resultado["extraccion"])
    assert aud["pruebas"] is True


def test_pdf_crudo_no_tiene_encabezados_de_ia_pero_extraccion_si():
    """
    El PDF de una sentencia real nunca dice literal "13. Pruebas
    Presentadas:" — ese texto lo escribe la IA. Pasar el PDF crudo en vez
    de la extracción hacía que esto SIEMPRE diera falso, sin importar si
    la IA extrajo bien o mal.
    """
    pdf_crudo_realista = (
        "Guayaquil, 22 de abril del 2019. VISTOS: En el juicio contencioso "
        "tributario que sigue Juan Perez contra el SRI, se resuelve declarar "
        "con lugar la demanda conforme a los antecedentes expuestos..."
    )
    resultado = {**_RESULTADO_BASE, "extraccion": _EXTRACCION_COMPLETA}
    aud_con_pdf_crudo = _auditoria_campos(resultado, texto_extraccion=pdf_crudo_realista)
    aud_con_extraccion = _auditoria_campos(resultado, texto_extraccion=resultado["extraccion"])
    assert aud_con_pdf_crudo["otros_actos"] is False
    assert aud_con_pdf_crudo["pruebas"] is False
    assert aud_con_extraccion["otros_actos"] is True
    assert aud_con_extraccion["pruebas"] is True
