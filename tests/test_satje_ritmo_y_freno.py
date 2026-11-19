"""Protecciones de la salida a SATJE: ritmo, freno de emergencia y proxy obligatorio.

Contexto: el 16-22 oct el índice de civil hizo ~14 consultas/s desde la IP de
Render y SATJE la bloqueó; el proxy existía en el código pero nadie lo activaba.
"""
import threading
import time

import pytest
import requests

import api


class _R:
    def __init__(self, code=200): self.status_code = code


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    monkeypatch.setattr(api, "_proximo_turno", 0.0)
    monkeypatch.setattr(api, "_ventana", api.collections.deque())
    monkeypatch.setattr(api, "_freno_hasta", 0.0)
    monkeypatch.setattr(api, "_sin_proxy", False)
    monkeypatch.setattr(api, "_PAUSA_REINTENTO", 0)


def test_ritmo_global_entre_hilos(monkeypatch):
    # 10 consultas desde 5 hilos a 20/s deben tardar ~0,45 s, no ~0.
    monkeypatch.setattr(api, "_MAX_RPS", 20.0)
    monkeypatch.setattr(api.requests, "get", lambda *a, **k: _R(200))
    t0 = time.monotonic()
    hilos = [threading.Thread(target=lambda: [api._get("u") for _ in range(2)]) for _ in range(5)]
    [h.start() for h in hilos]; [h.join() for h in hilos]
    assert time.monotonic() - t0 >= 0.40


def test_freno_de_emergencia_deja_de_consultar(monkeypatch):
    monkeypatch.setattr(api, "_MAX_RPS", 0)
    monkeypatch.setattr(api, "_FRENO_FALLOS", 3)
    llamadas = []
    monkeypatch.setattr(api.requests, "get", lambda *a, **k: llamadas.append(1) or _R(429))
    for _ in range(3):
        api._get("u")                       # 3 rechazos seguidos → freno
    with pytest.raises(api.SatjeNoDisponible, match="freno"):
        api._get("u")
    assert len(llamadas) == 3               # con el freno puesto no salió nada


def test_un_tropiezo_con_mayoria_de_exitos_no_frena(monkeypatch):
    # 26-oct 04:28: un corte de segundos con ~20 consultas en paralelo juntaba
    # 5 fallos seguidos y frenaba civil 17 min. Ahora cuenta la proporción.
    monkeypatch.setattr(api, "_MAX_RPS", 0)
    monkeypatch.setattr(api, "_FRENO_FALLOS", 3)
    respuestas = iter([_R(200)] * 10 + [_R(503)] * 3 + [_R(200)])
    monkeypatch.setattr(api.requests, "get", lambda *a, **k: next(respuestas))
    for _ in range(14):
        api._get("u")
    assert api._freno_hasta == 0.0


def test_los_fallos_viejos_se_olvidan(monkeypatch):
    monkeypatch.setattr(api, "_MAX_RPS", 0)
    monkeypatch.setattr(api, "_FRENO_FALLOS", 3)
    monkeypatch.setattr(api, "_FRENO_VENTANA_S", 0.05)
    monkeypatch.setattr(api.requests, "get", lambda *a, **k: _R(503))
    for _ in range(2):
        api._get("u")
    time.sleep(0.1)                         # salen de la ventana
    api._get("u")
    assert api._freno_hasta == 0.0


def test_proxy_obligatorio_en_produccion(monkeypatch):
    monkeypatch.setattr("config.config.cargar_proxy_brightdata", lambda: ("", False))
    monkeypatch.setenv("RENDER", "true")
    activo, detalle = api.activar_proxy_desde_entorno()
    assert not activo and "SIN PROXY" in detalle
    llamadas = []
    monkeypatch.setattr(api.requests, "post", lambda *a, **k: llamadas.append(1) or _R(200))
    with pytest.raises(api.SatjeNoDisponible, match="sin proxy"):
        api._post("u")
    assert llamadas == []                   # nunca salió desde la IP del servidor


def test_proxy_se_activa_si_hay_url(monkeypatch):
    monkeypatch.setattr("config.config.cargar_proxy_brightdata", lambda: ("http://u:p@brd.superproxy.io:33335", False))
    activo, _ = api.activar_proxy_desde_entorno()
    assert activo and api._PROXIES == {"http": "http://u:p@brd.superproxy.io:33335",
                                       "https": "http://u:p@brd.superproxy.io:33335"}


def test_el_arranque_activa_el_proxy_antes_de_los_jobs():
    from pathlib import Path
    src = Path(api.__file__).with_name("main.py").read_text(encoding="utf-8")
    assert src.index("activar_proxy_desde_entorno()") < src.index("crear_jobs_arranque()")
