"""Re-índice de números anotados como de otra materia (worker/reindexar_worker.py)."""
import threading
from worker import reindexar_worker as rw
from config import materia as MATERIA

MAT = next(iter(MATERIA.IDS_MATERIA_SATJE))


class _Cur:
    def __init__(self, log): self.log = log
    def execute(self, sql, args=None): self.log.append((" ".join(sql.split())[:40], args))
    def __enter__(self): return self
    def __exit__(self, *a): pass


class _Conn:
    def __init__(self, log): self.log = log
    def cursor(self): return _Cur(self.log)
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_corrige_el_indice_solo_si_hay_juicio_de_la_materia(monkeypatch):
    escrito = []
    lotes = [["01-2020-00001", "01-2020-00002"], []]
    monkeypatch.setattr(rw, "_tabla", lambda: None)
    monkeypatch.setattr(rw, "_pendientes", lambda *a: lotes.pop(0))
    monkeypatch.setattr(rw, "_todos", lambda n: [{"idMateria": 999}, {"idMateria": MAT, "nombreDelito": "COBRO"}]
                        if n.endswith("1") else [{"idMateria": 999}])
    monkeypatch.setattr(rw.jm, "_conn", lambda: _Conn(escrito))
    tot = rw.correr(crear_jobs=False)
    assert tot == {"revisados": 2, "encontrados": 1, "sin_respuesta": 0, "encolados": 0}
    updates = [a for s, a in escrito if s.startswith("UPDATE worker_indice_satje")]
    assert updates == [(MAT, "COBRO", "01-2020-00001")]


def test_sin_respuesta_no_se_anota(monkeypatch):
    escrito = []
    lotes = [["01-2020-00003"], []]
    monkeypatch.setattr(rw, "_tabla", lambda: None)
    monkeypatch.setattr(rw, "_pendientes", lambda *a: lotes.pop(0))
    monkeypatch.setattr(rw, "_todos", lambda n: None)
    monkeypatch.setattr(rw.jm, "_conn", lambda: _Conn(escrito))
    tot = rw.correr(crear_jobs=False)
    assert tot["sin_respuesta"] == 1 and not escrito


def test_detener_corta(monkeypatch):
    monkeypatch.setattr(rw, "_tabla", lambda: None)
    monkeypatch.setattr(rw, "_pendientes", lambda *a: ["x"])
    ev = threading.Event(); ev.set()
    assert rw.correr(crear_jobs=False, detener=ev)["revisados"] == 0


def test_el_runner_despacha_el_job():
    src = open("worker/runner.py", encoding="utf-8").read()
    assert '"reindexar_otra_materia" in activos' in src


def test_espera_si_satje_no_esta_disponible(monkeypatch):
    escrito = []
    lotes = [["01-2020-00009"], []]
    llamadas = {"n": 0}

    def todos(n):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise rw.api.SatjeNoDisponible("freno de emergencia activo")
        return [{"idMateria": 999}]
    monkeypatch.setattr(rw, "_tabla", lambda: None)
    monkeypatch.setattr(rw, "_pendientes", lambda *a: lotes.pop(0))
    monkeypatch.setattr(rw, "_todos", todos)
    monkeypatch.setattr(rw.jm, "_conn", lambda: _Conn(escrito))
    ev = threading.Event()
    monkeypatch.setattr(ev, "wait", lambda t=None: False)       # no dormir en la prueba
    tot = rw.correr(crear_jobs=False, detener=ev)
    assert llamadas["n"] == 2 and tot["revisados"] == 1


def test_corte_de_conexion_a_la_base_se_reintenta(monkeypatch):
    import psycopg2
    escrito = []
    lotes = [["01-2020-00010"], []]
    intentos = {"n": 0}

    def conn():
        intentos["n"] += 1
        if intentos["n"] == 1:
            raise psycopg2.InterfaceError("connection already closed")
        return _Conn(escrito)
    monkeypatch.setattr(rw, "_tabla", lambda: None)
    monkeypatch.setattr(rw, "_pendientes", lambda *a: lotes.pop(0))
    monkeypatch.setattr(rw, "_todos", lambda n: [{"idMateria": 999}])
    monkeypatch.setattr(rw.jm, "_conn", conn)
    ev = threading.Event()
    monkeypatch.setattr(ev, "wait", lambda t=None: False)
    tot = rw.correr(crear_jobs=False, detener=ev)
    assert tot["revisados"] == 1 and intentos["n"] == 2
