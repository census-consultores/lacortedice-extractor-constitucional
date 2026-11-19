"""
Tests de resolucion/extractores/{tribunal,corte}.py — mapeo de campos entre
lo que devuelve la capa IA (resolucion.ai) y el dict de fila (Strapi/Excel).

La capa IA se mockea siempre: estos tests validan el MAPEO de campos, no el
análisis GPT/Qwen en sí (eso requeriría llamadas reales, fuera de alcance
de un test unitario).
"""
from resolucion.extractores import tribunal as extractor_tribunal
from resolucion.extractores import corte as extractor_corte
from config.config import (
    COMPLETITUD_COMPLETO, COMPLETITUD_PARCIAL, COMPLETITUD_ERROR_GPT,
)


# ── extractores/tribunal.py ──────────────────────────────────────────────────

def test_extraer_tribunal_exito_completo(monkeypatch):
    gpt_resultado = {
        "error": "",
        "cuantia": "1000.00",
        "tipo_accion": "Acción de Impugnación",
        "objeto_controversia": "Glosa de IVA",
        "decision_final": "Con lugar",
        "resumen": "Resumen ejecutivo",
        "categorias": "IVA",
        "extraccion": "texto extraccion",
        "jurisprudencia": "texto jurisprudencia",
        "fecha_emision": "2023-05-01",
        "administracion": "SRI",
        "cuantia_origen": "gpt",
        "pdf_truncado": False,
        "modo": "completo",
        "validacion_ok": True,
        "docx_bytes": b"docx-bytes",
        "_auditoria": {"cuantia": True, "tipo_accion": True},
    }
    monkeypatch.setattr(extractor_tribunal, "analizar_sentencia", lambda *a, **kw: gpt_resultado)

    resultado = extractor_tribunal.extraer(
        numero="09501-2023-00010",
        pdf_bytes=b"pdf-bytes",
        demandado="SRI",
        modelo="gpt-4o",
    )

    assert resultado["error"] == ""
    assert resultado["Completitud"] == COMPLETITUD_COMPLETO
    assert resultado["Cuantía"] == "1000.00"
    assert resultado["Tipo de Acción"] == "Acción de Impugnación"
    assert resultado["_docx_bytes_tribunal"] == b"docx-bytes"
    assert resultado["_docx_nombre_tribunal"] == "09501-2023-00010_ficha_tecnica.docx"
    assert resultado["_auditoria"] == {"cuantia": True, "tipo_accion": True}


def test_extraer_tribunal_propaga_abogados(monkeypatch):
    gpt_resultado = {
        "error": "", "validacion_ok": True,
        "abogado_actor": "Ab. Juan Pérez",
        "abogado_demandado": "Dra. María López",
    }
    monkeypatch.setattr(extractor_tribunal, "analizar_sentencia", lambda *a, **kw: gpt_resultado)

    resultado = extractor_tribunal.extraer(numero="09501-2023-00010", pdf_bytes=b"x")

    assert resultado["Abogado del Actor"] == "Ab. Juan Pérez"
    assert resultado["Abogado del Demandado"] == "Dra. María López"


def test_extraer_tribunal_validacion_parcial(monkeypatch):
    gpt_resultado = {
        "error": "",
        "validacion_ok": False,
        "validacion_problemas": ["Falta cuantía", "Falta tipo de acción"],
    }
    monkeypatch.setattr(extractor_tribunal, "analizar_sentencia", lambda *a, **kw: gpt_resultado)

    resultado = extractor_tribunal.extraer(numero="09501-2023-00010", pdf_bytes=b"x")

    assert resultado["Completitud"] == COMPLETITUD_PARCIAL
    assert "Falta cuantía" in resultado["Observaciones"]


def test_extraer_tribunal_error_gpt_no_propaga_campos(monkeypatch):
    monkeypatch.setattr(
        extractor_tribunal, "analizar_sentencia",
        lambda *a, **kw: {"error": "OpenAI timeout"},
    )

    resultado = extractor_tribunal.extraer(numero="09501-2023-00010", pdf_bytes=b"x")

    assert resultado["error"] == "OpenAI timeout"
    assert resultado["Completitud"] == COMPLETITUD_ERROR_GPT
    assert resultado["Cuantía"] == ""  # no se sobreescribe con datos parciales


def test_extraer_tribunal_guarda_pdf_local(tmp_path, monkeypatch):
    monkeypatch.setattr(
        extractor_tribunal, "analizar_sentencia",
        lambda *a, **kw: {"error": "", "validacion_ok": True},
    )

    extractor_tribunal.extraer(
        numero="09501-2023-00010", pdf_bytes=b"contenido-pdf",
        carpeta_pdfs=tmp_path,
    )

    archivo = tmp_path / "09501-2023-00010_sentencia.pdf"
    assert archivo.exists()
    assert archivo.read_bytes() == b"contenido-pdf"


# ── extractores/corte.py ─────────────────────────────────────────────────────

def test_extraer_corte_exito(monkeypatch):
    gpt_resultado = {
        "error": "",
        "resumen": "resumen cnj",
        "extraccion": "extraccion cnj",
        "jurisprudencia": "jurisprudencia cnj",
        "validacion_ok": True,
        "docx_bytes": b"docx-cnj",
    }
    monkeypatch.setattr(extractor_corte, "analizar_casacion", lambda *a, **kw: gpt_resultado)

    resultado = extractor_corte.extraer(pdf_bytes=b"pdf-cnj", numero="09501-2023-00010")

    assert resultado["error"] == ""
    assert resultado["Completitud CNJ"] == COMPLETITUD_COMPLETO
    assert resultado["_resumen_cnj"] == "resumen cnj"
    assert resultado["_docx_bytes_cnj"] == b"docx-cnj"
    assert resultado["_docx_nombre_cnj"] == "09501-2023-00010_ficha_tecnica_cnj.docx"


def test_extraer_corte_error_gpt(monkeypatch):
    monkeypatch.setattr(
        extractor_corte, "analizar_casacion",
        lambda *a, **kw: {"error": "DeepInfra 500"},
    )

    resultado = extractor_corte.extraer(pdf_bytes=b"x", numero="09501-2023-00010")

    assert resultado["error"] == "DeepInfra 500"
    assert resultado["Completitud CNJ"] == COMPLETITUD_ERROR_GPT
    assert resultado["_resumen_cnj"] == ""


def test_extraer_corte_parcial_guarda_observacion(monkeypatch):
    monkeypatch.setattr(
        extractor_corte, "analizar_casacion",
        lambda *a, **kw: {
            "error": "", "validacion_ok": False,
            "validacion_problemas": ["Sin jurisprudencia citada"],
        },
    )

    resultado = extractor_corte.extraer(pdf_bytes=b"x", numero="09501-2023-00010")

    assert resultado["Completitud CNJ"] == COMPLETITUD_PARCIAL
    assert resultado["_obs_cnj"] == "Sin jurisprudencia citada"
