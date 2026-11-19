"""Con SATJE caído el job pausa y reintenta el mismo número (sin marcar error),
y ACCIONES_ARRANQUE "reintentar_errores_red" recupera lo que quedó marcado."""
import json
import threading

import pytest
import requests

import api
from worker import job_manager as jm
from worker import runner


@pytest.fixture(autouse=True)
def _aislado(monkeypatch):
    monkeypatch.setattr(runner, "_satje", {"hasta": 0.0, "fallos": 0, "sondea": None})
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    for f in ("info", "warn", "error"):
        monkeypatch.setattr(runner.log, f, lambda *a, **k: None)
    monkeypatch.setattr(runner, "PAUSA_SEG", 0)
    monkeypatch.setattr(runner, "_subir_archivos_strapi", lambda *a, **k: None)
    estados, registrados = [], []
    monkeypatch.setattr(jm, "obtener_job", lambda _id: {
        "tribunal": "09332", "año": "2025", "checkpoint": 0, "desde": 7, "hasta": 7,
        "dry_run": True, "modelo": "x", "workers": "", "solo_descarga": True,
        "total_procesadas": 0, "exitosas": 0, "omitidas": 0, "errores": 0})
    monkeypatch.setattr(jm, "_actualizar", lambda _id, **k: estados.append(k.get("estado")))
    monkeypatch.setattr(jm, "_emitir_sse", lambda *a, **k: None)
    monkeypatch.setattr(jm, "registrar_caso", lambda _id, fila: registrados.append(fila))
    monkeypatch.setattr(jm, "reportar_progreso", lambda *a, **k: None)
    return estados, registrados


def test_satje_caido_pausa_y_reintenta_el_mismo_numero(monkeypatch, _aislado):
    estados, registrados = _aislado
    monkeypatch.setattr(runner, "SATJE_BACKOFF_MIN", (0.0005,))  # 30 ms
    llamadas = []

    def _procesar(numero, ctx):
        llamadas.append(numero)
        if len(llamadas) < 3:
            raise api.SatjeNoDisponible("SATJE sin respuesta en buscarCausas")
        return {"Número de juicio": numero, "Estado de Juicio": "Sin resultados"}

    monkeypatch.setattr(runner, "_procesar_con_timeout", _procesar)
    runner.ejecutar_job(1, threading.Event(), threading.Event())
    assert llamadas == ["09332-2025-00007"] * 3
    assert [f["Estado de Juicio"] for f in registrados] == ["Sin resultados"]
    assert estados[-1] == "completado"
    assert runner._satje["fallos"] == 0  # SATJE respondió: backoff reiniciado


def test_kill_corta_la_espera(monkeypatch, _aislado):
    estados, registrados = _aislado
    kill, llamado = threading.Event(), threading.Event()
    llamadas = []

    def _procesar(numero, ctx):
        llamadas.append(numero)
        llamado.set()
        raise requests.exceptions.ConnectTimeout(
            "HTTPSConnectionPool(host='api.funcionjudicial.gob.ec', port=443): connect timeout=8")

    monkeypatch.setattr(runner, "_procesar_con_timeout", _procesar)
    t = threading.Thread(target=runner.ejecutar_job, args=(1, threading.Event(), kill))
    t.start()
    assert llamado.wait(5)
    threading.Event().wait(0.3)  # ya está en la pausa de 2 min
    kill.set()
    t.join(timeout=3)  # si no respeta kill, sigue esperando
    assert not t.is_alive()
    assert llamadas == ["09332-2025-00007"]
    assert estados[-1] == "cancelado" and registrados == []


def test_pausa_respeta_pause_y_backoff_crece():
    runner._satje_resultado(1, "x", caido=True)
    assert runner._satje["fallos"] == 1
    runner._satje_resultado(2, "y", caido=True)   # otro job cae en la misma pausa
    assert runner._satje["fallos"] == 1
    pausa = threading.Event()
    pausa.set()
    assert runner._esperar_turno_satje(1, pausa, threading.Event()) is False


def test_satje_caido_solo_por_red_hacia_satje():
    assert runner._satje_caido(api.SatjeNoDisponible("x"))
    assert runner._satje_caido(requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='api.funcionjudicial.gob.ec', port=443)"))
    assert not runner._satje_caido(requests.exceptions.ReadTimeout("openrouter.ai timed out"))
    assert not runner._satje_caido(ValueError("satje"))


def test_numeros_a_recuperar_toma_el_ultimo_registro():
    filas = [
        ("09332-2025-00001", "Error de procesamiento", "[SATJE_ERROR] SatjeNoDisponible: x"),
        ("09332-2025-00002", "Error de procesamiento", "[RED_TIMEOUT] ConnectTimeout: x"),
        ("09332-2025-00002", "Sin resultados", ""),                      # ya se recuperó
        ("09332-2025-00003", "Error de procesamiento", "[PDF_ERROR] x"),  # otro error
        ("17230-2024-00010", "Con sentencia", ""),
        ("17230-2024-00010", "Error de procesamiento", "[RED_TIMEOUT] x"),
    ]
    assert jm._numeros_a_recuperar(filas) == ["09332-2025-00001", "17230-2024-00010"]


def test_accion_reintentar_errores_red_crea_un_job_por_par_una_vez(monkeypatch):
    hechas = []
    filas = [("09332-2025-00009", "Error de procesamiento", "[SATJE_ERROR] x"),
             ("09332-2025-00004", "Error de procesamiento", "[SATJE_ERROR] x"),
             ("17230-2024-00010", "Error de procesamiento", "[RED_TIMEOUT] x")]

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): self.sql = sql
        def fetchall(self):
            return [(m,) for m in hechas] if "iniciado_por" in self.sql else list(filas)

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()

    creados = []
    monkeypatch.setattr(jm, "_conn", lambda: Conn())
    monkeypatch.setattr(jm, "crear_job", lambda **k: creados.append(k) or {"id": len(creados)})
    spec = json.dumps({"reintentar_errores_red": [{"id": "satje-21oct", "desde": "2026-10-21T00:00:00Z"}]})
    assert jm.acciones_arranque(spec) == [1, 2]
    assert [(c["tribunal"], c["año"], c["desde"], c["hasta"]) for c in creados] == [
        ("09332", "2025", 4, 9), ("17230", "2024", 10, 10)]
    assert json.loads(creados[0]["parametros"]) == {"numeros": [4, 9]}
    assert all(c["solo_descarga"] and c["workers"] == "reintento"
               and c["iniciado_por"] == "arranque:satje-21oct" for c in creados)
    hechas.append("arranque:satje-21oct")
    assert jm.acciones_arranque(spec) == []


def test_job_reintento_solo_procesa_los_numeros_indicados(monkeypatch, _aislado):
    job = jm.obtener_job(1) | {"workers": "reintento", "desde": 4, "hasta": 9,
                               "parametros": json.dumps({"numeros": [4, 9]})}
    monkeypatch.setattr(jm, "obtener_job", lambda _id: job)
    vistos = []
    monkeypatch.setattr(runner, "_procesar_con_timeout",
                        lambda numero, ctx: vistos.append(numero) or
                        {"Número de juicio": numero, "Estado de Juicio": "Sin resultados"})
    runner.ejecutar_job(1, threading.Event(), threading.Event())
    assert vistos == ["09332-2025-00004", "09332-2025-00009"]
