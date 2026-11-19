"""
Tests del flujo "corpus primero": solo_descarga (bajar sin IA), re-extracción
desde PDFs guardados y evaluación contra el conjunto patrón. Sin red ni base.
"""
import json
import sys
import types
from pathlib import Path

import pytest

from worker import reextraccion_worker as rw
from worker import runner
import worker.job_manager as jm


# ── parseo de la marca del job ───────────────────────────────────────────────

def test_parsear_marca_defaults():
    assert rw.parsear_marca("reextraccion") == {"niveles": ["tribunal", "corte", "apelacion"],
                                                 "completitud": None, "limite": None}


def test_parsear_marca_completa():
    m = rw.parsear_marca("reextraccion:niveles=corte,apelacion,zzz:completitud=PDF sin análisis:limite=25")
    assert m["niveles"] == ["corte", "apelacion"]
    assert m["completitud"] == "PDF sin análisis"
    assert m["limite"] == 25


def test_url_absoluta(monkeypatch):
    monkeypatch.setattr(rw, "STRAPI_URL", "https://cms.ejemplo.ec")
    assert rw._url_absoluta("/uploads/a.pdf") == "https://cms.ejemplo.ec/uploads/a.pdf"
    assert rw._url_absoluta("https://cdn.x/a.pdf") == "https://cdn.x/a.pdf"
    assert rw._url_absoluta("") == ""


# ── solo_descarga: el runner no llama a la IA pero sí persiste ───────────────

def test_runner_solo_descarga_apaga_ia_y_persiste_cascarones():
    src = Path(runner.__file__).read_text(encoding="utf-8")
    assert '"gpt_ok":          not dry_run and not solo_descarga' in src
    assert '"gpt_cnj_ok":      not dry_run and not solo_descarga' in src
    # Las clasificaciones internas (prompts 7/8/10) no corren sin IA
    assert 'fila.get("_tipos_defecto") and not solo_descarga' in src
    # Casación y apelación se crean como cascarón con el PDF
    assert 'solo_descarga and fila.get("_file_id_pdf_cnj")' in src
    assert 'solo_descarga and fila.get("_file_id_pdf_apel")' in src


def test_crear_job_acepta_solo_descarga():
    import inspect
    assert "solo_descarga" in inspect.signature(jm.crear_job).parameters


# ── re-extracción de una causa (IA y Strapi mockeados) ───────────────────────

class _StrapiFake:
    _headers = {}
    def __init__(self):
        self.guardadas = []
    def _buscar_causa(self, numero):
        return {"documentId": "doc-1"}
    def subir_docx(self, data, nombre):
        return f"/uploads/{nombre}"
    def subir_pdf_con_id(self, data, nombre):
        return f"/uploads/{nombre}", 99


def test_reextraer_una_tribunal(monkeypatch, tmp_path):
    datos = {"numero": "09359-2024-00010", "url_pdf_tribunal": "/uploads/09359-2024-00010_sentencia.pdf",
             "url_pdf_cnj": None, "url_pdf_apel": None, "estado_juicio": "Archivado",
             "resultado": "Con sentencia.", "completitud": "PDF sin análisis"}
    causa = {"tribunal_codigo": "09359", "tribunal_nombre": "Unidad Judicial de Trabajo",
             "año": "2024", "actor": "Juan Pérez", "demandado": "Empresa S.A."}

    monkeypatch.setattr(rw, "descargar_pdf_guardado", lambda url, **kw: b"%PDF-fake")
    llamadas = {}
    def fake_extraer(**kw):
        llamadas.update(kw)
        return {"error": "", "Decisión Final": "Acepta la demanda.", "Completitud": "Completo",
                "_extraccion": "x", "_docx_bytes_tribunal": b"docx", "_docx_nombre_tribunal": "f.docx",
                "_tipos_defecto": "DESPIDO_INTEMPESTIVO", "_auditoria": {"cuantia": True}}
    monkeypatch.setattr(rw, "extraer_tribunal", fake_extraer)
    guardado = {}
    monkeypatch.setattr(rw.cms_tribunal, "guardar", lambda fila, strapi, job_id: guardado.update(fila) or {"data": {"documentId": "doc-1"}})
    monkeypatch.setattr(rw, "_persistir_extras_tribunal", lambda *a, **k: None)
    monkeypatch.setattr(rw, "_relink_pdf", lambda *a, **k: None)
    monkeypatch.setattr(rw.jm, "registrar_caso", lambda job_id, fila: None)

    hecho = rw.reextraer_una(datos, causa, ["tribunal"], "gpt-4o", "alibaba", 7,
                             _StrapiFake(), tmp_path, tmp_path)

    assert hecho == ["tribunal ✓"]
    assert llamadas["pdf_bytes"] == b"%PDF-fake"
    assert llamadas["demandado"] == "Empresa S.A."
    assert llamadas["nombre_tribunal"] == "Unidad Judicial de Trabajo"
    assert guardado["Decisión Final"] == "Acepta la demanda."
    assert guardado["Número de juicio"] == "09359-2024-00010"
    # el PDF no se re-sube: solo viaja la URL ya conocida
    assert "_pdf_bytes_tribunal" not in guardado
    assert guardado["_url_pdf_tribunal"] == "/uploads/09359-2024-00010_sentencia.pdf"


def test_reextraer_una_sin_pdf_omite(monkeypatch, tmp_path):
    datos = {"numero": "09359-2024-00011", "url_pdf_tribunal": None, "url_pdf_cnj": None, "url_pdf_apel": None}
    monkeypatch.setattr(rw.jm, "registrar_caso", lambda job_id, fila: None)
    hecho = rw.reextraer_una(datos, {}, ["tribunal"], "gpt-4o", "alibaba", 7, _StrapiFake(), tmp_path, tmp_path)
    assert hecho == ["tribunal omitido (sin PDF guardado)"]


def test_reextraer_corte_requiere_causa_en_strapi(monkeypatch, tmp_path):
    class _SinCausa(_StrapiFake):
        def _buscar_causa(self, numero): return None
    datos = {"numero": "09359-2024-00012", "url_pdf_tribunal": None,
             "url_pdf_cnj": "/uploads/c.pdf", "url_pdf_apel": None}
    hecho = rw.reextraer_una(datos, {}, ["corte"], "gpt-4o", "alibaba", 7, _SinCausa(), tmp_path, tmp_path)
    assert hecho == ["corte/apelación omitidas (la causa no existe en Strapi)"]


# ── evaluación contra el patrón (lógica pura) ────────────────────────────────

def _cargar_evaluar():
    import importlib.util
    ruta = Path(__file__).resolve().parent.parent / "scripts" / "evaluar_patron.py"
    spec = importlib.util.spec_from_file_location("evaluar_patron", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_evaluar_una_puntaje_perfecto():
    ev = _cargar_evaluar()
    patron = {"numero": "x", "decision_final": "con lugar", "tipo_accion": "procedimiento sumario",
              "categorias": ["Despido intempestivo"], "tipos_defecto": ["DESPIDO_INTEMPESTIVO"],
              "campos_con_dato": ["cuantia", "decision_final"], "decision_apelacion": "Confirma la sentencia."}
    actual = {"decision_final": "con lugar", "tipo_accion": "procedimiento sumario — despido",
              "categorias": ["Despido Intempestivo"], "tipos_defecto": ["DESPIDO_INTEMPESTIVO"],
              "campos_con_dato": ["cuantia", "decision_final", "objeto"],
              "decision_apelacion": "Confirma la sentencia.", "completitud": "Completo"}
    r = ev._evaluar_una(patron, actual)
    assert r["puntaje"] == 100.0
    assert set(r["criterios"]) == {"decision_final", "tipo_accion", "categorias", "tipos_defecto",
                                   "campos_con_dato", "decision_apelacion"}


def test_evaluar_una_parcial():
    ev = _cargar_evaluar()
    patron = {"numero": "x", "decision_final": "sin lugar",
              "tipos_defecto": ["A", "B"], "campos_con_dato": ["cuantia", "objeto"]}
    actual = {"decision_final": "con lugar", "tipos_defecto": ["B", "C"], "campos_con_dato": ["cuantia"]}
    r = ev._evaluar_una(patron, actual)
    assert r["criterios"]["decision_final"] == 0.0
    assert r["criterios"]["tipos_defecto"] == pytest.approx(1 / 3)
    assert r["criterios"]["campos_con_dato"] == 0.5
    assert r["puntaje"] == pytest.approx(round(100 * (0 + 1 / 3 + 0.5) / 3, 1))


def test_evaluar_una_sin_criterios():
    ev = _cargar_evaluar()
    assert ev._evaluar_una({"numero": "x", "notas": "solo notas"}, {})["puntaje"] is None
