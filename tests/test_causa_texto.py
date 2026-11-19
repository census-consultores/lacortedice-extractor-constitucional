"""Índice de texto de las sentencias (causa_texto) y endpoint /buscar."""
import os

import pytest

from worker import job_manager as jm


def test_guardar_textos_solo_pdf_subidos(monkeypatch):
    guardados = []
    monkeypatch.setattr(jm, "guardar_texto", lambda n, niv, t: guardados.append((n, niv, t)))
    import resolucion.ai._cliente as cli
    monkeypatch.setattr(cli, "extraer_texto_pdf", lambda pdf, **kw: pdf.decode())
    fila = {
        "Número de juicio": "09332-2024-00001",
        "_pdf_bytes_tribunal": b"texto tribunal", "_url_pdf_tribunal": "/uploads/a.pdf",
        "_pdf_bytes_apel": b"texto apel",                      # sin URL: dry_run, no se indexa
        "_pdf_bytes_cnj": b"", "_url_pdf_cnj": "/uploads/c.pdf",  # sin bytes: nada que indexar
    }
    jm.guardar_textos_de_fila(fila)
    assert guardados == [("09332-2024-00001", "tribunal", "texto tribunal")]


def test_buscar_exige_clave_del_buscador(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main, "WORKER_API_KEY", "w")
    monkeypatch.setattr(main, "BUSCADOR_API_KEY", "b")
    monkeypatch.setattr(jm, "buscar_texto",
                        lambda q, **kw: {"total": 1, "resultados": [{"numero": "x", "url_pdf_tribunal": "/u.pdf"}]})
    c = TestClient(main.app)
    assert c.get("/buscar", params={"q": "velo"}, headers={"x-worker-key": "mala"}).status_code == 401
    r = c.get("/buscar", params={"q": "velo"}, headers={"x-worker-key": "b"})
    assert r.status_code == 200
    assert r.json()["resultados"][0]["url_pdf_tribunal"].endswith("/u.pdf")
    assert r.json()["resultados"][0]["url_pdf_tribunal"].startswith("http")
    assert "Buscador" in c.get("/buscador").text


def test_sql_de_causa_texto_es_consistente():
    assert "GENERATED ALWAYS AS (to_tsvector('spanish', texto))" in jm.CREATE_CAUSA_TEXTO_SQL
    assert all(n in jm.CREATE_CAUSA_TEXTO_SQL for n, _, _ in jm.NIVELES_TEXTO)
